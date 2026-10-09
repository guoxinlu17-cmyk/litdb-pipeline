# -*- coding: utf-8 -*-
"""把「无扫描件各卷」的引文落到印刷页码——用 docx 重建的逐页索引，不 OCR。

输入：
  * _docx_pages/_docx_pages_{2,3,6,7,8}.json（由 _docx_pages_index.py 生成，含每页正文与页眉页码）
  * 05_文献数据库/引文页码候选.csv（筛出状态为「⛔ 无扫描件」且逐字/近乎逐字命中的条目）

输出：05_文献数据库/无扫描卷页码候选.{csv,md}

判定口径：
  * 逐页精确命中 → 印刷页码直接取该页页眉所印数字；
  * 未逐字命中则算每页覆盖率，取最高者，并标注相似度；
  * 每页都附带「邻页页码是否 ±1」的连续性自证与页内原文片段。

用法：python -B _resolve_full_pages.py
"""
import csv
import io
import json
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cite_page_candidates as C  # noqa: E402
import _quote_spans as Q  # noqa: E402

TOOLKIT = Path(__file__).resolve().parent
PAGES_DIR = TOOLKIT / "_docx_pages"
from _paths import db_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = Path(db_dir())
VOLS = (2, 3, 6, 7, 8)
DROP = set("…\u2026 \t\r\n\u3000，。、；：（）《》“”‘’\"'.,;:()[]【】·—–-！？!?／/｜|　")


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch not in DROP)


def load_volume(vol: int) -> dict:
    path = PAGES_DIR / f"_docx_pages_{vol}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for p in data["pages"]:
        p["ntext"] = norm(p["text"])
    return data


def context_of(text: str, needle_n: str, span: int = 70) -> str:
    n = norm(text)
    i = n.find(needle_n[:20])
    if i < 0:
        return n[:span * 2]
    # 归一位置 → 原文位置（近似：按比例走）
    lo = max(0, i - span // 2)
    return n[lo:lo + span + 20]


def resolve_one(data: dict, quote: str) -> dict:
    """返回 {page, printed, how, ratio, ctx, all_pages}"""
    segs = C.split_ellipsis(quote) or [quote]
    needle = max((norm(s) for s in segs), key=len)
    if len(needle) < 6:
        return {"how": "引文过短", "all_pages": []}
    hits = [p for p in data["pages"] if needle in p["ntext"]]
    if hits:
        p = hits[0]
        return {"page": p["seq"], "printed": p["printed"], "head": p["head"], "how": "逐页精确",
                "ratio": 1.0, "ctx": context_of(p["text"], needle),
                "all_pages": [(h["seq"], h["printed"]) for h in hits]}
    # 覆盖率退而求其次
    best, best_p = 0.0, None
    for p in data["pages"]:
        if len(p["ntext"]) < 50:
            continue
        sm = SequenceMatcher(None, needle, p["ntext"], autojunk=False)
        cov = sum(b.size for b in sm.get_matching_blocks()) / len(needle)
        if cov > best:
            best, best_p = cov, p
    if best_p is None:
        return {"how": "未命中", "all_pages": []}
    return {"page": best_p["seq"], "printed": best_p["printed"], "head": best_p["head"],
            "how": "覆盖率最高", "ratio": round(best, 3),
            "ctx": context_of(best_p["text"], needle), "all_pages": []}


def neighbour_check(data: dict, seq: int) -> str:
    """用邻页页码自证：前后各两页的页码若与本页构成连续序列，则可信。"""
    idx = {p["seq"]: p for p in data["pages"]}
    cur = idx.get(seq, {}).get("printed")
    if cur is None:
        return "⚠️ 本页页眉未印页码"
    ok = 0
    tot = 0
    for d in (-2, -1, 1, 2):
        nb = idx.get(seq + d, {}).get("printed")
        if nb is not None:
            tot += 1
            if abs(nb - cur) == abs(d) or (d < 0 and cur - nb == -d) or (d > 0 and nb - cur == d):
                ok += 1
    return f"✅ 邻页页码连续（{ok}/{tot}）" if tot and ok >= max(1, tot - 1) else f"⚠️ 邻页页码不连续（{ok}/{tot}）"


def main() -> int:
    rows = list(csv.DictReader(io.open(DB / "引文页码候选.csv", encoding="utf-8-sig")))
    targets = [r for r in rows
               if r["状态"].startswith("⛔")
               and ("逐字命中" in r["备注"] or "近乎逐字" in r["备注"])]

    # 重新抽完整引文（CSV 里截断）
    full_quote: dict[int, str] = {}
    seq = 0
    for paper in C.DEFAULT_PAPERS:
        text = paper.read_text(encoding="utf-8", errors="replace")
        spans = Q.build_quote_spans(text)
        for m in C.CITE_RE.finditer(text):
            if C.CN_NUM.get(m.group(1)) is None:
                continue
            seq += 1
            q, _g, _h = C.pick_quote_v3(text, m.start(), spans)
            full_quote[seq] = q

    cache: dict[int, dict] = {}
    out = []
    for r in targets:
        vol = C.CN_NUM.get(r["卷"][1])
        if vol not in VOLS:
            continue
        cache.setdefault(vol, load_volume(vol))
        data = cache[vol]
        quote = full_quote.get(int(r["序号"]), r["引文"])
        res = resolve_one(data, quote)
        res.update({"序号": r["序号"], "论文": r["来源论文"], "卷": r["卷"], "引文": quote})
        if res.get("page"):
            res["自证"] = neighbour_check(data, res["page"])
        else:
            res["自证"] = "—"
        out.append(res)

    fields = ["序号", "论文", "卷", "印刷页码", "docx页序", "页眉", "页内原文", "方式", "相似度",
              "同卷其他命中页", "自证", "引文"]
    recs = []
    for r in out:
        recs.append({
            "序号": r["序号"], "论文": r["论文"], "卷": r["卷"],
            "印刷页码": r.get("printed") if r.get("printed") else "（页眉未印页码，需核）",
            "docx页序": r.get("page", ""), "页眉": r.get("head", ""),
            "页内原文": r.get("ctx", ""), "方式": r.get("how", ""),
            "相似度": r.get("ratio", ""),
            "同卷其他命中页": "；".join(f"{p}页(seq{s})" for s, p in r.get("all_pages", [])) or "",
            "自证": r.get("自证", ""), "引文": r["引文"][:80],
        })
    with (DB / "无扫描卷页码候选.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(recs)

    lines = ["# 无扫描件各卷（第2/3/6/7/8卷）引文页码候选", "",
             "> 来源：各卷 docx 按页重建（页眉印有页码），**未做 OCR**。页码仍需回查纸本。", ""]
    for r in recs:
        lines += [f"## [{r['序号']}] {r['论文'][-4:]}｜{r['卷']}｜印刷 **{r['印刷页码']}**",
                  "",
                  f"- 引文：{r['引文']}",
                  f"- 页眉：{r['页眉']}｜docx 页序 {r['docx页序']}｜方式 {r['方式']}｜相似度 {r['相似度']}",
                  f"- 页内原文：{r['页内原文']}",
                  f"- 自证：{r['自证']}" + (f"｜同卷其他命中页：{r['同卷其他命中页']}" if r['同卷其他命中页'] else ""),
                  ""]
    (DB / "无扫描卷页码候选.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"处理 {len(recs)} 条：")
    for r in recs:
        print(f"[{r['序号']:>2}] {r['论文'][-4:]}｜{r['卷']}｜印刷 {r['印刷页码']}｜{r['方式']}"
              f"｜{r['自证'][:14]}｜{r['引文'][:30]}")
    print(f"\n已写出：{DB}\\无扫描卷页码候选.csv / .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
