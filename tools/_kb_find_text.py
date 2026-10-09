# -*- coding: utf-8 -*-
"""查一句话是否在库里、落在哪个块上（只读），用于诊断"逐字引文检索不到"。

用法：python -B _kb_find_text.py "要查的句子"
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import LazyPath, kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = LazyPath(kb_sqlite)
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# 要查的句子**只从命令行给**。这里刻意不留默认值：
# 默认值会把你论文正在用的检索词写进源码，公开即等于公开选题。
PHRASES = sys.argv[1:]
if not PHRASES:
    sys.exit('用法：python -B _kb_find_text.py "要查的句子" ["更多句子" ...]')
DROP = set("…\u2026 \t\r\n\u3000，。、；：（）《》“”‘’\"'.,;:()[]【】·—–-！？!?／/｜|　")


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch not in DROP)


all_chunks = conn.execute("""
    SELECT c.id, c.doc_id, c.seq, c.section, c.weight, LENGTH(c.text) L, c.text, d.path
    FROM chunks c JOIN docs d ON d.id = c.doc_id""").fetchall()
print(f"库内块 {len(all_chunks):,}")

for ph in PHRASES:
    n = norm(ph)
    hits = []
    for c in all_chunks:
        if n and n in norm(c["text"]):
            hits.append(c)
    print("=" * 100)
    print(f"◆ 「{ph}」（归一后 {n}）→ 命中 {len(hits)} 个块")
    for c in hits[:6]:
        sec = c["section"] or ""
        vec = conn.execute("SELECT 1 FROM vecs WHERE chunk_id=?", (c["id"],)).fetchone()
        i = norm(c["text"]).find(n)
        ctx = norm(c["text"])[max(0, i - 40):i + len(n) + 40]
        print(f"   id={c['id']} doc={c['doc_id']} seq={c['seq']} section=「{sec[:22]}」 {c['L']}字"
              f" weight={c['weight']} 向量={'有' if vec else '无'}")
        print(f"      {Path(c['path']).name[:60]}")
        print(f"      …{ctx}…")
    if not hits:
        # 退一步：按片段找
        for frag in [n[i:i + 8] for i in range(0, max(1, len(n) - 8), 6)][:4]:
            f2 = [c for c in all_chunks if frag in norm(c["text"])]
            print(f"   片段「{frag}」命中 {len(f2)} 块"
                  + (f"｜例：section「{(f2[0]['section'] or '')[:18]}」 {Path(f2[0]['path']).name[:40]}" if f2 else ""))
conn.close()
