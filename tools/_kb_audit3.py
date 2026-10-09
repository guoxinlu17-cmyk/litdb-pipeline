# -*- coding: utf-8 -*-
"""知识库细部核查（只读）：weight 与向量缺口的关系、page_start 填充规律、重复文件、
DOI/元数据抽取正确性、块序号异常。

用法：python -B _kb_audit3.py
"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = kb_sqlite()
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
Q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

print("=" * 100)
print("一、weight=0 与「无向量」是不是同一批块")
print("=" * 100)
w0 = {r[0] for r in conn.execute("SELECT id FROM chunks WHERE weight = 0")}
nv = {r[0] for r in conn.execute("SELECT c.id FROM chunks c WHERE c.id NOT IN (SELECT chunk_id FROM vecs)")}
print(f"  weight=0 的块 {len(w0)}｜无向量的块 {len(nv)}｜交集 {len(w0 & nv)}")
print(f"  → {'完全重合，属设计（weight=0 不建向量索引）' if w0 == nv else '不重合，需细查'}")
print("\n  这些 weight=0 的块来自哪些文档、什么章节：")
for r in conn.execute("""
    SELECT d.path, c.section, COUNT(*) n FROM chunks c JOIN docs d ON d.id=c.doc_id
    WHERE c.weight = 0 GROUP BY d.id, c.section ORDER BY n DESC LIMIT 10"""):
    print(f"    {r['n']:>4}  「{(r['section'] or '')[:26]}」  {Path(r['path']).name[:52]}")

print()
print("=" * 100)
print("二、page_start 填充规律（按 kind / 扩展名）")
print("=" * 100)
for r in conn.execute("""
    SELECT d.kind, COUNT(DISTINCT d.id) nd,
           COUNT(c.id) nchunks,
           SUM(CASE WHEN c.page_start IS NOT NULL THEN 1 ELSE 0 END) wp
    FROM docs d JOIN chunks c ON c.doc_id=d.id GROUP BY d.kind"""):
    print(f"  kind={r['kind']:<6} 文档 {r['nd']:>3}｜块 {r['nchunks']:>6}｜有 page_start {r['wp']:>6}"
          f"（{r['wp']/max(1,r['nchunks']):.0%}）")
print("\n  有 page_start 的文档数 / 总数：",
      Q("SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE page_start IS NOT NULL"), "/",
      Q("SELECT COUNT(*) FROM docs"))
print("  这些文档扩展名：", Counter(
    Path(r["path"]).suffix for r in conn.execute(
        "SELECT DISTINCT d.path FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.page_start IS NOT NULL")).most_common())
print("  无 page_start 的文档示例（前 6，非全集）：")
for r in conn.execute("""SELECT d.path, COUNT(c.id) n FROM docs d JOIN chunks c ON c.doc_id=d.id
                         WHERE d.id NOT IN (SELECT DISTINCT doc_id FROM chunks WHERE page_start IS NOT NULL)
                         GROUP BY d.id ORDER BY n DESC LIMIT 6"""):
    print(f"    {r['n']:>4} 块  {Path(r['path']).name[:62]}")
print("\n  PDF 类文档里 page_start 的取值示例：")
for r in conn.execute("""SELECT c.section, c.page_start, c.page_end, LENGTH(c.text) L, d.path
                         FROM chunks c JOIN docs d ON d.id=c.doc_id
                         WHERE c.page_start IS NOT NULL ORDER BY c.id LIMIT 5"""):
    print(f"    page {r['page_start']}–{r['page_end']}  section「{(r['section'] or '')[:20]}」 {r['L']}字  "
          f"{Path(r['path']).name[:40]}")

print()
print("=" * 100)
print("三、重复文件核对（大小与 SHA）")
print("=" * 100)
for ids in ("169,170", "189,190", "205,206"):
    print(f"  ids={ids}")
    for r in conn.execute(f"SELECT id, path, size, substr(sha256,1,12) sha, chunk_count FROM docs WHERE id IN ({ids})"):
        print(f"    id={r['id']:<4} {r['size']:>10,} B  sha={r['sha']}  块{r['chunk_count']:<4} {Path(r['path']).name[:60]}")

print()
print("=" * 100)
print("四、元数据抽取正确性（抽查）")
print("=" * 100)
print("  ① 疑似把正文里的 DOI 当成文档 DOI：")
for r in conn.execute("""SELECT id, title, doi, path FROM docs
                         WHERE doi IS NOT NULL AND (title LIKE '#%' OR title LIKE '%文献%'
                                                    OR title LIKE '%说明%' OR title LIKE '%登记%')"""):
    print(f"    id={r['id']:<4} doi={r['doi'][:40]:<42} title={r['title'][:40]}")
print("\n  ② 标题是否像「从正文里抓的第一个标题」（以 # 开头者为可疑）：")
for r in conn.execute("SELECT id, title, path FROM docs WHERE title LIKE '#%' LIMIT 8"):
    print(f"    id={r['id']:<4} 「{r['title'][:56]}」 {Path(r['path']).name[:34]}")
print("\n  ③ authors 非空但格式异常的：")
for r in conn.execute("SELECT id, authors, path FROM docs WHERE authors IS NOT NULL AND TRIM(authors)<>'' LIMIT 8"):
    print(f"    id={r['id']:<4} 「{r['authors'][:60]}」 {Path(r['path']).name[:36]}")

print()
print("=" * 100)
print("五、块序号与文档计数一致性")
print("=" * 100)
bad = conn.execute("""
    SELECT d.id, d.chunk_count, COUNT(c.id) real FROM docs d LEFT JOIN chunks c ON c.doc_id=d.id
    GROUP BY d.id HAVING d.chunk_count <> COUNT(c.id)""").fetchall()
print(f"  chunk_count 与实际块数不一致的文档：{len(bad)}")
for r in bad[:6]:
    print(f"    id={r['id']} 记录 {r['chunk_count']}｜实际 {r['real']}")
seq0 = Q("SELECT COUNT(*) FROM chunks WHERE seq = 0")
print(f"  seq=0 的块：{seq0}（每篇文档第 1 块，正常）")
print(f"  seq 最大值：{Q('SELECT MAX(seq) FROM chunks')}")
print(f"  (doc_id, seq) 重复的组合：{Q('SELECT COUNT(*) FROM (SELECT doc_id, seq FROM chunks GROUP BY doc_id, seq HAVING COUNT(*)>1)')}")

print()
print("=" * 100)
print("六、para_start / para_end 填充情况")
print("=" * 100)
print("  有 para_start 的块：", Q("SELECT COUNT(*) FROM chunks WHERE para_start IS NOT NULL"))
print("  有 para_end 的块：", Q("SELECT COUNT(*) FROM chunks WHERE para_end IS NOT NULL"))
conn.close()
