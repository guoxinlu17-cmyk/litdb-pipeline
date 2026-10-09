# -*- coding: utf-8 -*-
"""05_文献数据库 全量清点 + 构建时间线（只读）。

输出：每个文件的角色归类、规模、修改时间；按时间重排以还原构建顺序；
      子目录内容概览；被引用但已不存在的路径（悬空引用）。

用法：python -B _litdb_analyze.py
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# 资料库目录**不写死**：交给同目录的 _paths.py 解析
# （顺序：命令行参数 → 环境变量 → 项目布局.json → 结构探测 → 报错，绝不猜）。
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 被 import 时也能找到 _paths
from _paths import LazyPath, db_dir  # noqa: E402

D = LazyPath(lambda: db_dir(strict=False))
if D is None:
    raise SystemExit(
        "解析不出「05_文献数据库」目录。请用环境变量 LITDB_DBDIR 或 LITDB_PROJECT 指定，"
        "或在项目根放 项目布局.json 声明 db 目录。")


def role(name: str, sub: str) -> str:
    if sub == "scripts":
        return "⑦ 脚本"
    if sub in ("extract", "fulltext", "fulltext_analysis", "analysis"):
        return "⑧ 中间产物"
    if sub == "snapshots":
        return "⑨ 快照"
    if sub == "index":
        return "③ 语义索引 B"
    if name.startswith("semantic_index"):
        return "③ 语义索引 A"
    if name.startswith("literature_db"):
        return "① 题录库"
    if name.startswith("refs.bib") or name.startswith("auto_meta"):
        return "② 题录衍生/基线"
    if name in ("引文台账.csv", "引文台账.md"):
        return "④ 引文台账"
    if name.startswith("二级文献"):
        return "⑤ 二级文献库"
    if name.startswith(("引文页码候选", "引文校勘清单", "无扫描卷页码候选", "引文页码复核报告")):
        return "⑥ 引文定位（本轮）"
    if name.endswith(".md"):
        return "⑩ 报告/衍生成果"
    if name.endswith((".json", ".csv", ".tsv", ".xlsx", ".npz", ".txt")):
        return "⑪ 其他数据"
    return "⑫ 未分类"


print("=" * 104)
print("一、顶层文件清单（按角色）")
print("=" * 104)
files = []
for p in sorted(D.iterdir()):
    if p.is_file():
        files.append((p, "", p.stat().st_size, p.stat().st_mtime))
for sub in sorted(x for x in D.iterdir() if x.is_dir()):
    for p in sorted(sub.rglob("*")):
        if p.is_file():
            files.append((p, sub.name, p.stat().st_size, p.stat().st_mtime))

by_role = defaultdict(list)
for p, sub, size, mt in files:
    by_role[role(p.name, sub)].append((p, sub, size, mt))

total_size = sum(s for _p, _s, s, _m in files)
print(f"  文件合计 {len(files)} 个｜{total_size/1024/1024:.1f} MB")
for r in sorted(by_role):
    items = by_role[r]
    sz = sum(s for _p, _s, s, _m in items)
    print(f"\n  【{r}】{len(items)} 个 / {sz/1024/1024:.1f} MB")
    for p, sub, size, mt in sorted(items, key=lambda x: -x[2])[:14]:
        rel = f"{sub}/{p.name}" if sub else p.name
        try:
            lines = sum(1 for _ in p.open("rb"))
        except Exception:                                          # noqa: BLE001
            lines = 0
        print(f"     {size/1024:>9.1f} KB {lines:>7} 行  {rel[:74]}")
    if len(items) > 14:
        print(f"     …（另有 {len(items)-14} 个）")

print()
print("=" * 104)
print("二、构建时间线（按修改时间；只列顶层与关键子目录）")
print("=" * 104)
key = [(p, sub, size, mt) for p, sub, size, mt in files
       if sub in ("", "index", "scripts", "fulltext_analysis", "analysis")]
for p, sub, size, mt in sorted(key, key=lambda x: x[3]):
    t = datetime.fromtimestamp(mt).strftime("%m-%d %H:%M")
    rel = f"{sub}/{p.name}" if sub else p.name
    print(f"  {t}  {size/1024:>8.1f} KB  {rel[:78]}")

print()
print("=" * 104)
print("三、子目录内容")
print("=" * 104)
for sub in sorted(x for x in D.iterdir() if x.is_dir()):
    items = [f for f in sub.rglob("*") if f.is_file()]
    print(f"\n  ● {sub.name}/：{len(items)} 个文件 / {sum(f.stat().st_size for f in items)/1024/1024:.1f} MB")
    for f in sorted(items)[:10]:
        print(f"      {f.stat().st_size/1024:>8.1f} KB  {f.name[:80]}")
    if len(items) > 10:
        print(f"      …（另有 {len(items)-10} 个）")

print()
print("=" * 104)
print("四、悬空引用（文件里提到、磁盘上已不存在的路径）")
print("=" * 104)
pat = re.compile(r"[A-Za-z]:\\\\[^\"'\n\r,;\]\}]{6,150}")
json_paths = Counter()
for p, _sub, _s, _m in files:
    if p.suffix.lower() not in (".json", ".md", ".csv", ".tsv"):
        continue
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:                                              # noqa: BLE001
        continue
    for m in pat.finditer(txt):
        raw = m.group(0).replace("\\\\", "\\")
        json_paths[(str(p.relative_to(D)), raw)] += 1
sample = defaultdict(set)
for (src, raw), n in json_paths.items():
    sample[src].add(raw)
dangling = Counter()
exists = Counter()
for src, raws in sample.items():
    for raw in raws:
        if re.search(r"\.(pdf|docx|md|txt|json|npz|csv|xlsx|bib)$", raw, re.I):
            (exists if Path(raw).exists() else dangling)[src] += 1
print(f"  含路径引用的文件 {len(sample)} 个")
for src in sorted(sample, key=lambda s: -(dangling[s] + exists[s]))[:14]:
    print(f"    {src[:52]:<54} 存在的 {exists[src]:>4}｜已失效 {dangling[src]:>4}")
