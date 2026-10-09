# -*- coding: utf-8 -*-
"""只读检查 kb.sqlite 的重复结构，为安全去重提供依据（不写任何数据）。"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from _paths import kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = kb_sqlite()
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def scalar(sql, *args):
    return conn.execute(sql, args).fetchone()[0]


print("== 总量 ==")
print("docs   :", scalar("SELECT COUNT(*) FROM docs"))
print("chunks :", scalar("SELECT COUNT(*) FROM chunks"))
print("vecs   :", scalar("SELECT COUNT(*) FROM vecs"))

print("\n== docs 属性 ==")
print("有 sha256 :", scalar("SELECT COUNT(*) FROM docs WHERE sha256 IS NOT NULL AND sha256 <> ''"))
print("有向量的文档 :", scalar("SELECT COUNT(DISTINCT c.doc_id) FROM chunks c WHERE c.id IN (SELECT chunk_id FROM vecs)"))
print("无向量的文档 :", scalar(
    "SELECT COUNT(*) FROM docs d WHERE d.id NOT IN "
    "(SELECT DISTINCT c.doc_id FROM chunks c WHERE c.id IN (SELECT chunk_id FROM vecs))"))
print("id 范围 :", scalar("SELECT MIN(id) FROM docs"), "-", scalar("SELECT MAX(id) FROM docs"))

print("\n== 重复分组（按 path 文件名）==")
rows = conn.execute("""
    SELECT d.id, d.path, substr(coalesce(d.sha256,''),1,8) AS sha,
           (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks,
           (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id AND c.id IN (SELECT chunk_id FROM vecs)) AS vecs
    FROM docs d ORDER BY d.path, d.id
""").fetchall()
from collections import defaultdict
groups = defaultdict(list)
for r in rows:
    key = (r["path"] or "").split("\\")[-1]
    groups[key].append(r)

dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
print(f"重复文件名的组数: {len(dup_groups)}；全部组：")
for key, items in dup_groups.items():
    print(f"  * {key}")
    for it in items:
        print(f"      id={it['id']} sha={it['sha']} chunks={it['chunks']} vecs={it['vecs']}")

print("\n== 无向量的文档 ==")
for it in rows:
    if it["vecs"] == 0:
        print(f"  id={it['id']} sha={it['sha']} chunks={it['chunks']}  {(it['path'] or '').split(chr(92))[-1]}")

print("\n== 唯一文件名的组（可能是旧库独有）==")
solo = [k for k, v in groups.items() if len(v) == 1]
print(f"单例组数: {len(solo)}；示例：{solo[:5]}")

print("\n== 同名组内 sha 是否一致 ==")
same_sha = 0
diff_sha = 0
for key, items in dup_groups.items():
    shas = {it["sha"] for it in items}
    if len(shas) == 1:
        same_sha += 1
    else:
        diff_sha += 1
print(f"同 sha: {same_sha} 组｜不同 sha: {diff_sha} 组")

print("\n== 判定用汇总 ==")
print("可安全删除的对象数（同名组内无向量的旧副本）:",
      sum(1 for items in dup_groups.values() for it in items if it["vecs"] == 0 and any(x["vecs"] > 0 for x in items)))
conn.close()
