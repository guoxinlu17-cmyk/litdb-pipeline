# -*- coding: utf-8 -*-
"""只读：查看指定 id 的文档内容片段（用于判断空壳/噪声条目）。"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import LazyPath, kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = LazyPath(kb_sqlite)
ids = [int(x) for x in sys.argv[1:]] or [198]

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
for doc_id in ids:
    row = conn.execute("SELECT id, path, title, year FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        print(f"id={doc_id} 不存在")
        continue
    chunks = conn.execute("SELECT id, LENGTH(text) n, substr(text,1,300) head FROM chunks WHERE doc_id = ? ORDER BY id", (doc_id,)).fetchall()
    print(f"\n=== id={row['id']} {row['title']} ({row['year']}) ===")
    print(f"路径：{row['path']}")
    print(f"块数：{len(chunks)}")
    for c in chunks[:3]:
        print(f"  [len={c['n']}] {c['head']!r}")
conn.close()
