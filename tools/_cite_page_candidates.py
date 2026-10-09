# -*- coding: utf-8 -*-
"""为论文中的「只有卷次、无页码」引文自动补出印刷页码候选（v3）。

v3 修复（v2 的 15 条 ❌「未命中」由此复检）：
  **引号配对错位**。v2 用正则 ``[“"]([^“”"]{6,400})[”"]`` 在标记前 400 字窗口内抓引文，
  把 ASCII 直引号 ``"`` 同时当开、闭引号；两篇论文正文主要用的就是直引号，一旦窗口内
  直引号个数为奇数，配对就整体错位一字，抓到的不是引文而是引文**前面**的正文
  （于是整句查库必然失败 → ❌ 未命中）。v3 改为全文一次配对（:mod:`_quote_spans`）。

抽取分三档（新增 `抽取方式` 列）：
  * `直接引语`——标记紧贴一条引语的闭引号（间隔 ≤4 非空白字符），是真正的引文；
  * `转述句·短语锚点`——标记跟在论文自己的叙述之后，取该句内最近的短引语作锚点；
  * `转述句·无锚点`——句内无引语，只能拿句末文字近似比对，**一律降级为 ⚠️ 近似**。

其余同 v2：
  1. **失败重试**：整句未命中时，按引号/分号/句号切成片段（≥10 字）逐段匹配，取最佳；
  2. **页号自证**：命中页若同时印着推算出的印刷页码 → 高置信；否则标注需人工确认
     （这是对偏移量的一次现场复验）；
  3. **给出上下文片段**，便于人工快速判断。

输出：`05_文献数据库/引文页码候选.{csv,md}`（多篇论文合并，含来源列）

⚠️ 全部为**候选值**：OCR 有误识、引文可能跨页或为转述，定稿前必须回查纸本。
"""
import argparse
import csv
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quote_spans as Q  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import db_dir, lit_dir, papers_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
LIT = Path(lit_dir())
DEFAULT_OUT = Path(db_dir())
DEFAULT_PAPERS = [
    Path(papers_dir() / "sample-paper-a.md"),
    Path(papers_dir() / "sample-paper-b.md"),
]
OFFSETS = {v: 0 for v in (1, 2, 3, 4, 5, 6, 7, 8)}
# 全 8 卷统一用 `.PAGES.md`：页码标记**就是印刷页码**，故偏移一律为 0。
#   * 第 1/4/5 卷的 .PAGES.md 由 .OCR.md 转换而来（原标记是 PDF 页序，已按 +7/+7/+13 换算）；
#   * 第 2/3/6/7/8 卷的 .PAGES.md 由 docx 页眉法生成。
# `.OCR.md` 仍在原处，是 OCR 流水线的产物，其它脚本（_ocr_md_to_docx 等）继续用它。
OCR_VOLUMES = {1: "第1卷", 4: "第4卷", 5: "第5卷",
               2: "第2卷", 3: "第3卷", 6: "第6卷", 7: "第7卷", 8: "第8卷"}
FULL_VOLUMES: dict[int, str] = {}     # 全部卷都有页级文本后，不再需要全文兜底
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
          "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8}

# 两种页码标记：OCR 版用水平分隔线（--- 第 N 页 ---），docx 页眉版用标题行（#### 第 N 页）
PAGE_SPLIT = re.compile(r"^(?:---\s*第\s*(\d+)\s*页\s*---|#{2,6}\s*第\s*(\d+)\s*页)\s*$", re.MULTILINE)
CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{CJK}])[ \t]+(?=[{CJK}])")
ELLIPSIS = re.compile(r"……|…|\.\.\.")
CITE_RE = re.compile(r"【[^】]*?(?:《?示例文集》?)?\s*第\s*([一二三四五六七八1-8])\s*卷[^】]*】")


def norm(text: str) -> str:
    text = SPACE_BETWEEN_CJK.sub("", text or "")
    text = ELLIPSIS.sub("", text)
    return re.sub(r"[\s，。、；：（）《》“”‘’\"'.,;:()\[\]【】·—–\-]+", "", text)


def vol_md(volume: int) -> Path | None:
    """该卷的页级文本文件。全 8 卷统一优先 `.PAGES.md`（页码＝印刷页，偏移 0）；
    找不到才退回 `.OCR.md`（第 1/4/5 卷的旧形式，页码为 PDF 页序）。"""
    for suffix in (".PAGES.md", ".OCR.md"):
        hits = sorted(LIT.glob(f"*{OCR_VOLUMES[volume]}*{suffix}"))
        if hits:
            return hits[0]
    return None


def load_ocr(volume: int) -> dict:
    """返回 {'pages': {页码: (原文本, 归一文本)}, 'big': 整卷归一拼接串, 'spans': [(起,止,页)]}

    同一页码出现多次时**拼接**而不是覆盖——目录页与正文页码会重启（如第6卷目录 3–6 与正文 3–6），
    覆盖会丢文本。
    """
    path = vol_md(volume)
    if path is None:
        return {"pages": {}, "big": "", "spans": [], "file": None}
    text = path.read_text(encoding="utf-8", errors="replace")
    marks = list(PAGE_SPLIT.finditer(text))
    acc: dict[int, str] = {}
    for k, m in enumerate(marks):
        num = int(m.group(1) or m.group(2))
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        body = text[m.end():end]
        acc[num] = (acc[num] + "\n" + body) if num in acc else body
    pages = {k: (v, norm(v)) for k, v in acc.items()}
    big, spans = build_index(pages)
    return {"pages": pages, "big": big, "spans": spans, "file": path.name}


def load_full(volume: int) -> str:
    """无页级文本时的兜底：读整卷全文（新版下通常为空）。"""
    if volume in OCR_VOLUMES:
        return ""
    candidates = sorted(LIT.glob(f"*{FULL_VOLUMES[volume]}*.full.md"))
    return norm(candidates[0].read_text(encoding="utf-8", errors="replace")) if candidates else ""


def best_window(needle: str, haystack: str) -> tuple[float, str]:
    n = len(needle)
    if n == 0 or not haystack:
        return 0.0, ""
    if needle in haystack:
        return 1.0, needle
    best, best_win = 0.0, ""
    step = max(1, n // 4)
    for start in range(0, max(1, len(haystack) - n + 1), step):
        window = haystack[start:start + n]
        ratio = SequenceMatcher(None, needle, window).ratio()
        if ratio > best:
            best, best_win = ratio, window
    return best, best_win


def build_index(pages: dict[int, tuple[str, str]]) -> tuple[str, list[tuple[int, int, int]]]:
    """把整卷归一化文本拼成一个大串，并记录 (起, 止, 页) 映射，用于跨页精确检索。"""
    parts, spans, pos = [], [], 0
    for page in sorted(pages):
        t = pages[page][1]
        spans.append((pos, pos + len(t), page))
        parts.append(t)
        pos += len(t)
    return "".join(parts), spans


def page_of(spans: list[tuple[int, int, int]], pos: int) -> int | None:
    lo, hi = 0, len(spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, page = spans[mid]
        if pos < start:
            hi = mid - 1
        elif pos >= end:
            lo = mid + 1
        else:
            return page
    return None


def locate(data: dict, quote: str) -> tuple[int | None, float, str, str, str]:
    """三级匹配。返回 (PDF页, 相似度, 窗口, 方式, 上下文)"""
    q = norm(quote)
    pages = data["pages"]
    if len(q) < 6:
        return None, 0.0, "", "引文过短", ""
    # ① 整卷拼接串上的精确检索（可命中跨页引文）
    idx = data["big"].find(q)
    if idx >= 0:
        start_page = page_of(data["spans"], idx)
        end_page = page_of(data["spans"], idx + len(q) - 1)
        if start_page is not None:
            how = "精确" if start_page == end_page else f"精确·跨页({start_page}–{end_page})"
            return start_page, 1.0, q[:60], how, context(pages[start_page][0], q)
    # ② 逐页精确
    for page, (raw, ntext) in pages.items():
        if q in ntext:
            return page, 1.0, q[:60], "精确·页内", context(raw, q)
    # ③ 分片锚定 + 滑窗
    size = max(4, len(q) // 4)
    frags = [q[i:i + size] for i in range(0, len(q), size)][:4]
    scored = []
    for page, (_raw, ntext) in pages.items():
        hits = sum(1 for f in frags if f and f in ntext)
        if hits:
            scored.append((hits, page))
    scored.sort(reverse=True)
    best = (None, 0.0, "", "未命中", "")
    for _hits, page in scored[:5]:
        for cand in (page - 1, page, page + 1):
            if cand not in pages:
                continue
            ratio, window = best_window(q, pages[cand][1])
            if ratio > best[1]:
                best = (cand, ratio, window, "分片+滑窗", context(pages[cand][0], window))
    return best


_DROP = set("…\u2026 \t\r\n\u3000，。、；：（）《》“”‘’\"'.,;:()[]【】·—–-")


def norm_with_map(text: str) -> tuple[str, list[int]]:
    """归一化并保留「归一后第 n 字 ↔ 原串下标」的映射，用于在**原页文字**里定位并打印上下文。"""
    chars: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text):
        if ch in _DROP:
            continue
        chars.append(ch)
        pos.append(i)
    return "".join(chars), pos


def context(raw: str, needle: str, span: int = 60) -> str:
    """在**原始页文本**中定位匹配片段，返回可读的上下文（找不到时退回页首，并标明）。"""
    n, pos = norm_with_map(raw)
    if not n:
        return ""
    hit = n.find(needle[:24])
    if hit < 0:
        return re.sub(r"\s+", " ", raw)[:span + 20] + "（⚠️ 未能在该页定位到引文片段，显示页首）"
    raw_i = pos[hit]
    start = max(0, raw_i - span // 2)
    return re.sub(r"\s+", " ", raw[start:start + span + 20])


def locate_full(txt: str, quote: str, k: int = 8) -> tuple[float, str]:
    """在无扫描件的整卷全文里做**局部覆盖率**比对（滑窗锚定 + difflib 覆盖度）。

    纯 ``in`` 判断只能给出「逐字命中/未命中」；论文引文常有一两字出入（漏字、改字、异体字），
    于是明明出自该卷却被判「未找到」。这里用 8 字滑窗锚出候选位置，再算**引文被原文覆盖的比例**：

        覆盖度 = 对齐上的字数 ÷ 引文净字数

    之所以不用 ``SequenceMatcher.ratio()``：后者按「引文＋窗口」总长归一，窗口比引文长时
    即使整句逐字命中，得分也只有 2L/(L+窗口长)（33 字的引文最高仅约 0.5），会把短引文
    一律判成"没找到"。覆盖度对短引文同样有效，且不因原文多出的字而扣分。
    """
    q = norm(quote)
    if len(q) < k or not txt:
        return 0.0, ""
    best, best_win = 0.0, ""
    seen: set[int] = set()
    step = max(1, k // 2)
    for i in range(0, len(q) - k + 1, step):
        s = q[i:i + k]
        start = 0
        while True:
            j = txt.find(s, start)
            if j < 0:
                break
            pos = max(0, j - i)
            if pos not in seen:
                seen.add(pos)
                lo = max(0, pos - 25)
                win = txt[lo:pos + len(q) + 25]
                # 用「引文被覆盖的比例」衡量，而非整体 ratio（见 docstring）
                sm = SequenceMatcher(None, q, win, autojunk=False)
                covered = sum(b.size for b in sm.get_matching_blocks())
                cov = covered / len(q)
                if cov > best:
                    best, best_win = cov, win
            start = j + 1
            if len(seen) > 400:
                return best, best_win
    return best, best_win


def split_fragments(quote: str) -> list[str]:
    parts = re.split(r"[“”\"'；;。.]", quote)
    return [p.strip() for p in parts if len(norm(p)) >= 10]


def split_ellipsis(quote: str, min_len: int = 4) -> list[str]:
    """按省略号（…… / … / ...）切分引文。

    论文里常见「民国以后，我在家奉亲，闭户读佛书……然而心里仍抛不下」这种**跳字引用**：
    省略号代表的正是原著里被略去的文字，因此 ``norm`` 把省略号一删，前后两截就会被强行
    接成一句，与原书永远对不上。必须先切段、再逐段定位。
    """
    parts = re.split(r"……|…|\.\.\.", quote)
    return [p.strip() for p in parts if len(norm(p)) >= min_len]


def locate_ellipsis(data: dict, quote: str) -> tuple[int | None, float, str, str, str]:
    """跳字引用专用定位：以最长的一段为锚，并核对其余各段落在哪一页。

    返回 ``(PDF页, 相似度, 窗口, 方式, 上下文)``；各段落页若都落在锚页 ±1 内，可信度较高。
    """
    segs = split_ellipsis(quote)
    if len(segs) < 2:
        return None, 0.0, "", "无省略号", ""
    anchor = max(segs, key=lambda s: len(norm(s)))
    page, ratio, _win, how, ctx = locate(data, anchor)
    if page is None or ratio < 0.7:
        return None, 0.0, "", "省略号·锚段未命中", ""
    plocs = []
    for s in segs:
        p2, r2, _w2, _h2, _c2 = locate(data, s)
        plocs.append((norm(s)[:12], (p2 if (p2 is not None and r2 >= 0.7) else None)))
    spread_ok = all(p is not None and abs(p - page) <= 1 for _t, p in plocs)
    how = f"省略号·锚段({how})｜段页 {plocs}｜{'各段同页±1' if spread_ok else '⚠️ 各段跨页较远'}"
    return page, ratio, norm(anchor)[:60], how, ctx


def cross_check(volume: int, quote: str, ocr_cache: dict, full_cache: dict) -> dict | None:
    """在其他卷里找这条引文：先其余 OCR 卷（可给页码），再无扫描件各卷全文（只能确认卷次）。

    跳字引用（含省略号）取其最长一段作探针，但要求该段 ≥12 字，避免短句到处命中。
    """
    needle = norm(quote)
    if len(needle) < 8:
        return None
    segs = split_ellipsis(quote)
    probe = norm(max(segs, key=lambda s: len(norm(s)))) if segs else needle
    if len(probe) < 12:
        probe = needle
    for other in OCR_VOLUMES:
        if other == volume:
            continue
        ocr_cache.setdefault(other, load_ocr(other))
        data = ocr_cache[other]
        if needle in data["big"] or probe in data["big"]:
            page, ratio, _w, how, ctx = locate(data, quote if needle in data["big"] else probe)
            if page is not None and ratio >= 0.7:
                printed = page - OFFSETS[other]
                proof = bool(re.search(rf"(?<!\d){printed}(?!\d)", data["pages"][page][0]))
                return {"volume": f"第{other}卷", "pdf": page, "printed": f"第{printed}页",
                        "ratio": f"{ratio:.2f}", "how": "跨卷·" + how, "ctx": ctx,
                        "proof": "✅ 该页印有此页码" if proof else "⚠️ 该页未见此页码"}
    for other in FULL_VOLUMES:
        full_cache.setdefault(other, load_full(other))
        if needle in full_cache[other] or probe in full_cache[other]:
            return {"volume": f"第{other}卷", "no_scan": True, "how": "跨卷·全文比对"}
    return None


def pick_quote(prefix: str) -> tuple[str, int]:
    """（v2 遗留）在给定窗口内用正则抓最后一条引语。

    ⚠️ 已弃用于主流程：直引号 ``"`` 开闭同形，窗口配对会错位。保留仅供旧脚本
    (:mod:`_diagnose_unmatched`) 兼容调用；新代码请用 :func:`pick_quote_v3`。
    """
    spans = list(re.finditer(r"[“\"]([^“”\"]{6,400})[”\"]", prefix))
    if not spans:
        return "", -1
    last = spans[-1]
    gap = len(prefix) - last.end()
    return last.group(1).strip(), gap


def pick_quote_v3(text: str, pos: int, spans: list[tuple[int, int]]) -> tuple[str, int, str]:
    """取标记 ``pos`` 处应归属的引文。返回 ``(引文, 间隔字数, 抽取方式)``。

    * ``直接引语``：标记紧贴闭引号（间隔 ≤4 非空白字符）；
    * ``转述句·短语锚点``：标记前是叙述句，取句内最近的短引语（≤100 字）作锚点；
    * ``转述句·无锚点``：句内无引语可用，退化为句末文字，调用方须降级处理。
    """
    direct = Q.direct_quote_before(spans, text, pos)
    if direct and len(norm(direct[0])) >= 8:
        return direct[0].strip(" ，。、；："), direct[1], "直接引语"

    phrase = Q.last_quoted_phrase(spans, text, pos, window=150, max_len=100)
    if phrase and len(norm(phrase[0])) >= 8:
        return phrase[0].strip(" ，。、；："), phrase[1], "转述句·短语锚点"

    sent = re.sub(r"\s+", "", Q.sentence_before(text, pos))[-90:]
    if not sent:
        sent = re.sub(r"\s+", "", text[max(0, pos - 90):pos])
    sent = re.sub(r"【[^】]*】", "", sent)   # 别把上一条的引文标注当成引文
    return sent.strip(" ，。、；："), -1, "转述句·无锚点"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--papers", nargs="*", default=[str(p) for p in DEFAULT_PAPERS])
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    a = ap.parse_args()
    outdir = Path(a.outdir)

    rows: list[dict] = []
    ocr_cache: dict[int, dict] = {}
    full_cache: dict[int, str] = {}
    seq = 0
    resolved = approximate = no_scan = unmatched = 0

    for paper_str in a.papers:
        paper = Path(paper_str).resolve()
        if not paper.exists():
            print(f"[跳过] 不存在：{paper}")
            continue
        text = paper.read_text(encoding="utf-8", errors="replace")
        marks = list(CITE_RE.finditer(text))
        spans = Q.build_quote_spans(text)
        print(f"\n=== {paper.name}：卷次标记 {len(marks)} 处｜引号配对 {len(spans)} 对 ===")
        for m in marks:
            volume = CN_NUM.get(m.group(1))
            if volume is None:
                continue
            quote, gap, how_pick = pick_quote_v3(text, m.start(), spans)
            seq += 1
            row = {"序号": seq, "来源论文": paper.stem, "卷": f"第{volume}卷", "引文": quote[:60],
                   "状态": "", "PDF页": "", "印刷页码候选": "", "相似度": "", "页号自证": "",
                   "抽取方式": how_pick, "方式": "", "上下文": "", "备注": ""}
            if how_pick == "转述句·短语锚点":
                row["备注"] = "标记前是转述句；以句内引语短语为锚点，需核对该页是否覆盖整句"
            elif how_pick == "转述句·无锚点":
                row["备注"] = "标记前是转述句且句内无引语：仅按句末文字近似定位，不可直接采信"

            if volume in OCR_VOLUMES:
                ocr_cache.setdefault(volume, load_ocr(volume))
                data = ocr_cache[volume]
                pdf_page, ratio, window, how, ctx = locate(data, quote)
                if (pdf_page is None or ratio < 0.7) and len(split_ellipsis(quote)) >= 2:
                    p3, r3, w3, h3, c3 = locate_ellipsis(data, quote)
                    if p3 is not None and r3 >= 0.7:
                        pdf_page, ratio, window, how, ctx = p3, r3, w3, h3, c3
                if pdf_page is None or ratio < 0.7:
                    for frag in split_fragments(quote):
                        p2, r2, w2, h2, c2 = locate(data, frag)
                        if p2 is not None and r2 > ratio:
                            pdf_page, ratio, window, how, ctx = p2, r2, w2, "片段·" + h2, c2
                if pdf_page is not None and ratio >= 0.7 and how_pick != "转述句·无锚点":
                    printed = pdf_page - OFFSETS[volume]
                    self_proof = bool(re.search(rf"(?<!\d){printed}(?!\d)", data["pages"][pdf_page][0]))
                    note = row.get("备注") or ("" if ratio >= 0.95 else "OCR 可能有误识，建议回查")
                    row.update({"状态": "✅ 候选", "PDF页": pdf_page, "印刷页码候选": f"第{printed}页",
                                "相似度": f"{ratio:.2f}", "方式": how, "上下文": ctx,
                                "页号自证": "✅ 该页印有此页码" if self_proof else "⚠️ 该页未见此页码",
                                "备注": note})
                    resolved += 1
                else:
                    # 跨卷核验：先看其他 OCR 卷，再看无扫描件各卷全文（可发现卷次误标）
                    alt = cross_check(volume, quote, ocr_cache, full_cache)
                    if alt:
                        row.update({"状态": "⚠️ 疑卷次误标", "PDF页": alt.get("pdf", ""),
                                    "印刷页码候选": alt.get("printed", ""), "相似度": alt.get("ratio", ""),
                                    "方式": alt["how"], "上下文": alt.get("ctx", ""),
                                    "页号自证": alt.get("proof", ""),
                                    "备注": f"所标【第{volume}卷】未检出；实见 **{alt['volume']}**。"
                                            + ("该卷无扫描件，需纸本定页" if alt.get("no_scan") else "请核对卷次")})
                        approximate += 1
                    elif pdf_page is not None and ratio >= 0.6:
                        printed = pdf_page - OFFSETS[volume]
                        cand = (f"约第{printed}页（待核）" if printed > 0
                                else f"（PDF {pdf_page} 页在卷首无印刷页码区，需纸本核对）")
                        row.update({"状态": "⚠️ 近似", "PDF页": pdf_page,
                                    "印刷页码候选": cand,
                                    "相似度": f"{ratio:.2f}", "方式": how, "上下文": ctx,
                                    "备注": (row.get("备注", "") + " 相似度偏低：可能为转述、跨页或 OCR 误识").strip()})
                        approximate += 1
                    else:
                        # 相似度过低（<0.6）时那点"命中"只是噪声，宁可报未命中，也不给假页码
                        row.update({"状态": "❌ 未命中", "方式": how, "上下文": ctx,
                                    "相似度": f"{ratio:.2f}" if pdf_page is not None else "",
                                    "备注": (row.get("备注", "") + f" 最高相似度仅 {ratio:.2f}，"
                                            "低于 0.6 视为未命中：多为 OCR 误识或卷次有误，须查纸本").strip()})
                        unmatched += 1
            else:
                full_cache.setdefault(volume, load_full(volume))
                ftxt = full_cache.get(volume, "")
                segs = split_ellipsis(quote) or [quote]
                probe = norm(max(segs, key=lambda s: len(norm(s))))[:40]
                found = bool(probe) and probe in ftxt
                sim, win = (1.0, "") if found else locate_full(ftxt, quote)
                row["方式"] = "全文比对" + ("·省略号分段" if len(segs) >= 2 else "") + f"｜局部相似度 {sim:.2f}"
                if found:
                    note = "原文在该卷文本中**逐字命中**（该卷无扫描件，无法给页码）"
                elif how_pick == "转述句·无锚点":
                    note = ("标记前为转述句、句内无引语，**无法逐字比对**；"
                            "该卷无扫描件，页码须查纸本")
                elif sim >= 0.9:
                    note = f"该卷有近乎逐字的原文（相似度 {sim:.2f}，疑字形/漏字差异），**该卷无扫描件，须回查纸本**"
                elif sim >= 0.7:
                    note = f"该卷有相近段落（相似度 {sim:.2f}，属转述或改写），**该卷无扫描件，须回查纸本**"
                else:
                    note = f"该卷文本中未找到（局部相似度仅 {sim:.2f}）——疑卷次有误或属论文推论，须回查纸本"
                row.update({"状态": "⛔ 无扫描件", "相似度": f"{sim:.2f}", "上下文": win[:60], "备注": note})
                no_scan += 1
            rows.append(row)
            if row["状态"].startswith("✅"):
                print(f"  ✅ {row['卷']}｜{row['引文'][:30]}…→ {row['印刷页码候选']}｜{row['相似度']}｜{row['页号自证']}")

    if not rows:
        print("未抓到任何引文", file=sys.stderr)
        return 1

    with (outdir / "引文页码候选.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    lines = [
        "# 论文引文 → 印刷页码候选表（v3）",
        "",
        f"- 来源论文：{len({r['来源论文'] for r in rows})} 篇｜引用标记 **{len(rows)}** 处",
        f"- 结果：**页码候选 {resolved} 条**｜近似待核 {approximate} 条｜未命中 {unmatched} 条｜"
        f"所在卷无扫描件（无法给页码）{no_scan} 条",
        f"- 偏移规则：第1卷 +{OFFSETS[1]}、第4卷 +{OFFSETS[4]}、第5卷 +{OFFSETS[5]}"
        "（印刷页 P ↔ OCR/Word 第 P+偏移 页）",
        "- 判定说明：「页号自证」= 命中页文本中是否也印着推算出的印刷页码——两项同时成立则为高置信候选。",
        "- 「抽取方式」三档：`直接引语`（标记紧贴闭引号，真引文）｜`转述句·短语锚点`"
        "（标记前是叙述句，以句内短引语为锚点）｜`转述句·无锚点`（句内无引语，一律只给 ⚠️ 近似）。",
        "- ⚠️ 全部为**候选值**：OCR 有误识、引文可能跨页或属转述，**定稿前必须回查纸本**。",
        "",
        "| 序号 | 来源 | 卷 | 引文（前 36 字） | 抽取方式 | 状态 | PDF 页 | 印刷页码候选 | 相似度 | 页号自证 | 方式 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(f"| {r['序号']} | {r['来源论文'][:10]} | {r['卷']} | {r['引文'][:36].replace('|','／')} | "
                     f"{r.get('抽取方式','')} | {r['状态']} | {r['PDF页']} | {r['印刷页码候选']} | "
                     f"{r['相似度']} | {r['页号自证']} | {r['方式']} |")
    (outdir / "引文页码候选.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n已写出：{outdir}\\引文页码候选.csv / .md")
    print(f"合计 {len(rows)} 处：候选 {resolved}｜近似 {approximate}｜未命中 {unmatched}｜无扫描件 {no_scan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
