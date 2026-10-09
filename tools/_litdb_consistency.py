# -*- coding: utf-8 -*-
"""05_文献数据库 一致性与质量分析（只读）。

  A. 题录库 v1→v4 的字段级演进
  B. refs.bib ↔ literature_db 覆盖关系与题录质量
  C. 引文台账 vs 二级文献数据库：口径与交叉
  D. quote_check_result.json 的内容与失效路径
  E. snapshots 结构
  F. 中间产物重复（extract / fulltext）
  G. 脚本可复现性（硬编码路径、依赖）

用法：python -B _litdb_consistency.py
"""
import csv
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# 资料库目录**不写死**：交给同目录的 _paths.py 解析（参数 → 环境变量 → 项目布局.json
# → 结构探测 → 报错）。换电脑/改目录名时不必再改这个脚本。
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 被 import 时也能找到 _paths
from _paths import LazyPath, db_dir  # noqa: E402

D = LazyPath(lambda: db_dir(strict=False))
if D is None:
    raise SystemExit(
        "解析不出「05_文献数据库」目录。请用环境变量 LITDB_DBDIR 或 LITDB_PROJECT 指定，"
        "或在项目根放 项目布局.json 声明 db 目录。")


def load(name):
    return json.loads((D / name).read_text(encoding="utf-8"))


print("=" * 104)
print("A. 题录库 v1→v4 的字段级演进")
print("=" * 104)
vers = {}
for n in ("literature_db.json", "literature_db_v2.json", "literature_db_v3.json", "literature_db_v4.json"):
    d = load(n)
    vers[n] = d
    fields = list(d[0].keys()) if d else []
    print(f"  {n:<26} {len(d)} 条｜{len(fields)} 字段｜{fields}")
base = list(vers["literature_db.json"][0].keys())
for n in ("literature_db_v2.json", "literature_db_v3.json", "literature_db_v4.json"):
    new = [f for f in vers[n][0].keys() if f not in base]
    print(f"  {n} 新增字段：{new}")
    if new:
        for f in new[:4]:
            vals = [str(r.get(f, ""))[:34] for r in vers[n][:3]]
            print(f"      {f} 样例：{vals}")

print()
print("  v1 vs v4 逐条是否同序/同题（抽 5 条）：")
for i in (0, 30, 60, 90, 120):
    a, b = vers["literature_db.json"][i], vers["literature_db_v4.json"][i]
    same = a.get("title") == b.get("title")
    print(f"     #{i:<4} 同题={same}  v1「{str(a.get('title'))[:30]}」｜v4「{str(b.get('title'))[:30]}」")

print()
print("=" * 104)
print("B. 题录质量（以 v4 为准）与 refs.bib 覆盖")
print("=" * 104)
v4 = vers["literature_db_v4.json"]
for f in ("title", "author", "journal", "year", "pages", "doc_id", "topic_l1", "relevance"):
    if f not in v4[0]:
        print(f"  {f:<12} 字段不存在")
        continue
    empty = sum(1 for r in v4 if not str(r.get(f) or "").strip()
                or str(r.get(f)).strip() in ("待核实", "待核", "None"))
    print(f"  {f:<12} 缺失/待核实 {empty:>4}/{len(v4)}（{empty/len(v4):.0%}）")
print("\n  author 字段样例（前 8）：", [str(r.get('author'))[:16] for r in v4[:8]])
print("  journal 字段样例（前 8）：", [str(r.get('journal'))[:18] for r in v4[:8]])
print("  year 分布（前 10）：", Counter(str(r.get("year")) for r in v4).most_common(10))

bib_txt = (D / "refs.bib").read_text(encoding="utf-8", errors="replace")
bib_keys = re.findall(r"@\w+\{([^,]+),", bib_txt)
bib_ids = re.findall(r"doc_id=([0-9a-f]{8})", bib_txt)
print(f"\n  refs.bib：{len(bib_keys)} 条｜带 doc_id 的 {len(bib_ids)} 条")
v4_ids = {str(r.get("doc_id")) for r in v4 if r.get("doc_id")}
print(f"  literature_db_v4 doc_id 数：{len(v4_ids)}")
print(f"  交集 {len(set(bib_ids) & v4_ids)}｜bib 里有而库中无 {len(set(bib_ids) - v4_ids)}"
      f"｜库里有而 bib 无 {len(v4_ids - set(bib_ids))}")
print(f"  bib 中「待核实」出现 {bib_txt.count('待核实')} 次")

print()
print("=" * 104)
print("C. 引文台账 vs 二级文献数据库")
print("=" * 104)
led = list(csv.DictReader(io.open(D / "引文台账.csv", encoding="utf-8-sig")))
print(f"  引文台账：{len(led)} 行｜字段 {list(led[0].keys())}")
print(f"     来源分布：{Counter(r.get('来源') for r in led).most_common()}")
print(f"     类型分布：{Counter(r.get('类型') for r in led).most_common()}")
print(f"     状态分布：{Counter(r.get('状态') for r in led).most_common(6)}")
l2 = load("二级文献数据库.json")
ent = l2.get("entries", l2 if isinstance(l2, list) else [])
print(f"\n  二级文献数据库：{len(ent)} 条｜键 {list(l2.keys()) if isinstance(l2, dict) else 'list'}")
if ent:
    print(f"     字段 {list(ent[0].keys())}")
    print(f"     类型分布：{Counter(str(r.get('类型')) for r in ent).most_common(6)}")
    print(f"     被引次数分布：{Counter(str(r.get('被引次数')) for r in ent).most_common(6)}")
    titles_l2 = {str(r.get("题名") or "").strip()[:16] for r in ent}
    titles_led = {str(r.get("引文") or "").strip()[:16] for r in led}
    print(f"  『二级文献』题名 与『台账引文』前 16 字交集：{len(titles_l2 & titles_led)}")
    print("  → 两者口径不同：台账 = 论文里的引文；二级文献 = 这些文献自身引用的文献（下游）")

print()
print("=" * 104)
print("D. quote_check_result.json")
print("=" * 104)
qc = load("quote_check_result.json")
print(f"  类型 {type(qc).__name__}｜条目 {len(qc)}")
if isinstance(qc, list) and qc:
    print(f"  字段 {list(qc[0].keys())}")
    print(f"  样例：{json.dumps(qc[0], ensure_ascii=False)[:300]}")
    paths = {str(r.get("path") or r.get("file") or "") for r in qc if isinstance(r, dict)}
    alive = sum(1 for p in paths if p and Path(p).exists())
    print(f"  涉及路径 {len(paths)} 个｜存在 {alive}（失效 {len(paths)-alive}）")

print()
print("=" * 104)
print("E. snapshots 结构")
print("=" * 104)
snap = D / "snapshots"
print(f"  根下文件 {len([f for f in snap.iterdir() if f.is_file()])} 个")
for sub in sorted(x for x in snap.iterdir() if x.is_dir()):
    fs = list(sub.rglob("*"))
    print(f"  {sub.name}/：{len([f for f in fs if f.is_file()])} 个文件")
meta = snap / "_meta.json"
if meta.exists():
    print(f"  _meta.json：{meta.read_text(encoding='utf-8')[:400]}")

print()
print("=" * 104)
print("F. 中间产物重复")
print("=" * 104)
for sub in ("extract", "fulltext"):
    items = {}
    for p in sorted((D / sub).glob("*.txt")):
        m = re.match(r"(\d+)_(.*)\.txt$", p.name)
        if m:
            items.setdefault(m.group(2), []).append((m.group(1), p.stat().st_size))
    dup = {k: v for k, v in items.items() if len(v) > 1}
    print(f"  {sub}/：{len(items)} 篇（去重前 {sum(len(v) for v in items.values())} 个文件）｜同名重复 {len(dup)} 组")
    for k, v in list(dup.items())[:6]:
        print(f"     「{k[:50]}」→ {v}")

print()
print("=" * 104)
print("G. 脚本可复现性")
print("=" * 104)
for p in sorted((D / "scripts").glob("*")):
    if p.suffix not in (".py", ".ps1"):
        continue
    txt = p.read_text(encoding="utf-8", errors="replace")
    hard = re.findall(r"[A-Za-z]:\\\\[^\"'\n\r]{4,90}", txt)
    imports = re.findall(r"^(?:import|from)\s+([\w\.]+)", txt, re.M)
    ext = sorted({i.split(".")[0] for i in imports
                  if i.split(".")[0] not in ("os", "sys", "json", "re", "csv", "io", "pathlib",
                                             "collections", "subprocess", "argparse", "time",
                                             "datetime", "shutil", "hashlib", "unicodedata", "math")})
    print(f"  {p.name:<26} {len(txt.splitlines()):>4} 行｜外部依赖 {ext or '无'}｜硬编码绝对路径 {len(set(hard))} 处")
