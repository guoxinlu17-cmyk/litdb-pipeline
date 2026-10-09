# -*- coding: utf-8 -*-
"""汇总本轮（数据库生成与核查）产生的全部工具：路径、用途、命令行、规模。

用途：为《数据库生成经验总结》提供**从磁盘实读**的工具清单，不凭记忆。
"""
import re
import sys
from datetime import datetime
from pathlib import Path
from _paths import toolkit_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

T = Path(toolkit_dir())
SINCE = datetime(2026, 9, 13, 0, 0)


def one_line(p: Path) -> str:
    txt = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^\s*"""(.*?)"""', txt, re.S | re.M)
    if not m:
        m = re.search(r'^\s*"""((?:.|\n)*?)$', txt, re.S | re.M)
    body = (m.group(1) if m else "").strip()
    for line in body.split("\n"):
        line = line.strip()
        if line and not line.startswith(("===", "---")):
            return line
    return ""


def usage(p: Path) -> str:
    txt = p.read_text(encoding="utf-8", errors="replace")
    cmds = re.findall(r"python -B \S+[^\n]*", txt)
    return cmds[0][:76] if cmds else ""


files = sorted([p for p in T.glob("*.py") if p.stat().st_mtime >= SINCE.timestamp()],
               key=lambda p: p.stat().st_mtime)
print(f"本工作期新增/修改的脚本 {len(files)} 个\n")
print(f"{'脚本':<28}{'行数':>5} 用途")
print("-" * 118)
for p in files:
    n = len(p.read_text(encoding='utf-8', errors='replace').splitlines())
    print(f"{p.name:<28}{n:>5} {one_line(p)[:78]}")

print("\n\n附：早期（9-13 之前）既有脚本，仍在使用")
print("-" * 118)
old = sorted([p for p in T.glob("*.py") if p.stat().st_mtime < SINCE.timestamp()])
for p in old:
    n = len(p.read_text(encoding='utf-8', errors='replace').splitlines())
    print(f"{p.name:<28}{n:>5} {one_line(p)[:78]}")
print(f"\n合计：本轮 {len(files)} 个 + 既有 {len(old)} 个 = {len(files)+len(old)} 个脚本")
