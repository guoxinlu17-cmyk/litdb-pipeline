# -*- coding: utf-8 -*-
"""从 OCR 文本的 `【页眉】` 里自动标定「PDF 页序 ↔ 印刷页码」的偏移量。

背景（为什么值得单独做一个工具）
--------------------------------
书籍扫描件里，**页眉印着印刷页码**，是唯一的权威页码来源。但正文 OCR 出来的页码
与 PDF 页序之间差一个常数偏移（本次实测第 1/4/5 卷为 +7 / +7 / +13），
而这个偏移过去是**人工试出来的**——换一本书就得重来。

本工具用"**投票 + 稳健拟合**"自动求出偏移：

1. 每页从页眉里取**所有**候选数字（页码可能印在行首「674<文集名>」，
   也可能印在行末「1930年<卷目>161」；紧跟 年/月/日 的是日期，剔除）；
2. 对每个候选算 offset = PDF页序 − 候选页码，**投票**选出支持度最高的偏移；
3. 用该偏移解释全部页：能解释的为"命中"，不能解释的列为异常；
4. 检查命中页的印刷页码是否**严格 +1 递增**（这是最强的结构验证）；
5. 若异常页**成片连续**，则判为"偏移在卷中发生变化"，按段重新估计并报告分段点。

用法
----
    python -B _page_number.py <ocr.md> [...]            # 只标定并报告
    python -B _page_number.py <ocr.md> --json out.json  # 同时输出映射表
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PAGE_RE = re.compile(r"^--- 第 (\d+) 页 ---$", re.MULTILINE)
# 只认**行首**的页眉标记：文件头的说明文字里可能出现行内的「【页眉】」字样，
# 若不加锚定，说明段会被误当成页眉。
HEAD_RE = re.compile(r"^【页眉】([^\n]*)", re.MULTILINE)
HEADING_PAGE_RE = re.compile(r"^(?:#{2,6}\s*第\s*(\d+)\s*页|---\s*第\s*(\d+)\s*页\s*---)\s*$", re.MULTILINE)
SKIP_NEXT = ("年", "月", "日", "期", "卷", "辑")


def header_of(page_text: str) -> str:
    """取页眉文本。

    新版（版式感知 OCR）会单独标出 `【页眉】…`；旧版 OCR 没有这个标记，
    那时页码就印在**页首**（如「674<文集名>」「1930年<卷目>161」），
    故回退为取页面正文的开头一小段。
    """
    m = HEAD_RE.search(page_text)
    if m:
        return m.group(1).strip()
    return re.sub(r"\s+", "", page_text)[:24]


def number_candidates(head: str, scan: int = 18) -> list[int]:
    """从页眉开头 `scan` 个字里取所有候选页码（剔除日期/期号）。"""
    h = re.sub(r"\s+", "", head)[:scan]
    out: list[int] = []
    for m in re.finditer(r"\d{1,4}", h):
        if h[m.end():m.end() + 1] in SKIP_NEXT:
            continue
        v = int(m.group(0))
        if 1 <= v <= 2000:
            out.append(v)
    return out


def read_pages(path: Path) -> list[tuple[int, str]]:
    """返回 [(页序, 页眉文本)]。

    **同时支持两种页标记**：旧式 `--- 第 N 页 ---`（OCR 产物）与标题式 `#### 第 N 页`
    （`.PAGES.md` 成品）。标题式里 N 已经是印刷页码，故其偏移必然为 0——这正好当自检。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    marks = list(HEADING_PAGE_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((int(m.group(1) or m.group(2)), header_of(text[m.end():end])))
    return out


def calibrate(pages: list[tuple[int, str]], tol: int = 1) -> dict:
    """投票求偏移 + 结构校验。"""
    cand = {p: number_candidates(h) for p, h in pages}
    votes: Counter = Counter()
    for p, cs in cand.items():
        for c in cs:
            votes[p - c] += 1
    if not votes:
        return {"ok": False, "reason": "页眉里读不到任何页码数字", "pages": len(pages)}

    # 主偏移：支持度最高；并列时取绝对值小者（偏移通常为个位/十位数）
    top = sorted(votes.items(), key=lambda kv: (-kv[1], abs(kv[0])))
    main_off, main_votes = top[0]

    hits, misses, conflicts = [], [], []
    for p, cs in cand.items():
        fit = [c for c in cs if abs((p - c) - main_off) <= tol]
        if not fit:
            (conflicts if cs else misses).append((p, cs))
            continue
        # 多个候选都拟合时，选与主偏移完全相等的那个
        exact = [c for c in fit if p - c == main_off]
        hits.append((p, (exact or fit)[0]))

    hits.sort()
    # ── 结构校验：只在**相邻 PDF 页都命中页眉**时判 +1（跳过的无页眉页不算断） ──
    consec = [(a, b) for (pa, a), (pb, b) in zip(hits, hits[1:]) if pb == pa + 1]
    mono = sum(1 for a, b in consec if b - a == 1)
    mono_ratio = mono / max(1, len(consec))

    # ── 偏移分段判定：只看**命中区间内部**的连续异常页；卷首/卷尾不算 ──
    if hits:
        first_hit, last_hit = hits[0][0], hits[-1][0]
        interior = [p for p, _c in conflicts if first_hit < p < last_hit]
    else:
        interior = []
    runs = []
    for p in sorted(interior):
        if runs and p - runs[-1][-1] <= 2:
            runs[-1].append(p)
        else:
            runs.append([p])
    long_runs = [r for r in runs if len(r) >= 5]

    return {
        "ok": True,
        "pages": len(pages),
        "offset": main_off,
        "offset_votes": main_votes,
        "offset_share": round(main_votes / max(1, len(hits)), 3),
        "hit_pages": len(hits),
        "miss_pages": [p for p, _c in misses],
        "conflict_pages": [p for p, _c in conflicts],
        "leading_unmapped": first_hit - 1 if hits else 0,
        "monotonic": f"{mono}/{len(consec)} 对相邻 PDF 页为 +1",
        "mono_ratio": round(mono_ratio, 3),
        "offset_changes": [{"from_page": r[0], "to_page": r[-1], "count": len(r)} for r in long_runs],
        "offset_candidates": top[:4],
        "mapping": [{"pdf": p, "printed": n} for p, n in hits],
    }


def report(res: dict, name: str = "") -> str:
    if not res.get("ok"):
        return f"  ❌ 标定失败：{res.get('reason')}"
    seg = ""
    if res["offset_changes"]:
        seg = "｜⚠️ 偏移疑似分段：" + "、".join(
            f"第 {s['from_page']}–{s['to_page']} 页（{s['count']} 页连续异常）" for s in res["offset_changes"])
    lead = f"｜卷首未映射 {res.get('leading_unmapped', 0)} 页" if res.get("leading_unmapped") else ""
    return (
        f"  偏移 = **{res['offset']}**（投票 {res['offset_votes']} 页支持，占命中页 {res['offset_share']:.0%}）\n"
        f"  命中 {res['hit_pages']}/{res['pages']} 页｜无页眉数字 {len(res['miss_pages'])} 页"
        f"｜与主偏移冲突 {len(res['conflict_pages'])} 页{lead}{seg}\n"
        f"  相邻 PDF 页的印刷页码 +1 一致性：{res['monotonic']}（{res['mono_ratio']:.1%}）"
        + (f"\n  其他候选偏移：{res['offset_candidates']}" if len(res["offset_candidates"]) > 1 else "")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", default="")
    ap.add_argument("--tol", type=int, default=1)
    a = ap.parse_args()
    all_res = {}
    for f in a.files:
        p = Path(f)
        if not p.exists():
            print(f"（不存在）{f}")
            continue
        res = calibrate(read_pages(p), tol=a.tol)
        all_res[p.name] = res
        print("=" * 96)
        print(f"页码标定：{p.name}（{res.get('pages')} 页）")
        print(report(res, p.name))
        if res.get("mapping"):
            head = res["mapping"][:3]
            tail = res["mapping"][-3:]
            print(f"  映射样例：{head} … {tail}")
    if a.json and all_res:
        Path(a.json).write_text(json.dumps(all_res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写出：{a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
