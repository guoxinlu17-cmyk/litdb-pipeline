# -*- coding: utf-8 -*-
"""抽查：用工具自身的归一化与定位逻辑，复核几条已定页引文落在哪一页、上下文是什么。

用法：python -B _spot_check_pages.py
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cite_page_candidates as C  # noqa: E402

# 抽查用例从**外部 JSON** 读；仓库不内置任何用例——卷次与引文属于你的项目数据。
#   cases.json 形如： [[1, "要抽查的引文片段"], [2, "另一条"]]
CASES_FILE = (Path(sys.argv[1]) if len(sys.argv) > 1
              else Path(__file__).with_name("spot_check_cases.json"))
if not CASES_FILE.exists():
    sys.exit(f"缺少抽查用例文件：{CASES_FILE}\n"
             '请新建 JSON： [[1, "要抽查的引文片段"], [2, "另一条"]]\n'
             f"然后：python -B {Path(__file__).name} <cases.json>")
CASES = [tuple(x) for x in json.loads(CASES_FILE.read_text(encoding="utf-8"))]

for vol, q in CASES:
    d = C.load_ocr(vol)
    page, ratio, win, how, ctx = C.locate(d, q)
    printed = (page - C.OFFSETS[vol]) if page else None
    head = ""
    if page and page in d["pages"]:
        import re
        m = re.search(r"【页眉】([^【\n]{0,30})", d["pages"][page][0])
        head = m.group(1).strip() if m else ""
    print("=" * 100)
    print(f"第{vol}卷｜文件 {d['file']}")
    print(f"  引文：{q[:52]}")
    print(f"  定位：PDF/页序 {page} → 印刷第 {printed} 页｜相似度 {ratio:.2f}｜方式 {how}")
    print(f"  页眉：{head or '（无）'}")
    print(f"  页内上下文：{ctx[:96]}")
