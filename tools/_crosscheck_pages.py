# -*- coding: utf-8 -*-
"""交叉验证：`_resolve_full_pages.py`（docx 节索引直算）与 `_cite_page_candidates.py`（新 .PAGES.md 页文件）
对同一批引文给出的页码是否一致。

用法：python -B _crosscheck_pages.py
"""
import csv
import io
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import db_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = Path(db_dir())


def num(s: str) -> str:
    m = re.search(r"(\d+)", s or "")
    return m.group(1) if m else ""


def main() -> int:
    a = {int(r["序号"]): r for r in
         csv.DictReader(io.open(DB / "无扫描卷页码候选.csv", encoding="utf-8-sig"))}
    b = {int(r["序号"]): r for r in
         csv.DictReader(io.open(DB / "引文页码候选.csv", encoding="utf-8-sig"))}

    same = diff = miss = 0
    print(f"{'序号':>4} {'卷':>4} {'直算页':>6} {'页文件':>6} {'一致':>4}  引文")
    for seq in sorted(a):
        pa, pb = num(a[seq]["印刷页码"]), num(b.get(seq, {}).get("印刷页码候选", ""))
        if not pb:
            miss += 1
            flag = "缺"
        elif pa == pb:
            same += 1
            flag = "✅"
        else:
            diff += 1
            flag = "❌"
        print(f"{seq:>4} {a[seq]['卷']:>4} {pa:>6} {pb or '—':>6} {flag:>4}  {a[seq]['引文'][:26]}")
    print(f"\n一致 {same}｜不一致 {diff}｜页文件未给页 {miss}｜合计 {len(a)}")
    print("两种口径：① 直接读 docx 节索引，命中节向前找最近带页码的节；"
          "② 生成的 .PAGES.md 里按页搜索。口径独立，一致即互证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
