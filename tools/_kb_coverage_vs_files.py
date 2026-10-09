# -*- coding: utf-8 -*-
"""内容覆盖盘点（只读）：文献目录里的文件 vs 知识库里的文档。

回答三个问题：
  ① 库里有没有"指向已删除文件"的悬空文档？
  ② 目录里有哪些文件没入库（哪些值得入）？
  ③ 库内文档分布在哪些目录、各类多少？

用法：python -B _kb_coverage_vs_files.py
"""
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import kb_sqlite, project_root  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = kb_sqlite()
ROOT = Path(project_root())
EXTS = {".pdf", ".md", ".docx", ".txt", ".epub", ".djvu"}

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

docs = [(r["id"], r["path"], r["title"], r["kind"]) for r in conn.execute("SELECT id, path, title, kind FROM docs")]
kb_paths = {str(Path(p).resolve()).lower(): (i, t) for i, p, t, _k in docs}

print("=" * 100)
print("一、库内文档的目录分布")
print("=" * 100)
by_dir = Counter()
for _i, p, _t, _k in docs:
    parts = Path(p).parts
    key = "\\".join(parts[-3:-1]) if len(parts) >= 3 else "\\".join(parts[:-1])
    by_dir[key] += 1
for k, v in by_dir.most_common(12):
    print(f"  {v:>4}  {k}")

print()
print("=" * 100)
print("二、悬空文档（库里有记录、磁盘上已无该文件）")
print("=" * 100)
dangling = [(i, p, t) for i, p, t, _k in docs if not Path(p).exists()]
print(f"  共 {len(dangling)} 个")
for i, p, t in dangling[:20]:
    print(f"    id={i:<4} {Path(p).name[:66]}")

print()
print("=" * 100)
print("三、目录里有、库里没有的文件")
print("=" * 100)
on_disk = []
for p in ROOT.rglob("*"):
    if p.is_file() and p.suffix.lower() in EXTS:
        if any(seg.startswith(".") for seg in p.parts):
            continue
        on_disk.append(p)
missing = [p for p in on_disk
           if str(p.resolve()).lower() not in kb_paths
           and not any(str(p.resolve()).lower() == k for k in kb_paths)]
print(f"  磁盘文件 {len(on_disk)} 个｜库内文档 {len(docs)} 个｜未入库 {len(missing)} 个")
bydir = defaultdict(list)
for p in missing:
    rel = p.relative_to(ROOT)
    bydir[str(rel.parent)].append(p)
for d, items in sorted(bydir.items(), key=lambda x: -len(x[1]))[:14]:
    tot = sum(i.stat().st_size for i in items) / 1024 / 1024
    print(f"    {len(items):>4} 个 / {tot:>7.1f} MB  {d}")
    for i in sorted(items, key=lambda x: -x.stat().st_size)[:3]:
        print(f"          · {i.name[:72]}（{i.stat().st_size/1024/1024:.1f} MB）")

print()
print("=" * 100)
print("四、按扩展名统计未入库文件")
print("=" * 100)
print("  ", dict(Counter(p.suffix.lower() for p in missing)))

print()
print("=" * 100)
print(f"五、库内 {len(docs)} 个文档的类型盘点")
print("=" * 100)
groups = defaultdict(list)
for i, p, t, k in docs:
    name = Path(p).name
    if "PAGES" in name and "全集" in name:
        groups["《示例文集》卷（页级）"].append((i, name))
    elif "全集" in name:
        groups["《示例文集》其他"].append((i, name))
    elif name.endswith(".md"):
        groups["Markdown（自建/登记表）"].append((i, name))
    else:
        groups["研究文献 PDF"].append((i, name))
for g, items in groups.items():
    print(f"\n  【{g}】{len(items)} 个")
    for i, n in sorted(items)[:40]:
        print(f"    id={i:<4} {n[:78]}")
conn.close()
