# -*- coding: utf-8 -*-
"""把 docx 页眉法重建的逐页内容写成带**印刷页码**标记的 Markdown，供入库。

这几卷 docx 的分节结构是：

    [只含页眉的节，页眉里印着页码] → [该页正文节] → [该页脚注节] → 下一页……

例（第8卷）::

    节1000  页眉「日 记 643」 0 字      ← 页眉节，页码 643
    节1001  无页眉           926 字    ← 643 页正文
    节1002  无页眉            31 字    ← 643 页脚注
    节1003  页眉「644 示例文集」0 字  ← 下一页页眉

因此**无页码节一律「承前」**归入最近一个带页码的页；不插值、不编造页码。
第一个带页码节之前的内容记为「卷首」（无印刷页码），写入文件头，不冒充某一页。

页码标记 `--- 第 N 页 ---` 里的 N 是**书页上印的页码**（不是 PDF 页序），故这些卷偏移量为 0。

用法：
    python -B _docx_pages_to_md.py [卷号...] [--corpus NAME]

不传卷号时用 `--vols`（逗号分隔），默认 `1`。
`--corpus` 指定文集名，用于生成标题与输出文件名；默认取 `--corpus` 的缺省值「文集」。
**页码与卷号都不是猜出来的**：卷号由你给，页码取自页眉；两者都不会被插值补全。
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

TOOLKIT = Path(__file__).resolve().parent
from _paths import LazyPath, lit_dir  # 同目录共享模块
LIT = LazyPath(lit_dir)
PAGES_DIR = TOOLKIT / "_docx_pages"

HEADER = """# {corpus} 第{vol}卷（按页重建，带印刷页码）

> **来源与可信度说明（务必先读）**
> - 本文件**不是 OCR 结果**：由 `{src}` 转换件按页重建，共 {npages} 页。
> - 页码标记为标题行 `#### 第 N 页`：N 是**书页上印的页码**（取自该页页眉，如「644 某某文集」），
>   不是 PDF 页序；**使用本文件时偏移量为 0**。用标题标页还有一个好处——知识库切块时
>   章节名即页码，检索结果能直接显示命中页。
> - 分节结构为「页眉节（印页码）→ 正文节 → 脚注节」，无页码的节一律归入其前一个带页码的页，
>   只归并不插值，故**不会编造页码**；页码直接来自页眉的页 {fromhead} 页。
> - 生成时间：{when}｜生成脚本：`_docx_pages_to_md.py`
> - **引用仍须回查纸本**：文本来自扫描转换，可能有误识。
{extra}
---
"""


def assign(pages: list[dict]) -> tuple[list[int | None], list[str]]:
    """无页码节「承前」归入最近一个带页码的页。"""
    first = next((i for i, p in enumerate(pages) if p["printed"]), None)
    out: list[int | None] = [None] * len(pages)
    src: list[str] = [""] * len(pages)
    cur = None
    for i, p in enumerate(pages):
        if p["printed"]:
            cur = p["printed"]
            out[i], src[i] = cur, "页眉"
        elif first is None or i < first:
            out[i], src[i] = None, "卷首"
        else:
            out[i], src[i] = cur, "承前"
    return out, src


def main() -> int:
    ap = argparse.ArgumentParser(description="把 docx 页眉法重建的逐页内容写成带印刷页码的 Markdown")
    ap.add_argument("vols_pos", nargs="*", type=int, help="卷号（位置参数）")
    ap.add_argument("--vols", default="", help="卷号，逗号分隔，如 2,3,6,7,8")
    ap.add_argument("--corpus", default="文集", help="文集名，用于标题与输出文件名")
    args = ap.parse_args()

    vols = args.vols_pos or ([int(x) for x in args.vols.split(",") if x.strip()] if args.vols else [1])
    for vol in vols:
        src_json = PAGES_DIR / f"_docx_pages_{vol}.json"
        if not src_json.exists():
            print(f"第{vol}卷：缺 {src_json.name}，先跑 _docx_pages_index.py")
            continue
        data = json.loads(src_json.read_text(encoding="utf-8"))
        pages = data["pages"]
        nums, src = assign(pages)

        # 连续同页码的节合并为一块（页眉节本身 0 字，通常与正文节合并）
        blocks: list[dict] = []
        for p, n, s in zip(pages, nums, src):
            if blocks and blocks[-1]["printed"] == n and n is not None:
                blocks[-1]["text"] += ("\n" + p["text"]) if p["text"].strip() else ""
                if not blocks[-1]["head"] and p["head"]:
                    blocks[-1]["head"] = p["head"]
                continue
            blocks.append({"printed": n, "src": s, "head": p["head"], "text": p["text"]})

        front = [b for b in blocks if b["printed"] is None]
        real = [b for b in blocks if b["printed"] is not None]

        # 页码跳跃与回退的记录（供人工核查，不参与生成）
        seq = [b["printed"] for b in real]
        jumps = [(seq[i], seq[i + 1]) for i in range(len(seq) - 1) if seq[i + 1] - seq[i] not in (0, 1)]
        extra = ""
        if front:
            chars = sum(len(b["text"]) for b in front)
            extra = (f"> - 卷首（第一个带页码节之前）另有 {len(front)} 块 / {chars:,} 字，无印刷页码，"
                     f"列于本说明之后、正文之前。\n")
        if jumps:
            extra += f"> - 页眉页码出现 {len(jumps)} 处跳号/回退（如 " + \
                     "、".join(f"{a}→{b}" for a, b in jumps[:6]) + "），这些位置可能有缺页或编号重启。\n"

        out = LIT / f"{args.corpus} 第{vol}卷.PAGES.md"
        with out.open("w", encoding="utf-8") as fh:
            fh.write(HEADER.format(corpus=args.corpus, vol=vol, src=data["file"], npages=len(real),
                                   fromhead=sum(1 for b in real if b["src"] == "页眉"),
                                   when=time.strftime("%Y-%m-%d %H:%M"), extra=extra))
            if front:
                fh.write("\n## 卷首（无印刷页码）\n\n")
                for b in front:
                    if b["head"]:
                        fh.write(f"【页眉】{b['head']}\n")
                    fh.write(b["text"].strip() + "\n\n")
            for b in real:
                # 用**标题**而非水平分隔线标页：这样知识库切块时章节名就是页码，
                # 检索结果不会把整卷误标成「§卷首」。
                fh.write(f"\n#### 第 {b['printed']} 页\n\n")
                if b["head"]:
                    fh.write(f"【页眉】{b['head']}\n")
                fh.write(b["text"].strip() + "\n")

        text = out.read_text(encoding="utf-8")
        marks = re.findall(r"^#{2,6}\s*第\s*(\d+)\s*页\s*$", text, re.M)
        uniq = len(set(marks))
        print(f"第{vol}卷：节 {len(pages)} → 页块 {len(real)}（卷首 {len(front)}）"
              f"｜页码标记 {len(marks)} 个（不重复 {uniq}）｜承前归并 {Counter(src)['承前']} 节")
        print(f"   页码范围 {min(int(x) for x in marks)}–{max(int(x) for x in marks)}"
              f"｜跳号/回退 {len(jumps)} 处｜已写出 {out.name}（{out.stat().st_size/1024/1024:.1f} MB）")
        if jumps:
            print("   前几处跳号：" + "、".join(f"{a}→{b}" for a, b in jumps[:8]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
