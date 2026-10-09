# -*- coding: utf-8 -*-
"""知识库结构层核查（只读·修正版）：表结构、规模、元数据、分块、页码字段、重复、向量缺口。

要点：切块器把 Markdown 标题抽成了 ``chunks.section``（并从正文里去掉），而页码另有
``chunks.page_start/page_end`` 字段。因此「有没有页码」不能靠扫正文，要看这两个字段。

用法：python -B _kb_audit2.py
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import LazyPath, kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = LazyPath(kb_sqlite)
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

Q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

print("=" * 100)
print("四、分块与页码字段")
print("=" * 100)
print("  chunks 列：", [c[1] for c in conn.execute("PRAGMA table_info(chunks)")])
tot = Q("SELECT COUNT(*) FROM chunks")
ps = Q("SELECT COUNT(*) FROM chunks WHERE page_start IS NOT NULL")
pe = Q("SELECT COUNT(*) FROM chunks WHERE page_end IS NOT NULL")
print(f"  有 page_start 的块 {ps:,}/{tot:,}（{ps/tot:.0%}）｜有 page_end 的块 {pe:,}/{tot:,}")
psec = Q(r"""SELECT COUNT(*) FROM chunks
             WHERE section GLOB '第 * 页' OR section GLOB '第 [0-9]* 页'""")
page_like = Q(r"SELECT COUNT(*) FROM chunks WHERE section LIKE '第 % 页'")
print(f"  section 形如「第 N 页」的块 {page_like:,}（{page_like/tot:.0%}）")
print(f"  不同 page 章节数 {Q(chr(83)+'ELECT COUNT(DISTINCT section) FROM chunks WHERE section LIKE \'第 % 页\'')}")

print("\n  逐文档看页码覆盖（前 16 个，按块数）：")
rows = conn.execute("""
    SELECT d.id, d.path,
           COUNT(c.id) n,
           SUM(CASE WHEN c.page_start IS NOT NULL THEN 1 ELSE 0 END) wp,
           SUM(CASE WHEN c.section LIKE '第 % 页' THEN 1 ELSE 0 END) sp
    FROM docs d JOIN chunks c ON c.doc_id=d.id
    GROUP BY d.id ORDER BY n DESC LIMIT 16""").fetchall()
for r in rows:
    print(f"    {r['n']:>5} 块｜page字段 {r['wp']:>5}｜页章节 {r['sp']:>5}  {Path(r['path']).name[:60]}")

print("\n  权重字段 weight 分布：", dict(conn.execute(
    "SELECT weight, COUNT(*) FROM chunks GROUP BY weight ORDER BY 2 DESC LIMIT 6").fetchall()))

print()
print("=" * 100)
print("五、元数据完整度（修正：字段名 authors）")
print("=" * 100)
docs = Q("SELECT COUNT(*) FROM docs")
for f in ("title", "authors", "year", "journal", "doi", "zotero_key", "indexed_with"):
    empty = Q(f"SELECT COUNT(*) FROM docs WHERE {f} IS NULL OR TRIM(CAST({f} AS TEXT))=''")
    print(f"    {f:<12} 缺失 {empty:>4}/{docs}（{empty/docs:.0%}）")
print("\n  有 DOI 的文档：")
for r in conn.execute("SELECT id, doi, title FROM docs WHERE doi IS NOT NULL AND TRIM(doi)<>''"):
    print(f"    id={r['id']:<4} {r['doi'][:44]:<46} {(r['title'] or '')[:38]}")

print()
print("=" * 100)
print("六、重复")
print("=" * 100)
for r in conn.execute("""SELECT sha256, COUNT(*) n, GROUP_CONCAT(id) ids FROM docs
                         WHERE sha256 IS NOT NULL AND sha256<>''
                         GROUP BY sha256 HAVING n>1"""):
    print(f"  同 sha256：{r['n']} 份 ids={r['ids']}")
    for d in conn.execute(f"SELECT id, path, title, chunk_count, size FROM docs WHERE id IN ({r['ids']})"):
        print(f"    id={d['id']:<4} 块{d['chunk_count']:<5} {d['size']:>9,} B  {Path(d['path']).name[:62]}")
print("\n  标题高度相似的组（去空白后前 16 字相同）：")
seen = {}
for r in conn.execute("SELECT id, title, path FROM docs"):
    k = re.sub(r"\s+", "", (r["title"] or ""))[:16]
    seen.setdefault(k, []).append((r["id"], Path(r["path"]).name))
for k, v in seen.items():
    if len(v) > 1:
        print(f"    「{k}」×{len(v)}：{[i for i, _n in v]}  {[n[:40] for _i, n in v]}")

print()
print("=" * 100)
print("七、向量覆盖缺口")
print("=" * 100)
miss = conn.execute("""
    SELECT d.id, d.path, COUNT(c.id) n FROM docs d JOIN chunks c ON c.doc_id=d.id
    WHERE c.id NOT IN (SELECT chunk_id FROM vecs) GROUP BY d.id ORDER BY n DESC""").fetchall()
print(f"  缺向量的文档 {len(miss)} 个，合计 {sum(r['n'] for r in miss)} 块"
      f"（占全部块 {sum(r['n'] for r in miss)/tot:.1%}）")
d = conn.execute("SELECT chunk_id, length(vec) FROM vecs LIMIT 1").fetchone()
print(f"  向量存储：chunk_id={d[0]}｜length(vec)={d[1]} 字节（512 维 float32 = 2048 字节，说明是打包存储）")

print()
print("=" * 100)
print("八、抽查：page_start/page_end 是否真的填了页码")
print("=" * 100)
for r in conn.execute("""SELECT c.id, c.seq, c.section, c.page_start, c.page_end, LENGTH(c.text) L, d.path
                         FROM chunks c JOIN docs d ON d.id=c.doc_id
                         WHERE d.path LIKE '%第5卷%PAGES%' ORDER BY c.seq LIMIT 5"""):
    print(f"  块{r['id']} seq={r['seq']} section=「{r['section']}」 page={r['page_start']}–{r['page_end']} {r['L']}字")
print("  ……")
for r in conn.execute("""SELECT c.id, c.seq, c.section, c.page_start, c.page_end, LENGTH(c.text) L, d.path
                         FROM chunks c JOIN docs d ON d.id=c.doc_id
                         WHERE d.path LIKE '%第4卷%PAGES%' ORDER BY c.seq LIMIT 5"""):
    print(f"  块{r['id']} seq={r['seq']} section=「{r['section']}」 page={r['page_start']}–{r['page_end']} {r['L']}字")

print()
print("=" * 100)
print("九、cache 表（检索缓存）")
print("=" * 100)
for r in conn.execute("SELECT key, LENGTH(payload) L, created FROM cache LIMIT 5"):
    print(f"  {r['key'][:40]:<42} {r['L']:>7} B  {r['created']}")
conn.close()
