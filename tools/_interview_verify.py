#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_interview_verify.py — 访谈材料知识库结构验收（**严格只读**）

与 `验收.py` 的分工
===================
`验收.py` 是给**文献库**用的：查页码覆盖率、页码倒挂、印刷页码锚点。
访谈库里 `page_start/page_end` 恒为 NULL，那些检查没有对象。
本工具只查**访谈库该查的东西**，并坚持一条安全铁律：

    **绝不用 kb_engine.connect() 打开目标库** —— connect() 在路径写错时会
    `mkdir` + 建一个空库（"空壳陷阱"）。本工具一律用 `file:...?mode=ro` 只读打开；
    库文件不存在就报错，**绝不创建任何文件**（自测里有一项专门验证这一点）。

检查项
======
1. 基础设施：kb.sqlite 是否存在、integrity_check、user_version vs 引擎 SCHEMA_VERSION
2. 计数与一致性：docs/chunks/vecs、chunk_count 不一致、悬空块、weight>0 无向量、
   同 sha256 重复、盲文档（无 weight>0 块）、空库/空壳库
3. 访谈专用：`(rescued)` 节、weight<=0 的节、巨型分块（>1200 字符）、过短分块、
   **时间码覆盖率**、**说话人标签覆盖率**、page 锚点是否被误用
4. 文件与库对账：库内路径失效（dangling）、`--src` 下有未入库的转写稿
5. 元数据治理：title / authors / year 缺失面 + 取样清单
6. 解析器版本：docs.indexed_with 分布 vs 引擎 PARSER_REV，复算引擎自己的
   stale_docs / stale_kind（`chunk`=必须全量重灌，`metadata_only` 无效；`meta`=只刷元数据）

用法
====
  python _interview_verify.py --kb-root "<项目>\\.kb" [--src "<项目>\\转写稿"] \\
         [--out 访谈库验收报告.md] [--json 结果.json]
  python _interview_verify.py --selftest

退出码：0 = 无 ERROR；1 = 有 ERROR。
控制台中文可能因代码页显示为乱码，**以报告文件为准**。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".markdown", ".docx", ".pdf"}
ENGINE_ENV = "KB_ENGINE"

# 可选前导时间码，避免「[00:00:10] A03：…」这种推荐格式被漏掉
SPEAKER_RE = re.compile(
    r"(?m)^\s*(?:[\[\(]?\d{1,2}:\d{2}(?::\d{2})?[\]\)]?\s*)?([^\s：:，,。]{1,12})\s*[：:]\s*\S")
TIMECODE_RE = re.compile(r"[\[\(]?\b\d{1,2}:\d{2}(?::\d{2})?\b[\]\)]?")
MEGA_CHARS = 1200          # 引擎的切分阈值
MIN_CHUNK_CHARS = 40       # 引擎 `if len(body) < 40: return []`


def find_engine(explicit: str | None = None) -> Path:
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    if os.environ.get(ENGINE_ENV):
        cands.append(Path(os.environ[ENGINE_ENV]))
    home = Path.home()
    cands += sorted((home / ".dsh" / "profiles").glob("*/node_modules/dsh-kb-rag/kb_engine.py"))
    cands += sorted((home / ".dsh").glob("**/dsh-kb-rag/kb_engine.py"))
    seen, uniq = set(), []
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r in seen or not r.is_file():
            continue
        seen.add(r)
        uniq.append(r)
    if not uniq:
        raise SystemExit("找不到 kb_engine.py。请用 --engine 指定，或设环境变量 %s。" % ENGINE_ENV)
    uniq.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return uniq[0]


def load_engine(path: Path):
    spec = importlib.util.spec_from_file_location("kb_engine_for_verify", str(path))
    if spec is None or spec.loader is None:
        raise SystemExit("无法载入引擎：%s" % path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def open_ro(db_file: Path) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % str(db_file).replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    return con


def resolve_db(kb_root: Path) -> Path:
    """kb_root 可以是 .kb 目录本身，也可以是项目根（宽容但会明说）。"""
    if (kb_root / "kb.sqlite").is_file():
        return kb_root / "kb.sqlite"
    if (kb_root / ".kb" / "kb.sqlite").is_file():
        return kb_root / ".kb" / "kb.sqlite"
    return kb_root / "kb.sqlite"


def verify(kb_root: Path, src: Path | None, mod) -> tuple[dict, list[dict]]:
    items: list[dict] = []

    def add(code, sev, msg, detail=None):
        items.append({"code": code, "sev": sev, "msg": msg, "detail": detail})

    db_file = resolve_db(kb_root)
    stats: dict = {"kb_root": str(kb_root), "db_file": str(db_file)}

    # ── 1. 基础设施：先判存在性，绝不创建 ──
    if not db_file.is_file():
        add("E_DB_MISSING", "E",
            "在 %s 下找不到 kb.sqlite。**不做任何创建**——kb_root 必须是「直接包含 "
            "kb.sqlite 的目录」（通常是项目的 .kb 目录）。路径写错时引擎会静默建一个空库，"
            "看起来像「库是空的」，实际是路径错了。" % kb_root)
        return stats, items

    size = db_file.stat().st_size
    stats["db_bytes"] = size
    stats["db_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(db_file.stat().st_mtime))
    con = open_ro(db_file)
    try:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        uv = con.execute("PRAGMA user_version").fetchone()[0]
        stats["integrity_check"] = integ
        stats["user_version"] = uv
        stats["schema_version_engine"] = mod.SCHEMA_VERSION
        if integ != "ok":
            add("E_INTEGRITY", "E", "PRAGMA integrity_check = %r，库已损坏。" % integ)
        if uv != mod.SCHEMA_VERSION:
            add("W_SCHEMA_VERSION", "W",
                "库 user_version=%s、引擎 SCHEMA_VERSION=%s 不一致。引擎下次写入会自动迁移，"
                "**迁移前先备份**。" % (uv, mod.SCHEMA_VERSION))

        def q1(sql, args=()):
            r = con.execute(sql, args).fetchone()
            return r[0] if r else None

        docs_n = q1("SELECT COUNT(*) FROM docs")
        chunks_n = q1("SELECT COUNT(*) FROM chunks")
        vecs_n = q1("SELECT COUNT(*) FROM vecs")
        stats.update({"docs": docs_n, "chunks": chunks_n, "vecs": vecs_n})
        if docs_n == 0:
            add("E_EMPTY_DB", "E",
                "库里 0 篇文档。若你以为这里应该有材料，基本可确认是「空壳库」——"
                "引擎在错误路径上自动创建的空库（本库 %.1f KB）。先核对 kb_root 是否"
                "「直接含 kb.sqlite 的目录」。" % (size / 1024),
                {"db_file": str(db_file),
                 "dir_listing": sorted(p.name for p in db_file.parent.iterdir())[:20]})

        # ── 2. 计数与一致性 ──
        mismatch = con.execute(
            "SELECT d.id, d.path, d.chunk_count, COUNT(c.id) AS real FROM docs d "
            "LEFT JOIN chunks c ON c.doc_id = d.id GROUP BY d.id "
            "HAVING d.chunk_count IS NOT NULL AND d.chunk_count != real").fetchall()
        if mismatch:
            add("E_CHUNK_COUNT_MISMATCH", "E",
                "%d 篇文档的 chunk_count 与实际块数不一致（增量入库或手工改库的典型残留）。"
                % len(mismatch),
                [{"path": Path(r["path"]).name, "chunk_count": r["chunk_count"],
                  "real": r["real"]} for r in mismatch[:8]])

        orphan = q1("SELECT COUNT(*) FROM chunks WHERE doc_id NOT IN (SELECT id FROM docs)")
        if orphan:
            add("E_ORPHAN_CHUNKS", "E",
                "%d 个块没有对应文档（doc_id 悬空）——正常删除走 ON DELETE CASCADE，"
                "出现悬空说明有人直接改过库。" % orphan)

        novec = q1("SELECT COUNT(*) FROM chunks c LEFT JOIN vecs v ON v.chunk_id = c.id "
                   "WHERE v.chunk_id IS NULL AND c.weight > 0")
        if novec:
            add("E_MISSING_VECS", "E",
                "%d 个 weight>0 的块没有向量 → 向量路检索不到它们（模型不可用时入库的典型"
                "后果）。修法：重跑 kb_ingest（增量会补向量）或 rebuild=true。" % novec)

        dup = con.execute("SELECT sha256, COUNT(*) n FROM docs WHERE sha256 IS NOT NULL "
                          "GROUP BY sha256 HAVING n > 1").fetchall()
        if dup:
            add("E_DUP_SHA256", "E",
                "%d 组文档内容完全相同（同 sha256）→ 同一份转写稿入库多次，检索会重复命中。"
                % len(dup), [{"sha256": r["sha256"][:12], "n": r["n"]} for r in dup[:8]])

        blind = con.execute(
            "SELECT d.path FROM docs d WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.doc_id = d.id AND c.weight > 0)").fetchall()
        if blind:
            add("E_BLIND_DOC", "E",
                "%d 篇文档没有任何 weight>0 的块 → 在检索里**完全不存在**（盲文档），"
                "而且搜不到时不会收到任何提示。" % len(blind),
                [Path(r["path"]).name for r in blind[:8]])

        # ── 3. 访谈专用指标 ──
        rescued = q1("SELECT COUNT(*) FROM chunks WHERE section LIKE '%(rescued)%'")
        if rescued:
            add("E_RESCUED", "E",
                "%d 个块的 section 带 (rescued) 后缀 → 触发过『整篇不可检索兜底』，"
                "这些文档的 section 语义不可信；请检查标题里是否出现"
                "「附录／致谢／参考文献」。" % rescued)

        zero_secs = con.execute(
            "SELECT section, COUNT(*) n FROM chunks WHERE weight <= 0 AND section != 'References' "
            "GROUP BY section").fetchall()
        if zero_secs:
            add("W_ZERO_WEIGHT", "W",
                "存在 weight<=0 的非 References 分块（%s）——引擎会丢弃这类节，"
                "库里有说明是旧版本写入或手工插入的。"
                % "、".join(r["section"] for r in zero_secs[:8]))

        mega = con.execute("SELECT COUNT(*), COALESCE(MAX(LENGTH(text)),0) FROM chunks "
                           "WHERE LENGTH(text) > ?", (MEGA_CHARS,)).fetchone()
        if mega[0]:
            sample = con.execute(
                "SELECT d.path, LENGTH(c.text) n, substr(c.text,1,60) head FROM chunks c "
                "JOIN docs d ON d.id = c.doc_id WHERE LENGTH(c.text) > ? "
                "ORDER BY LENGTH(c.text) DESC LIMIT 5", (MEGA_CHARS,)).fetchall()
            add("E_MEGA_CHUNK", "E",
                "%d 个块超过 %d 字符（最长 %d）→ 中文长段落未被切开，向量被整篇平均，"
                "检索精度下降。多半是转写稿在句末标点后没有空白（split_long 无切点）。"
                % (mega[0], MEGA_CHARS, mega[1]),
                [{"file": Path(r["path"]).name, "chars": r["n"], "head": r["head"]}
                 for r in sample])

        tiny = q1("SELECT COUNT(*) FROM chunks WHERE weight > 0 AND LENGTH(text) < ?",
                  (MIN_CHUNK_CHARS,))
        if tiny:
            add("W_TINY_CHUNK", "W",
                "%d 个 weight>0 的块不足 %d 字符（引擎本会丢弃这类小节，出现说明分块逻辑"
                "版本不一致或有人手工插入）。" % (tiny, MIN_CHUNK_CHARS))

        rows = con.execute("SELECT text, section, page_start, page_end FROM chunks "
                           "WHERE weight > 0").fetchall()
        n_pos = n_tc = n_sp = n_page = n_tc_sec = 0
        for r in rows:
            n_pos += 1
            t = r["text"] or ""
            if TIMECODE_RE.search(t):
                n_tc += 1
            if SPEAKER_RE.search(t):
                n_sp += 1
            if r["page_start"] is not None or r["page_end"] is not None:
                n_page += 1
            if TIMECODE_RE.search(r["section"] or ""):
                n_tc_sec += 1
        stats.update({"positive_chunks": n_pos, "chunks_with_timecode": n_tc,
                      "chunks_with_speaker": n_sp, "chunks_with_page": n_page,
                      "sections_with_timecode": n_tc_sec})
        if n_pos:
            r_tc, r_sp = n_tc / n_pos, n_sp / n_pos
            if r_tc == 0:
                add("W_NO_TIMECODE", "W",
                    "weight>0 的块里没有任何时间码 → 命中后无法定位到访谈的具体时点。"
                    "访谈材料的引用锚点就是时间码（页锚点对 txt/md 恒为 NULL）。"
                    "带时间码的 section 有 %d 个。" % n_tc_sec)
            elif r_tc < 0.3:
                add("W_LOW_TIMECODE_COVERAGE", "W",
                    "只有 %.0f%%（%d/%d）的块含时间码，锚点覆盖不足。"
                    % (r_tc * 100, n_tc, n_pos))
            if r_sp == 0:
                add("W_NO_SPEAKER", "W",
                    "weight>0 的块里没有可识别的说话人标签（形如「A03：」）→ "
                    "命中后无法判断话是谁说的。")
            elif r_sp < 0.3:
                add("W_LOW_SPEAKER_COVERAGE", "W",
                    "只有 %.0f%%（%d/%d）的块含说话人标签。" % (r_sp * 100, n_sp, n_pos))
            if n_page:
                add("I_PAGE_ANCHORS", "I",
                    "有 %d 个块带 page_start/page_end 页锚点 → 库里混入了 PDF 文档"
                    "（如期刊上发表的访谈文本）。这本身没问题，但别把页锚点当作访谈稿的"
                    "引用依据。" % n_page)
            else:
                add("I_ANCHOR_TIMECODE", "I",
                    "全部 %d 个 weight>0 块的 page 字段为 NULL（文本类材料的正常状态）；"
                    "时间码覆盖 %.0f%%，说话人覆盖 %.0f%%。" % (n_pos, r_tc * 100, r_sp * 100))

        stats["kinds"] = {r["k"]: r["n"] for r in con.execute(
            "SELECT COALESCE(kind,'(null)') k, COUNT(*) n FROM docs GROUP BY 1 ORDER BY n DESC")}
        stats["top_sections"] = [
            {"section": r["section"], "weight": r["weight"], "n": r["n"], "chars": r["chars"]}
            for r in con.execute(
                "SELECT section, weight, COUNT(*) n, SUM(LENGTH(text)) chars FROM chunks "
                "GROUP BY section, weight ORDER BY chars DESC LIMIT 15")]

        # ── 4. 文件与库对账 ──
        docs = con.execute("SELECT id, path, title, authors, year, sha256, indexed_with "
                           "FROM docs").fetchall()
        dangling = [r for r in docs if not Path(r["path"]).is_file()]
        if dangling:
            add("E_DANGLING_PATH", "E",
                "%d 篇文档在磁盘上已不存在（路径失效）→ 无法回到原始转写稿核对逐字引文。"
                "常见原因：入库后改名/移动，但没同步改库。" % len(dangling),
                [Path(r["path"]).name for r in dangling[:8]])
        if src is not None:
            if not src.is_dir():
                add("W_SRC_MISSING", "W", "--src 目录不存在：%s" % src)
            else:
                disk = [p for p in src.rglob("*") if p.is_file()
                        and p.suffix.lower() in TEXT_EXTS and not p.name.startswith("~$")]
                in_db = {str(Path(r["path"]).resolve()).lower() for r in docs}
                missing = [p for p in disk if str(p.resolve()).lower() not in in_db]
                stats["src_files"] = len(disk)
                stats["src_missing"] = len(missing)
                if missing:
                    add("E_MISSING_SOURCE", "E",
                        "%d 个转写稿在磁盘上但**没有入库**（按绝对路径比对）。访谈材料要求"
                        "齐备，请核对是否确实漏了。" % len(missing),
                        [p.name for p in missing[:12]])
                prefix = str(src.resolve()).lower()
                outside = [Path(r["path"]).name for r in docs
                           if not str(Path(r["path"]).resolve()).lower().startswith(prefix)]
                if outside:
                    add("I_OUTSIDE_SRC", "I",
                        "%d 篇库内文档不在 --src 目录下（从别处入库的材料）。" % len(outside),
                        outside[:8])

        # ── 5. 元数据治理 ──
        gaps, samples = {"title": 0, "authors": 0, "year": 0}, {"title": [], "authors": [], "year": []}
        for r in docs:
            for k in gaps:
                v = r[k]
                if v is None or (isinstance(v, str) and not v.strip()):
                    gaps[k] += 1
                    if len(samples[k]) < 6:
                        samples[k].append(Path(r["path"]).name)
        stats["meta_gaps"] = gaps
        for k, n in gaps.items():
            if n:
                add("W_META_GAP", "W", "有 %d/%d 篇文档的 %s 为空" % (n, docs_n, k), samples[k])

        # ── 6. 解析器版本（复算引擎自己的判定）──
        stale = [r for r in docs if str(r["indexed_with"] or "") != mod.PARSER_TOKEN]
        revs = {mod._rev_of(r["indexed_with"]) for r in stale}
        stats.update({"engine_token": mod.PARSER_TOKEN, "parser_rev": mod.PARSER_REV,
                      "stale_docs": len(stale), "stale_revs": sorted(revs),
                      "stale_kind": mod._stale_kind(revs) if stale else "none"})
        if stale:
            kind = mod._stale_kind(revs)
            add("W_STALE_DOCS", "W",
                "%d/%d 篇文档由旧解析器写入（indexed_with ≠ %s，实际 rev=%s），引擎判定 "
                "stale_kind = **%s**%s。" % (len(stale), docs_n, mod.PARSER_TOKEN, sorted(revs),
                                             kind,
                                             "（分块/向量会变 → 需全量重灌，metadata_only 无效）"
                                             if kind == "chunk" else "（只刷元数据即可）"),
                [Path(r["path"]).name for r in stale[:5]])
    finally:
        con.close()
    return stats, items


def render_report(stats: dict, items: list[dict], engine: Path, mod, out: Path) -> dict:
    tot = {"E": sum(1 for i in items if i["sev"] == "E"),
           "W": sum(1 for i in items if i["sev"] == "W"),
           "I": sum(1 for i in items if i["sev"] == "I")}
    L = ["# 访谈材料知识库验收报告", "",
         "- 引擎：`%s`（VERSION %s / PARSER_REV %d / SCHEMA_VERSION %s）"
         % (engine, mod.VERSION, mod.PARSER_REV, mod.SCHEMA_VERSION),
         "- 库：`%s`" % stats.get("db_file"),
         "- 打开方式：`file:...?mode=ro` 只读（**未调用 connect()**，不会创建或迁移库）",
         "- 结论：❌ %d / ⚠️ %d / ℹ️ %d" % (tot["E"], tot["W"], tot["I"]), "",
         "## 关键计数", "", "| 指标 | 值 |", "| --- | --- |"]
    for k, label in [("db_bytes", "库体积(bytes)"), ("db_mtime", "库 mtime"),
                     ("integrity_check", "integrity_check"), ("user_version", "库 user_version"),
                     ("schema_version_engine", "引擎 SCHEMA_VERSION"),
                     ("docs", "文档数"), ("chunks", "分块数"), ("vecs", "向量数"),
                     ("positive_chunks", "weight>0 块数"),
                     ("chunks_with_timecode", "含时间码的块"),
                     ("chunks_with_speaker", "含说话人标签的块"),
                     ("sections_with_timecode", "含时间码的 section"),
                     ("chunks_with_page", "带页锚点的块"),
                     ("src_files", "--src 磁盘文件数"), ("src_missing", "其中未入库"),
                     ("engine_token", "引擎 PARSER_TOKEN"), ("parser_rev", "引擎 PARSER_REV"),
                     ("stale_docs", "陈旧文档数"), ("stale_kind", "引擎判定")]:
        if k in stats:
            L.append("| %s | %s |" % (label, stats[k]))
    if stats.get("meta_gaps"):
        L.append("| 元数据缺失 title/authors/year | %s |"
                 % "/".join(str(stats["meta_gaps"][k]) for k in ("title", "authors", "year")))
    L.append("")
    if stats.get("kinds"):
        L += ["**文件类型分布**：" + "、".join("%s=%d" % (k, v)
                                          for k, v in stats["kinds"].items()), ""]
    if stats.get("top_sections"):
        L += ["## section 分布（前 15）", "", "| section | 权重 | 块数 | 字符数 |",
              "| --- | --- | --- | --- |"]
        for s in stats["top_sections"]:
            L.append("| `%s` | %s | %d | %d |" % (s["section"], s["weight"], s["n"], s["chars"]))
        L.append("")
    L += ["## 发现", ""]
    if not items:
        L.append("未发现问题。")
    for it in items:
        L.append("- %s **%s** %s" % ({"E": "❌", "W": "⚠️", "I": "ℹ️"}[it["sev"]],
                                     it["code"], it["msg"]))
        if it.get("detail"):
            d = it["detail"]
            for x in (d if isinstance(d, list) else [d])[:10]:
                L.append("  - `%s`" % json.dumps(x, ensure_ascii=False)[:220])
    L.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    return tot


# ── 自测 ─────────────────────────────────────────────────────────────────────
def _insert_doc(db, path: Path, sha: str, chunks, indexed_with: str,
                title="A03 - 2024 - 首次访谈", authors="A03", year=2024,
                chunk_count=None, with_vecs=True):
    cur = db.execute(
        "INSERT INTO docs(path,title,authors,year,journal,doi,kind,sha256,size,mtime,"
        "chunk_count,indexed_at,zotero_key,indexed_with) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(path), title, authors, year, None, None, path.suffix.lstrip("."), sha,
         path.stat().st_size if path.is_file() else 0, time.time(),
         len(chunks) if chunk_count is None else chunk_count, time.time(), None, indexed_with))
    did = cur.lastrowid
    for i, c in enumerate(chunks):
        sec, w, text = c[0], c[1], c[2]
        pg = c[3] if len(c) > 3 else None
        cc = db.execute(
            "INSERT INTO chunks(doc_id,section,weight,seq,text,para_start,para_end,"
            "page_start,page_end) VALUES(?,?,?,?,?,?,?,?,?)",
            (did, sec, w, i, text, 1, 1, pg, pg))
        if with_vecs and w > 0:
            db.execute("INSERT INTO vecs(chunk_id,vec) VALUES(?,?)", (cc.lastrowid, b"\x00" * 8))
    return did


def selftest() -> int:
    print("== _interview_verify.py 自测 ==")
    engine = find_engine()
    mod = load_engine(engine)
    print("引擎：%s" % engine)
    print("VERSION %s / PARSER_REV %d" % (mod.VERSION, mod.PARSER_REV))
    checks: list[tuple[str, bool, str]] = []
    tmp = Path(tempfile.mkdtemp(prefix="ivverify_"))

    def add_check(name, cond, info=""):
        checks.append((name, bool(cond), info))

    plain_chunks = [
        ("[00:00:10] 开场与知情同意", 1.0,
         "A03：这次访谈是自愿参加的，任何问题都可以不回答，随时可以停下来，不会有任何影响。"
         "B01：好的，我明白，那我们开始吧，我会尽量把当时的情况讲清楚。"),
        ("[00:03:20] 家庭背景", 1.0,
         "B01：五口人，我排行老二，父亲在供销社上班，母亲在家种地，日子不算宽裕但也能过得去。"),
    ]
    tc_chunks = [("[00:00:10] 开场与知情同意", 1.0, "[00:00:10] " + plain_chunks[0][2]),
                 ("[00:03:20] 家庭背景", 1.0, "[00:03:20] " + plain_chunks[1][2])]

    try:
        # ---- 干净库：必须 0 ERROR，且覆盖率断言非平凡 ----
        clean = tmp / "clean"
        db = mod.connect(str(clean))
        f1 = clean / "A03 - 2024 - 首次访谈.md"
        f1.write_text("x", encoding="utf-8")
        _insert_doc(db, f1, "a" * 64, tc_chunks, mod.PARSER_TOKEN)
        f2 = clean / "A04 - 2024 - 首次访谈.md"
        f2.write_text("y", encoding="utf-8")
        _insert_doc(db, f2, "b" * 64,
                    [("[00:01:00] 工作经历", 1.0,
                      "[00:01:00] A04：他换过几次工作，每次都是自己决定的，中间还去外地待过两年。")],
                    mod.PARSER_TOKEN)
        db.commit()
        db.close()
        st, items = verify(clean, clean, mod)
        add_check("① 干净库无 ERROR", not any(i["sev"] == "E" for i in items),
                  "E=%s" % [i["code"] for i in items if i["sev"] == "E"])
        add_check("①a 计数正确 (2 文档/3 块)", st["docs"] == 2 and st["chunks"] == 3,
                  "docs=%s chunks=%s" % (st["docs"], st["chunks"]))
        add_check("①b 时间码覆盖率 100%",
                  st["chunks_with_timecode"] == st["positive_chunks"] == 3,
                  "tc=%s pos=%s" % (st["chunks_with_timecode"], st["positive_chunks"]))
        add_check("①c 说话人覆盖率 100%（含前导时间码也能识别）",
                  st["chunks_with_speaker"] == st["positive_chunks"] == 3,
                  "sp=%s pos=%s" % (st["chunks_with_speaker"], st["positive_chunks"]))
        add_check("①d stale_docs=0", st["stale_docs"] == 0, "stale=%s" % st["stale_docs"])
        out = clean / "r.md"
        tot = render_report(st, items, engine, mod, out)
        add_check("①e 干净报告写出且 E=0", out.is_file() and tot["E"] == 0, "tot=%s" % tot)

        # ---- 缺陷库 ----
        bad = tmp / "bad"
        db = mod.connect(str(bad))
        names = ["ok", "mismatch", "dup", "novec", "mega", "rescued", "blind", "stale",
                 "notc", "orphansrc"]
        paths = {n: bad / ("%s - 2024 - 首次访谈.md" % n) for n in names}
        for p in paths.values():
            p.write_text("x", encoding="utf-8")
        _insert_doc(db, paths["ok"], "c" * 64, plain_chunks, mod.PARSER_TOKEN)
        _insert_doc(db, paths["mismatch"], "d" * 64,
                    [("[00:01:00] A", 1.0,
                      "A03：内容够长够长的内容内容内容内容内容内容内容内容内容内容内容内容。")],
                    mod.PARSER_TOKEN, chunk_count=99)
        _insert_doc(db, paths["dup"], "c" * 64, plain_chunks, mod.PARSER_TOKEN)   # 同 sha256
        db.execute("INSERT INTO chunks(doc_id,section,weight,seq,text,para_start,para_end,"
                   "page_start,page_end) VALUES(9999,'orphan',1.0,0,?,1,1,NULL,NULL)",
                   ("x" * 100,))                                                   # 悬空块
        _insert_doc(db, paths["novec"], "e" * 64,
                    [("[00:02:00] B", 1.0, "A03：这段没有向量，向量路检索不到它。" * 3)],
                    mod.PARSER_TOKEN, with_vecs=False)
        _insert_doc(db, paths["mega"], "f" * 64,
                    [("[00:03:00] C", 1.0, "他讲到这一段的时候停顿了很久。" * 200)],
                    mod.PARSER_TOKEN)
        _insert_doc(db, paths["rescued"], "g" * 64,
                    [("Appendix (rescued)", 1.0, "A03：这段本来被判成附录，是兜底救回来的。" * 3)],
                    mod.PARSER_TOKEN)
        _insert_doc(db, paths["blind"], "h" * 64,
                    [("Appendix", 0.0, "A03：全是附录，没有正文权重。" * 4)],
                    mod.PARSER_TOKEN)
        _insert_doc(db, paths["stale"], "i" * 64, plain_chunks, "3.1.0/rev4")
        _insert_doc(db, paths["notc"], "k" * 64,
                    [("Body", 1.0, "这段文本既没有时间码也没有说话人标签，用来验证覆盖率检查。" * 3)],
                    mod.PARSER_TOKEN)
        _insert_doc(db, bad / "gone - 2024 - 首次访谈.md", "j" * 64, plain_chunks,
                    mod.PARSER_TOKEN, title=None, authors=None, year=None)          # 路径失效
        db.commit()
        db.close()

        st2, items2 = verify(bad, bad, mod)
        codes = {i["code"] for i in items2}
        for want, label in [
            ("E_CHUNK_COUNT_MISMATCH", "② chunk_count 不一致"),
            ("E_ORPHAN_CHUNKS", "③ 悬空块"),
            ("E_MISSING_VECS", "④ weight>0 无向量"),
            ("E_DUP_SHA256", "⑤ 同 sha256 重复"),
            ("E_BLIND_DOC", "⑥ 盲文档"),
            ("E_MEGA_CHUNK", "⑦ 巨型分块"),
            ("E_RESCUED", "⑧ (rescued) 兜底痕迹"),
            ("E_DANGLING_PATH", "⑨ 路径失效"),
            ("E_MISSING_SOURCE", "⑩ 磁盘有、库里没有的转写稿"),
            ("W_STALE_DOCS", "⑪ 陈旧解析器版本"),
            ("W_META_GAP", "⑫ 元数据缺失"),
            ("W_ZERO_WEIGHT", "⑬ weight<=0 的非 References 块"),
            ("W_NO_TIMECODE", "⑭ 无时间码"),
        ]:
            add_check(label, want in codes, "codes=%s" % sorted(codes))
        add_check("⑮ 引擎判定 stale_kind = chunk", st2["stale_kind"] == "chunk",
                  "stale_kind=%s revs=%s" % (st2["stale_kind"], st2["stale_revs"]))
        add_check("⑯ 未入库文件计数正确", st2.get("src_missing") == 1,
                  "src_missing=%s src_files=%s" % (st2.get("src_missing"), st2.get("src_files")))
        add_check("⑰ 缺陷库确有 ERROR", any(i["sev"] == "E" for i in items2), "")

        # ---- 不存在的路径：绝不能创建任何东西 ----
        ghost = tmp / "no_such_dir"
        st3, items3 = verify(ghost, None, mod)
        add_check("⑱ 路径不存在时只报错、**不创建任何文件**",
                  (not ghost.exists()) and any(i["code"] == "E_DB_MISSING" for i in items3),
                  "exists=%s codes=%s" % (ghost.exists(), [i["code"] for i in items3]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, good, _ in checks if good)
    for name, good, info in checks:
        print("%s %s" % ("PASS" if good else "FAIL", name))
        if not good and info:
            print("     %s" % info)
    print("\n自测：%d/%d 通过" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="访谈材料知识库结构验收（只读）")
    ap.add_argument("--kb-root", help="知识库目录（**直接包含 kb.sqlite**，通常是 <项目>/.kb）")
    ap.add_argument("--src", help="转写稿源目录（对账：磁盘有、库里没有）")
    ap.add_argument("--out", help="Markdown 报告输出路径")
    ap.add_argument("--json", help="同时输出机读 JSON")
    ap.add_argument("--engine", help="kb_engine.py 路径（默认自动定位）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.kb_root:
        ap.print_help()
        return 2

    engine = find_engine(args.engine)
    mod = load_engine(engine)
    kb_root = Path(args.kb_root)
    src = Path(args.src) if args.src else None
    print("engine : %s (VERSION %s / rev%d)" % (engine, mod.VERSION, mod.PARSER_REV))
    stats, items = verify(kb_root, src, mod)
    print("db     : %s" % stats.get("db_file"))
    print("docs=%s chunks=%s vecs=%s stale=%s kind=%s"
          % (stats.get("docs"), stats.get("chunks"), stats.get("vecs"),
             stats.get("stale_docs"), stats.get("stale_kind")))
    for it in items:
        if it["sev"] != "I":
            print("  [%s] %s" % (it["sev"], it["code"]))
    out = Path(args.out) if args.out else kb_root / "访谈库验收报告.md"
    tot = render_report(stats, items, engine, mod, out)
    print("total  : E=%d W=%d I=%d" % (tot["E"], tot["W"], tot["I"]))
    print("report : %s" % out)
    if args.json:
        Path(args.json).write_text(json.dumps({"stats": stats, "findings": items},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        print("json   : %s" % args.json)
    print("(console CJK may be mojibake; read the report file)")
    return 1 if tot["E"] else 0


if __name__ == "__main__":
    sys.exit(main())
