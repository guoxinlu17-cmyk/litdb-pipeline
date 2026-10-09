# -*- coding: utf-8 -*-
"""核查 05_文献数据库 下各套并行成果的规模、字段与相互关系（只读）。

对象：literature_db v1–v4、semantic_index、二级文献数据库、引文台账、refs.bib、衍生 md。
目的：看清"到底有几个数据库、各自管什么、有没有重叠与过期"。

用法：python -B _db_family_audit.py
"""
import csv
import io
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import LazyPath, db_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DBD = LazyPath(db_dir)
def head(path: Path, n: int = 120) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:n].replace("\n", " ⏎ ")


print("=" * 100)
print("一、README（用户自建说明）")
print("=" * 100)
rp = DBD / "README.md"
print(rp.read_text(encoding="utf-8", errors="replace")[:2600] if rp.exists() else "（无）")

print()
print("=" * 100)
print("二、literature_db 各版本")
print("=" * 100)
for name in ("literature_db.json", "literature_db_v2.json", "literature_db_v3.json", "literature_db_v4.json"):
    p = DBD / name
    if not p.exists():
        continue
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                        # noqa: BLE001
        print(f"  {name}: 解析失败 {e}")
        continue
    if isinstance(d, dict):
        keys = list(d.keys())
        print(f"  {name}: dict，顶层键 {keys[:8]}")
        for k in keys[:4]:
            v = d[k]
            if isinstance(v, list):
                print(f"       {k}: {len(v)} 条；样例字段 {list(v[0].keys())[:12] if v and isinstance(v[0], dict) else v[:2]}")
            else:
                print(f"       {k}: {str(v)[:80]}")
    else:
        print(f"  {name}: list，{len(d)} 条；字段 {list(d[0].keys())[:14] if d and isinstance(d[0], dict) else '?'}")

print()
print("=" * 100)
print("三、semantic_index（独立语义索引？）")
print("=" * 100)
for name in ("semantic_index.meta.json", "embed_dim.txt", "manifest.json", "priority_map.json", "fulltext_manifest.json"):
    p = DBD / name
    if not p.exists():
        continue
    print(f"  ● {name}（{p.stat().st_size/1024:.0f} KB）")
    print(f"     开头：{head(p)}")
    if name.endswith(".json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for k, v in list(d.items())[:6]:
                    print(f"     {k}: {len(v) if isinstance(v, (list, dict)) else v}"
                          f"{'｜样例 '+str(v[0])[:90] if isinstance(v, list) and v else ''}")
            elif isinstance(d, list):
                print(f"     list {len(d)} 条；样例 {str(d[0])[:120]}")
        except Exception as e:                                    # noqa: BLE001
            print(f"     解析失败：{e}")
npz = DBD / "semantic_index.npz"
if npz.exists():
    print(f"  ● semantic_index.npz（{npz.stat().st_size/1024/1024:.1f} MB）— numpy 打包数组")

print()
print("=" * 100)
print("四、二级文献数据库 / 引文台账 / refs.bib")
print("=" * 100)
for name in ("二级文献数据库.json", "refs.bib"):
    p = DBD / name
    if not p.exists():
        continue
    if name.endswith(".bib"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        print(f"  {name}: {txt.count('@')} 个条目｜{len(txt):,} 字符")
        print(f"     首条：{txt[:220]}".replace('\n', ' ⏎ '))
    else:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            for k, v in list(d.items())[:5]:
                print(f"  {name}｜{k}: {len(v) if isinstance(v,(list,dict)) else v}")
        else:
            print(f"  {name}: {len(d)} 条；字段 {list(d[0].keys()) if d else '?'}")
            print(f"     样例：{json.dumps(d[0], ensure_ascii=False)[:300] if d else ''}")

# 不写死文件名：台账类清单随项目而变，写死等于把某次作业的产物名固化进源码。
for p in sorted(DBD.glob("*.csv")):
    rows = list(csv.DictReader(io.open(p, encoding="utf-8-sig")))
    print(f"  {p.name}: {len(rows)} 行；字段 {list(rows[0].keys())[:10] if rows else '?'}")

print()
print("=" * 100)
print("五、衍生成果 md（自建分析产物）")
print("=" * 100)
# 同上：不写死产物名，目录下全部 .md 视为自建衍生成果。
for p in sorted(DBD.glob("*.md")):
    t = p.read_text(encoding="utf-8", errors="replace")
    print(f"  {p.name:<24} {p.stat().st_size/1024:>7.0f} KB｜{len(t.splitlines()):>6} 行｜开头：{head(p, 70)}")

print()
print("=" * 100)
print("六、子目录内容")
print("=" * 100)
for sub in sorted(p for p in DBD.iterdir() if p.is_dir()):
    files = list(sub.rglob("*"))
    print(f"  {sub.name}/：{len([f for f in files if f.is_file()])} 个文件")
    for f in sorted([x for x in files if x.is_file()])[:4]:
        print(f"      {f.name[:74]}")
