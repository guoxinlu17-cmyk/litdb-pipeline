# -*- coding: utf-8 -*-
"""核对若干已定页引文的**语境与页眉年份**，看论文的年份/文本标注是否站得住。

用法：python -B _verify_cite_context.py
"""
import io
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import lit_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
LIT = Path(lit_dir())
CORPUS = os.environ.get("LITDB_CORPUS", "文集")   # 文集名，如「某某全集」

# 用例从**外部 JSON** 读：(卷, 印刷页, 论文里的标注, 引文片段)
# 仓库不内置——「卷+页+论文标注+逐字引文」四元组就是论文注释本身。
#   cases.json 形如： [[1, 100, "第1卷，某年文本", "引文片段"], ...]
CASES_FILE = (Path(sys.argv[1]) if len(sys.argv) > 1
              else Path(__file__).with_name("cite_context_cases.json"))
if not CASES_FILE.exists():
    sys.exit(f"缺少用例文件：{CASES_FILE}\n"
             '请新建 JSON： [[1, 100, "第1卷，某年文本", "引文片段"]]\n'
             f"然后：python -B {Path(__file__).name} <cases.json>")
CASES = [tuple(x) for x in json.loads(CASES_FILE.read_text(encoding="utf-8"))]


def load(vol: int, pages: list[int]) -> dict[int, str]:
    p = LIT / f"{CORPUS} 第{vol}卷.PAGES.md"
    text = p.read_text(encoding="utf-8", errors="replace")
    marks = [(int(m.group(1)), m.start(), m.end())
             for m in re.finditer(r"^#### 第 (\d+) 页\s*$", text, re.M)]
    out: dict[int, str] = {}
    for k, (num, s, e) in enumerate(marks):
        if num in pages:
            end = marks[k + 1][1] if k + 1 < len(marks) else len(text)
            out.setdefault(num, "")
            out[num] += re.sub(r"\s+", "", text[e:end])
    return out


cache: dict[int, dict[int, str]] = {}
for vol, page, label, frag in CASES:
    if vol not in cache:
        cache[vol] = load(vol, [c[1] for c in CASES if c[0] == vol])
    body = cache[vol].get(page, "")
    print("=" * 100)
    print(f"第{vol}卷 第{page}页｜论文标注：{label}")
    head = re.search(r"【页眉】([^【\n]{0,40})", body)
    print(f"  该页页眉：{head.group(1) if head else '（无）'}")
    i = body.find(re.sub(r"[\s，。、；：,.]", "", frag)[:12])
    print(f"  引文所在上下文：…{body[max(0, i-70):i+90] if i >= 0 else '（本页未找到该引文片段）'}…")
    print()
