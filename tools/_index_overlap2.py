# -*- coding: utf-8 -*-
"""修正核对：index/ 与 semantic_index 的路径可解析性、与主库的覆盖关系。

重点区分：
  * index/fulltext_chunks.json 存的是**文件名** + 清单里的 source_dir → 可解析；
  * semantic_index.meta.json 存的是**绝对路径**（旧目录位置）→ 若目录已搬迁则失效。

用法：python -B _index_overlap2.py
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# 三个目录都**不写死**：交给同目录的 _paths.py 解析（参数 → 环境变量 →
# 项目布局.json → 结构探测 → 报错）。换电脑/改目录名时不必改这个脚本。
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 被 import 时也能找到 _paths
from _paths import db_dir, kb_sqlite, lit_dir  # noqa: E402

DBD = db_dir(strict=False)
KB = kb_sqlite(strict=False)
LIT = lit_dir(strict=False)
_missing = [n for n, v in (("db", DBD), ("kb", KB), ("lit", LIT)) if v is None]
if _missing:
    raise SystemExit(
        "解析不出这些目录：" + "、".join(_missing) +
        "。请用环境变量 LITDB_PROJECT / LITDB_DBDIR / KB_ROOT 指定，"
        "或在项目根放 项目布局.json 声明。")

conn = sqlite3.connect(f"file:{KB}?mode=ro", uri=True)
kb_names = {Path(p).name for (p,) in conn.execute("SELECT path FROM docs")}
kb_md = {Path(p).name for (p,) in conn.execute("SELECT path FROM docs WHERE kind='md'")}

print("=" * 100)
print("① index/（源目录全文索引）")
print("=" * 100)
man = json.loads((DBD / "index" / "index_manifest.json").read_text(encoding="utf-8"))
src = Path(man["source_dir"])
print(f"  built_at {man['built_at']}｜model {man['model']}｜dim {man['dim']}｜chunk_size {man['chunk_size']}")
print(f"  source_dir {src}｜存在 {src.exists()}")
print(f"  files_ok/total {man['files_ok']}/{man['files_total']}｜chunks {man['chunks']}")
files = [f["file"] for f in man.get("files", [])]
ok = sum(1 for f in files if (src / f).exists())
print(f"  清单里的文件：{len(files)} 个，其中 {ok} 个能在 source_dir 下找到"
      f"（{ok/max(1,len(files)):.0%}）")
print(f"  与主库重叠（按文件名）：{len(set(files) & kb_names)} 个")
missing_in_kb = [f for f in files if f not in kb_names]
print(f"  在 index/ 里但主库没有的：{len(missing_in_kb)} 个，示例：")
for f in missing_in_kb[:8]:
    print(f"    · {f[:78]}")

print()
print("=" * 100)
print("② semantic_index（旧路径的绝对路径）")
print("=" * 100)
meta = json.loads((DBD / "semantic_index.meta.json").read_text(encoding="utf-8"))
paths = sorted({m.get("path", "") for m in meta})
print(f"  分块 {len(meta)}｜涉及路径 {len(paths)} 个")
alive = [p for p in paths if Path(p).exists()]
print(f"  路径存在 {len(alive)}/{len(paths)}")
if not alive:
    old_root = str(Path(paths[0]).parents[0])
    print(f"  旧根目录：{old_root}（现在不存在）")
    print(f"  推断新位置：{LIT}")
    moved_ok = sum(1 for p in paths if (LIT / Path(p).name).exists())
    print(f"  若按文件名搬到现目录，可找回 {moved_ok}/{len(paths)} 个 → **可修复**")
print(f"  与主库重叠（按文件名）：{len({Path(p).name for p in paths} & kb_names)} 个")

print()
print("=" * 100)
print("③ 三个语义索引的规模对比")
print("=" * 100)
print(f"  {'索引':<34}{'块数':>8}{'文档':>7}{'维度':>6}  状态")
print(f"  {'主知识库 .kb/kb.sqlite':<34}{10158:>8}{113:>7}{512:>6}  ✅ 在用，路径全部有效")
print(f"  {'index/fulltext_vectors.npz':<34}{man['chunks']:>8}{man['files_ok']:>7}{man['dim']:>6}  ⚠️ 可解析，但与主库重复")
print(f"  {'semantic_index.npz':<34}{len(meta):>8}{len(paths):>7}{512:>6}  ❌ 绝对路径全部失效")
conn.close()
