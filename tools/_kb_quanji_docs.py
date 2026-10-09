# -*- coding: utf-8 -*-
"""列出知识库里「某文集」相关文档，并对比同名卷的不同副本（只读）。

文集名与探针词都从环境变量给，源码里不留项目专名：
    LITDB_CORPUS   文集名关键字，用于筛选 docs.path（默认「全集」）
    LITDB_PROBES   额外探针词，逗号分隔，用于在各卷正文里数出现次数
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import kb_sqlite  # 同目录共享模块
DB = kb_sqlite()
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

CORPUS = os.environ.get("LITDB_CORPUS", "全集")
_PROBES = [s for s in os.environ.get("LITDB_PROBES", "").split(",") if s.strip()]

rows = conn.execute("""
    SELECT d.id, d.path, substr(coalesce(d.sha256,''),1,10) AS sha, d.kind, d.year, d.title,
           (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS chunks,
           (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id
              AND c.id IN (SELECT chunk_id FROM vecs)) AS vecs
    FROM docs d
    WHERE d.path LIKE ?
    ORDER BY d.path
""", (f"%{CORPUS}%",)).fetchall()

print(f"== 库内「全集」相关文档 {len(rows)} 个 ==")
for r in rows:
    name = Path(r["path"]).name
    print(f"  id={r['id']:>3} chunks={r['chunks']:>4} vecs={r['vecs']:>4} sha={r['sha']} "
          f"kind={r['kind'] or '-':<5} {name}")

# 对比各卷「同一卷的不同副本」
print("\n== 同名卷多副本对比 ==")
import re
buckets: dict[str, list[sqlite3.Row]] = {}
for r in rows:
    name = Path(r["path"]).name
    m = (re.search(r"全集[^\d]{0,4}(\d)\s*卷", name)
         or re.search(r"全集\s*(\d)\s*[(（]", name)
         or re.search(r"第\s*([2-8])\s*卷", name))
    if m:
        buckets.setdefault(m.group(1), []).append(r)
for vol, items in sorted(buckets.items()):
    print(f"  第{vol}卷：{len(items)} 个副本｜" + "；".join(
        f"id={it['id']}(chunks={it['chunks']})" for it in items))

# 逐卷对比：页码标记 / 文本特征 / 是否同一版本
print("\n== 各副本内容对比（OCR 噪声特征 + 页码标记）==")
cache: dict[int, str] = {}
for vol in sorted(buckets):
    items = buckets[vol]
    if len(items) < 2:
        continue
    print(f"\n第{vol}卷：")
    for it in items:
        cks = conn.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY id", (it["id"],)).fetchall()
        text = "\n".join(c["text"] for c in cks if c["text"])
        cache[it["id"]] = text
        marks = len(re.findall(r"---\s*第\s*\d+\s*页\s*---", text))
        print(f"  id={it['id']}｜{Path(it['path']).name}")
        print(f"     文本 {len(text):,} 字符｜页码标记 {marks} 个｜片段 {len(cks)} 个")
        # 探针词从环境变量给（逗号分隔），如 LITDB_PROBES="某某全集,某某卷目"
        # 不给则跳过。篇名/卷目属于项目数据，不进源码。
        for probe in _PROBES:
            print(f"       「{probe}」×{text.count(probe)}")
    # 覆盖度：A 的 40 字滑窗有多少能在 B 里找到
    if len(items) == 2:
        a, b = items[0]["id"], items[1]["id"]
        ta, tb = cache[a], cache[b]
        sh = [ta[i:i + 40] for i in range(0, max(1, len(ta) - 40), 400)]
        hit = sum(1 for s in sh if s in tb)
        print(f"  覆盖度：id={a} 抽 {len(sh)} 段 40 字样本，有 {hit} 段能在 id={b} 中逐字找到"
              f"（{hit / max(1, len(sh)):.1%}）")
        sh2 = [tb[i:i + 40] for i in range(0, max(1, len(tb) - 40), 400)]
        hit2 = sum(1 for s in sh2 if s in ta)
        print(f"  反向：id={b} 抽 {len(sh2)} 段，有 {hit2} 段能在 id={a} 中逐字找到"
              f"（{hit2 / max(1, len(sh2)):.1%}）")
conn.close()
