# -*- coding: utf-8 -*-
"""按 id 删除单个文档（docs + chunks + vecs + cache），用于清理残留的旧副本。

用法：python -B _kb_delete_doc.py <id> [<id> ...] [--apply]
默认 dry-run。
"""
import argparse
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from _paths import LazyPath, kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = LazyPath(kb_sqlite)
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    for doc_id in a.ids:
        row = conn.execute("SELECT id, path FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            print(f"id={doc_id} 不存在，跳过")
            continue
        chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()[0]
        vecs = conn.execute(
            "SELECT COUNT(*) FROM vecs WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id = ?)",
            (doc_id,)).fetchone()[0]
        name = (row["path"] or "").split("\\")[-1]
        print(f"id={doc_id} chunks={chunks} vecs={vecs}  {name}")
        if a.apply:
            conn.execute("DELETE FROM vecs WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))

    if a.apply:
        conn.execute("DELETE FROM cache")
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
        n_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_vecs = conn.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
        print(f"完成 → docs={n_docs} chunks={n_chunks} vecs={n_vecs}")
    else:
        print("（dry-run，未改动；加 --apply 执行）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
