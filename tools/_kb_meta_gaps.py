# -*- coding: utf-8 -*-
"""元数据可修复面与遗留问题核查（只读）：
  · 能从文件名里认出作者的有多少（authors 字段因此可批量补）
  · year 字段的可靠性（哪些文档的年份明显是抓错的）
  · 某专题的文档是否已清干净（专题名用 --topic 给）
  · 明确未入库的研究文献

用法：python -B _kb_meta_gaps.py
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import LazyPath, kb_sqlite, project_root  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = LazyPath(kb_sqlite)
ROOT = LazyPath(project_root)
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

docs = list(conn.execute("SELECT id, path, title, authors, year, journal, doi, kind FROM docs"))
print(f"库内文档 {len(docs)}")

print()
print("=" * 100)
print("一、作者名能否从文件名认出来")
print("=" * 100)
# 常见写法：以 _作者.pdf 结尾，或 ——…_作者.pdf
pat = re.compile(r"[_—\-]([\u4e00-\u9fff]{2,4})\.(pdf|docx|md)$")
name_authors = Counter()
hit = []
for r in docs:
    m = pat.search(Path(r["path"]).name)
    if m:
        name_authors[m.group(1)] += 1
        hit.append((r["id"], m.group(1), Path(r["path"]).name))
print(f"  文件名里带「_姓名」的文档：{len(hit)}/{len(docs)}")
print(f"  识别出的作者（前 20）：{name_authors.most_common(20)}")
print(f"  其中 authors 字段当前为空或错误的："
      f"{sum(1 for i, a, _n in hit if not (conn.execute('SELECT authors FROM docs WHERE id=?', (i,)).fetchone()[0] or '').strip() == a)}")

print()
print("=" * 100)
print("二、year 字段可靠性")
print("=" * 100)
years = Counter(r["year"] for r in docs)
print("  年份分布前 12：", years.most_common(12))
print("\n  年份 ≥ 2025 的文档（多为抓错或入库年）：")
for r in docs:
    if r["year"] and str(r["year"]) >= "2025":
        print(f"    id={r['id']:<4} year={r['year']}  {Path(r['path']).name[:66]}")
print("\n  年份为空或异常低的：")
for r in docs:
    y = str(r["year"] or "")
    if not y or (y.isdigit() and int(y) < 1930):
        print(f"    id={r['id']:<4} year={r['year']}  {Path(r['path']).name[:66]}")

print()
print("=" * 100)
TOPIC = os.environ.get("LITDB_TOPIC", "")
print(f"三、专题文档（LITDB_TOPIC={TOPIC or '（未设，跳过）'}）")
print("=" * 100)
east = [r for r in docs if TOPIC and (TOPIC in Path(r["path"]).name or TOPIC in (r["title"] or ""))]
print(f"  命中 {len(east)} 个")
for r in east[:10]:
    print(f"    id={r['id']} {Path(r['path']).name[:70]}")
on_disk = list(ROOT.rglob(f"*{TOPIC}*")) if TOPIC else []
print(f"  磁盘上仍有 {len(on_disk)} 个含「{TOPIC}」的文件：")
for p in on_disk[:8]:
    print(f"    {p.stat().st_size/1024/1024:>6.1f} MB  {str(p.relative_to(ROOT))[:80]}")

print()
print("=" * 100)
print("四、02_文献 全量清单（哪些研究文献进了库 / 没进）")
print("=" * 100)
lit = ROOT / os.environ.get("LITDB_TOPIC_DIR", "专题") / "01_文献"
kb = {str(Path(r["path"]).resolve()).lower() for r in docs}
papers = sorted(p for p in lit.glob("*.pdf"))
in_kb = [p for p in papers if str(p.resolve()).lower() in kb]
out_kb = [p for p in papers if str(p.resolve()).lower() not in kb]
print(f"  01_文献 下 PDF {len(papers)} 个｜已入库 {len(in_kb)}｜未入库 {len(out_kb)}")
for p in out_kb:
    kind = "全集（已用 PAGES.md 取代）" if "全集" in p.name else "★ 研究文献，未入库"
    print(f"    {p.stat().st_size/1024/1024:>6.1f} MB  {kind}  {p.name[:64]}")
conn.close()
