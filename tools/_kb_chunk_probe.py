# -*- coding: utf-8 -*-
"""只读：检查某文档的分块文本长度分布（判断是否属于无文本层扫描件）。"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from _paths import LazyPath, kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = LazyPath(kb_sqlite)
TARGET = "探索主体性"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

row = conn.execute("SELECT id, path FROM docs WHERE path LIKE ?", (f"%{TARGET}%",)).fetchone()
if row is None:
    print("未找到该文档")
    raise SystemExit(0)

chunks = conn.execute(
    "SELECT id, length(coalesce(text,'')) AS n, substr(coalesce(text,''), 1, 40) AS head "
    "FROM chunks WHERE doc_id = ? ORDER BY id", (row["id"],)).fetchall()

lengths = [c["n"] for c in chunks]
print(f"doc id={row['id']}｜分块数 {len(chunks)}")
if lengths:
    print(f"文本长度：最小 {min(lengths)}｜最大 {max(lengths)}｜平均 {sum(lengths)/len(lengths):.0f}｜"
          f"长度=0 的分块 {sum(1 for n in lengths if n == 0)} 个")
print("前 5 个分块预览：")
for c in chunks[:5]:
    print(f"  len={c['n']:>5}  {c['head']!r}")

vecs = conn.execute(
    "SELECT COUNT(*) FROM vecs WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id = ?)",
    (row["id"],)).fetchone()[0]
print(f"该文档的向量数：{vecs}")
conn.close()
