# -*- coding: utf-8 -*-
"""引文校勘：把论文引文与命中页原文逐字对比，列出差异。

动机：页码定位成功 ≠ 引文无误。复跑时曾发现若干引文与原书有出入（形如「异文甲／异文乙」
这类一字之差）。本脚本用 difflib 把**归一化后**的论文引文与命中处的
原文窗口对齐，列出所有 增/删/改，供作者按纸本复核。

注意：比对基准是 OCR 文本，OCR 自身有误识（如把文集名认错字），差异未必都是论文的错；
      每条只报"差异"，不替作者判定谁对谁错。

输出：05_文献数据库/引文校勘清单.{md,csv}
"""
import csv
import difflib
import io
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cite_page_candidates as C  # noqa: E402
import _quote_spans as Q  # noqa: E402

from _paths import LazyPath, db_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = LazyPath(db_dir)
def main() -> int:
    rows = list(csv.DictReader(io.open(DB / "引文页码候选.csv", encoding="utf-8-sig")))
    # 重新抽取完整引文（CSV 里截断到 60 字）
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

    ocr_cache: dict[int, dict] = {}
    out_rows = []
    for r in rows:
        if not r["状态"].startswith("✅"):
            continue
        vol = C.CN_NUM.get(r["卷"][1])
        if vol not in C.OCR_VOLUMES:
            continue
        ocr_cache.setdefault(vol, C.load_ocr(vol))
        data = ocr_cache[vol]
        paper_q = full_quote.get(int(r["序号"]), r["引文"])
        try:
            page = int(r["PDF页"])
        except (TypeError, ValueError):
            continue
        # 命中页 ±1 页拼成搜索面（引文可能跨页）
        span_pages = [p for p in (page - 1, page, page + 1) if p in data["pages"]]
        hay = "".join(data["pages"][p][1] for p in span_pages)

        segs = C.split_ellipsis(paper_q) or [paper_q]
        all_ops = []
        win_parts = []
        for seg in segs:
            na = C.norm(seg)
            if len(na) < 4:
                continue
            sm = difflib.SequenceMatcher(None, na, hay, autojunk=False)
            blocks = [b for b in sm.get_matching_blocks() if b.size >= 1]
            if not blocks:
                all_ops.append(("未对齐", na[:20], "", "", ""))
                continue
            # 以最大的匹配块为锚，向两侧合并间距 ≤12 的块（避免零散单字把窗口拉飞）
            k = max(range(len(blocks)), key=lambda i: blocks[i].size)
            lo = hi = k
            while lo > 0 and blocks[lo].b - (blocks[lo - 1].b + blocks[lo - 1].size) <= 12:
                lo -= 1
            while hi < len(blocks) - 1 and blocks[hi + 1].b - (blocks[hi].b + blocks[hi].size) <= 12:
                hi += 1
            j1 = blocks[lo].b
            j2 = blocks[hi].b + blocks[hi].size
            win = hay[j1:j2]          # 只留匹配区，避免窗口边界造成的假差异
            win_parts.append(win)
            sm2 = difflib.SequenceMatcher(None, na, win, autojunk=False)
            for tag, i1, i2, k1, k2 in sm2.get_opcodes():
                if tag == "equal":
                    continue
                all_ops.append((tag, na[i1:i2], win[k1:k2],
                                na[max(0, i1 - 8):i1], na[i2:i2 + 8]))
        out_rows.append({
            "序号": r["序号"], "论文": r["来源论文"], "卷": r["卷"],
            "PDF页": page, "印刷页": r["印刷页码候选"], "相似度": r["相似度"],
            "差异数": len(all_ops),
            "论文引文": paper_q,
            "原文窗口（归一）": " …… ".join(win_parts),
            "差异明细": "；".join(
                f"{'论文多字' if t == 'delete' else '原文多字(疑论文漏字)' if t == 'insert' else '未对齐' if t == '未对齐' else '改字'}"
                f"「{a}」→「{b}」" + (f"（前文…{c}）" if c else "")
                for t, a, b, c, _d in all_ops) or "无差异（逐字一致）",
        })

    with (DB / "引文校勘清单.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    # ── 第二阶段：无扫描件各卷（第2/3/6/7/8卷），用全文窗口做同样的对称比对 ──
    print("\n（无扫描件各卷：逐字/近乎逐字命中者一并对勘）")
    full_cache: dict[int, str] = {}
    for r in rows:
        if not r["状态"].startswith("⛔"):
            continue
        if "逐字命中" not in r["备注"] and "近乎逐字" not in r["备注"]:
            continue
        vol = C.CN_NUM.get(r["卷"][1])
        if vol not in C.FULL_VOLUMES:
            continue
        full_cache.setdefault(vol, C.load_full(vol))
        ftxt = full_cache[vol]
        if not ftxt:
            continue
        paper_q = full_quote.get(int(r["序号"]), r["引文"])
        segs = C.split_ellipsis(paper_q) or [paper_q]
        all_ops, win_parts = [], []
        for seg in segs:
            na = C.norm(seg)
            if len(na) < 4:
                continue
            cov, win = C.locate_full(ftxt, seg)
            if cov < 0.7 or not win:
                all_ops.append(("未对齐", na[:20], "", "", ""))
                continue
            win_parts.append(win)
            sm = difflib.SequenceMatcher(None, na, win, autojunk=False)
            blocks = [b for b in sm.get_matching_blocks() if b.size >= 1]
            k = max(range(len(blocks)), key=lambda i: blocks[i].size)
            lo = hi = k
            while lo > 0 and blocks[lo].b - (blocks[lo - 1].b + blocks[lo - 1].size) <= 12:
                lo -= 1
            while hi < len(blocks) - 1 and blocks[hi + 1].b - (blocks[hi].b + blocks[hi].size) <= 12:
                hi += 1
            j1, j2 = blocks[lo].b, blocks[hi].b + blocks[hi].size
            core = win[j1:j2]
            sm2 = difflib.SequenceMatcher(None, na, core, autojunk=False)
            for tag, i1, i2, k1, k2 in sm2.get_opcodes():
                if tag == "equal":
                    continue
                all_ops.append((tag, na[i1:i2], core[k1:k2],
                                na[max(0, i1 - 8):i1], na[i2:i2 + 8]))
        out_rows.append({
            "序号": r["序号"], "论文": r["来源论文"], "卷": r["卷"],
            "PDF页": "无扫描件", "印刷页": "无法给页码（该卷无扫描件）", "相似度": r["相似度"],
            "差异数": len(all_ops), "论文引文": paper_q,
            "原文窗口（归一）": " …… ".join(win_parts),
            "差异明细": "；".join(
                f"{'论文多字' if t == 'delete' else '原文多字(疑论文漏字)' if t == 'insert' else '未对齐' if t == '未对齐' else '改字'}"
                f"「{a}」→「{b}」" + (f"（前文…{c}）" if c else "")
                for t, a, b, c, _d in all_ops) or "无差异（逐字一致）",
        })

    clean = [r for r in out_rows if r["差异数"] == 0]
    dirty = [r for r in out_rows if r["差异数"] > 0]
    lines = [
        "# 引文校勘清单（论文引文 × 命中页原文）",
        "",
        f"- 比对条目：**{len(out_rows)}** 条（仅限已定位到扫描页的引文）",
        f"- 逐字一致：**{len(clean)}** 条｜存在差异：**{len(dirty)}** 条",
        "- 比对基准：扫描卷 OCR 文本。**OCR 本身有误识**，差异未必出自论文；请按纸本判定。",
        "",
    ]
    for r in dirty:
        lines += [
            f"## [{r['序号']}] {r['论文']}｜{r['卷']}｜印刷 {r['印刷页']}"
            f"（PDF {r['PDF页']}，相似度 {r['相似度']}）",
            "",
            f"- 论文引文：{r['论文引文']}",
            f"- 原页文字（同一段，归一化）：{r['原文窗口（归一）']}",
            f"- 差异 {r['差异数']} 处：{r['差异明细']}",
            "",
        ]
    if clean:
        lines += ["## 逐字一致条目", ""]
        for r in clean:
            lines.append(f"- [{r['序号']}] {r['论文']}｜{r['卷']}｜印刷 {r['印刷页']}：{r['论文引文'][:40]}")
        lines.append("")
    (DB / "引文校勘清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"比对 {len(out_rows)} 条：逐字一致 {len(clean)}｜有差异 {len(dirty)}")
    for r in dirty:
        print(f"\n[{r['序号']}] {r['论文']}｜{r['卷']}｜印刷 {r['印刷页']}（{r['差异数']} 处差异）")
        print(f"  论文：{r['论文引文'][:80]}")
        print(f"  原文：{r['原文窗口（归一）'][:80]}")
        print(f"  差异：{r['差异明细'][:160]}")
    print(f"\n已写出：{DB}\\引文校勘清单.md / .csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
