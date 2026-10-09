# -*- coding: utf-8 -*-
"""kb_cli.py —— 不依赖 DSH 插件、直接驱动知识库引擎的命令行工具。

为什么需要它
------------
知识库能力（入库 / 检索 / 问答 / 统计）其实由本机的一个 Python 引擎提供
（`kb_engine.py`），DSH 插件只是它的一层外壳。当插件因 DSH 版本升级而注册失败时，
本工具提供一条**保底通道**：能力还在，只是从"对话里点工具"变成"命令行敲一条"。

它做的事：按引擎协议（stdin 传 JSON 请求 → stdout 收 JSON 响应）调用引擎，并把结果排版成人能读的样子。

用法
----
    python -B kb_cli.py stats
    python -B kb_cli.py ingest "D:\\资料\\新文献" --force
    python -B kb_cli.py search "Liang Shuming rural reconstruction" --mode keyword --top-k 5
    python -B kb_cli.py search "文化失调" --section "第 294 页"
    python -B kb_cli.py rag "示例人物如何论证乡村建设" --top-k 5

知识库位置（kb_root）解析顺序
-----------------------------
    1) --kb-root 参数
    2) 环境变量 KB_ROOT
    3) 当前目录下的 .kb（若其中确有 kb.sqlite）
    4) 都没有 → 报错退出（**不会**擅自新建空库）

引擎位置解析顺序
----------------
    1) --engine 参数     2) 环境变量 KB_ENGINE
    3) ~/.dsh/profiles/*/node_modules/dsh-kb-rag/kb_engine.py（自动搜索）

不想每次都传 --kb-root，可一次性设置：
    setx KB_ROOT "<项目根>\\.kb"     （新开窗口后生效）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ENGINE_NAME = "kb_engine.py"


# ----------------------------------------------------------------- 定位

def find_engine(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        sys.exit(f"❌ --engine 指定的文件不存在：{p}")
    env = os.environ.get("KB_ENGINE")
    if env and Path(env).exists():
        return Path(env)
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    hits = sorted(home.glob(f".dsh/profiles/*/node_modules/dsh-kb-rag/{ENGINE_NAME}"))
    if hits:
        return hits[0]
    sys.exit(
        "❌ 找不到知识库引擎 kb_engine.py。\n"
        "   它在 dsh-kb-rag 插件目录里，通常在 %USERPROFILE%\\.dsh\\profiles\\<档名>\\node_modules\\dsh-kb-rag\\\n"
        "   可用 --engine 或环境变量 KB_ENGINE 显式指定。"
    )


def find_kb_root(explicit: str | None, allow_missing: bool = False) -> Path:
    if explicit:
        root = Path(explicit)
    elif os.environ.get("KB_ROOT"):
        root = Path(os.environ["KB_ROOT"])
    else:
        root = Path.cwd() / ".kb"
    db = root / "kb.sqlite"
    if not db.exists():
        if allow_missing:
            # 只有显式 --create 才走到这里；把路径打出来，避免"以为在用真库、其实在建空库"
            print(f"⚠️  该位置还没有数据库，将新建：{db}", file=sys.stderr)
            return root
        sys.exit(
            f"❌ 该位置没有数据库：{db}\n"
            "   请用 --kb-root 指定，或设置环境变量 KB_ROOT。\n"
            "   若确实要在此新建库，请在 ingest 命令后加 --create。\n"
            "   提示：真库通常几十 MB 以上；若只有几十 KB，那是路径传错留下的空壳。"
        )
    if db.stat().st_size < 1_000_000:
        print(f"⚠️  注意：{db} 只有 {db.stat().st_size/1024:.0f} KB，像是空壳库，可能不是你想要的。",
              file=sys.stderr)
    return root


# ----------------------------------------------------------------- 调用引擎

def call_engine(engine: Path, command: str, payload: dict) -> dict:
    payload = dict(payload)
    req = json.dumps(payload, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(engine), command],
            input=req.encode("utf-8"),
            capture_output=True,
            timeout=3600,
        )
    except subprocess.TimeoutExpired:
        sys.exit("❌ 引擎调用超时（超过 1 小时）。")
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        err = proc.stderr.decode("utf-8", "replace").strip()
        sys.exit(f"❌ 引擎没有返回内容。stderr：\n{err[:1500]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        sys.exit(f"❌ 引擎返回的不是合法 JSON：\n{out[:800]}")


# ----------------------------------------------------------------- 排版

def show_stats(resp: dict) -> None:
    print(f"知识库：{resp.get('db')}")
    print(f"  文档 {resp.get('docs', '?')}｜块 {resp.get('chunks', '?')}｜向量 {resp.get('vectors', '?')}"
          f"｜字符 {resp.get('chars', '?')}")
    health = resp.get("health") or {}
    if health:
        ok = "健康" if health.get("ok") else "⚠️ 有问题"
        print(f"  库健康：{ok}（孤儿块 {health.get('orphan_chunks', '?')}｜缺向量 {health.get('missing_vecs', '?')}）")
    if resp.get("stale_docs"):
        print(f"  ⚠️ 有 {resp['stale_docs']} 篇是旧版程序写入的，增量入库不会自动刷新它们")
    if resp.get("schema_version"):
        print(f"  库结构版本 {resp.get('schema_version')}｜写入程序 {resp.get('indexed_with') or resp.get('parser_rev') or '?'}")
    recent = resp.get("recent") or resp.get("recent_docs") or []
    if recent:
        print("  最近入库：")
        for d in recent[:8]:
            if isinstance(d, dict):
                print(f"    · {str(d.get('title') or d.get('path') or '?')[:58]}"
                      f"（{d.get('year') or '-'} · {d.get('chunks') or 0} 块）")


def show_ingest(resp: dict) -> None:
    t = resp.get("totals") or resp
    if resp.get("job_id"):
        print(f"已转入后台：job_id={resp['job_id']}｜待处理 {resp.get('pending_files', '?')} 篇")
        print("（后台任务不受命令行退出影响；完成情况查引擎报告或 .kb-jobs 目录）")
        return
    print("入库结果：")
    print(f"  新增 {t.get('added', 0)}｜更新 {t.get('updated', 0)}｜跳过 {t.get('skipped', 0)}"
          f"｜错误 {t.get('errors', 0)}｜元数据刷新 {t.get('meta_updated', 0)}")
    print(f"  块 {t.get('chunks', 0)}｜向量 {t.get('vectors', 0)}")
    files = resp.get("files") or []
    for f in files[:20]:
        if isinstance(f, dict):
            status = f.get("status") or f.get("action") or "?"
            name = str(f.get("path") or f.get("file") or "?")
            print(f"    [{status}] {name[-70:]}")
    # 文档进了库却没有块 —— 三种常见原因，直接讲明白，免得以为"入库失败"
    if t.get("chunks", 0) == 0 and (t.get("added", 0) or t.get("updated", 0)):
        print("\n⚠️  文档已登记，但没有生成任何块（因此检索不到）。常见原因：")
        print("    · 文件内容过短（引擎对过短文件不建索引，保护检索质量）")
        print("    · 扫描件没有文字层（需先 OCR 成带页码的 .PAGES.md 再入库）")
        print("    · 文件本身没有可提取文本（纯图片/表单）")


def show_hits(resp: dict) -> None:
    # 不同命令的命中字段名不同：search 用 sources/results，rag 用 evidence
    hits = (resp.get("sources") or resp.get("results")
            or resp.get("evidence") or resp.get("hits") or [])
    if resp.get("note"):
        print(f"（{resp['note']}）")
    if not hits:
        print("（没有命中。可换查询词、换 mode=keyword，或用 --kb-root 确认库是否正确）")
        return
    if resp.get("lang_note"):
        print(f"注：{resp['lang_note']}\n")
    for i, h in enumerate(hits, 1):
        if not isinstance(h, dict):
            print(f"{i}. {h}")
            continue
        title = h.get("title") or h.get("path") or "（无标题）"
        doi = h.get("doi")
        page = h.get("page_start") or h.get("page")
        bits = [str(b) for b in (h.get("authors"), h.get("year"), h.get("journal")) if b]
        if h.get("section"):
            bits.append(f"§{h['section']}")
        # 引擎 3.2.0 起 page 可能是区间数组（如 [1, 11]），渲染成人读的样子
        if isinstance(page, (list, tuple)) and page:
            first, last = page[0], page[-1]
            bits.append(f"第 {first} 页" if first == last else f"第 {first}–{last} 页")
        elif page:
            bits.append(f"第 {page} 页")
        print(f"{i}. {title}")
        if bits:
            print("   " + " · ".join(bits))
        if doi:
            print(f"   DOI: https://doi.org/{doi}")
        text = (h.get("text") or h.get("snippet") or "").strip().replace("\n", " ")
        if text:
            print(f"   > {text[:220]}")
        print()
    related = resp.get("related") or []
    if related:
        print("相关文献（可作补充阅读）：")
        for r in related[:6]:
            if isinstance(r, dict):
                print(f"    · {str(r.get('title') or r.get('path') or '?')[:60]}"
                      f"（{r.get('authors') or '-'} · {r.get('year') or '-'}"
                      f"{' · ' + str(r['journal']) if r.get('journal') else ''}）")


# ----------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="kb_cli.py",
        description="直接驱动知识库引擎的命令行工具（不依赖 DSH 插件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="详细说明（含 kb_root / 引擎定位规则）见本文件开头的注释。",
    )
    ap.add_argument("--kb-root", help="数据库所在目录（默认取 KB_ROOT 环境变量或当前目录的 .kb）")
    ap.add_argument("--engine", help="kb_engine.py 的完整路径（一般不用给）")
    ap.add_argument("--json", action="store_true", help="输出引擎原始 JSON，便于脚本处理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="查看文档/块/向量数量与最近入库")

    p_ing = sub.add_parser("ingest", help="把文件或文件夹入库（递归、增量、自动去重）")
    p_ing.add_argument("paths", nargs="+", help="要入库的文件或文件夹")
    p_ing.add_argument("--force", action="store_true", help="内容没变也强制重新解析")
    p_ing.add_argument("--rebuild", action="store_true", help="按库内记录把每篇原文重新解析")
    p_ing.add_argument("--metadata-only", action="store_true", help="只刷新题录，不重切块、不重算向量")
    p_ing.add_argument("--create", action="store_true", help="允许在此位置新建一个知识库（首次入库时用）")

    p_se = sub.add_parser("search", help="检索，返回片段与出处（含页码）")
    p_se.add_argument("query", help="查询词：建议英文/中文术语串，3–12 词")
    p_se.add_argument("--mode", default="hybrid", choices=["keyword", "vector", "hybrid"],
                      help="keyword=逐字匹配（核对引文用）｜hybrid=默认｜vector=语义")
    p_se.add_argument("--top-k", type=int, default=5, help="返回条数（默认 5）")
    p_se.add_argument("--snippet", type=int, default=300, help="每条片段字数")
    p_se.add_argument("--title", help="筛选：标题包含")
    p_se.add_argument("--section", help="筛选：章节名包含（可精确定位某页，如 '第 294 页'）")
    p_se.add_argument("--authors", help="筛选：作者包含")
    p_se.add_argument("--journal", help="筛选：刊物包含")
    p_se.add_argument("--year", help="筛选：年份，可写 2021 或 '>=2018'")
    p_se.add_argument("--kind", help="筛选：文件类型 pdf/md/docx")

    p_rag = sub.add_parser("rag", help="按证据作答（返回证据片段，供人或模型据此回答）")
    p_rag.add_argument("query", help="问题")
    p_rag.add_argument("--top-k", type=int, default=3, help="证据条数（默认 3）")

    a = ap.parse_args()

    engine = find_engine(a.engine)
    kb_root = str(find_kb_root(a.kb_root, allow_missing=bool(getattr(a, "create", False))).resolve())

    if a.cmd == "stats":
        resp = call_engine(engine, "stats", {"kb_root": kb_root})
        if a.json:
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        elif resp.get("ok") is False:
            print("❌", resp.get("error"))
        else:
            show_stats(resp)

    elif a.cmd == "ingest":
        payload: dict = {"paths": [str(Path(p).resolve()) for p in a.paths], "kb_root": kb_root}
        if a.force:
            payload["force"] = True
        if a.rebuild:
            payload["rebuild"] = True
        if a.metadata_only:
            payload["metadata_only"] = True
        resp = call_engine(engine, "ingest", payload)
        if a.json:
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        elif resp.get("ok") is False:
            print("❌", resp.get("error"))
        else:
            show_ingest(resp)

    else:  # search / rag
        payload = {"query": a.query, "kb_root": kb_root}
        if a.cmd == "search":
            payload["mode"] = a.mode
            payload["top_k"] = a.top_k
            payload["snippet"] = a.snippet
            filters = {k: v for k, v in (
                ("title", a.title), ("section", a.section), ("authors", a.authors),
                ("journal", a.journal), ("year", a.year), ("kind", a.kind)) if v}
            if filters:
                payload["filters"] = filters
        else:
            payload["top_k"] = a.top_k
        resp = call_engine(engine, a.cmd, payload)
        if a.json:
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        elif resp.get("ok") is False:
            print("❌", resp.get("error"))
        else:
            show_hits(resp)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
