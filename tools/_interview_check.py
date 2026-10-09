#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_interview_check.py — 访谈材料「入库前」自检（严格只读：不写库、不改原稿）

为什么需要它
============
kb_engine 的分块器是为**学术文献**写的：章节名表按论文体例、锚点是印刷页码、
超过 1200 字符按「句末标点 + 空白」切分。访谈转写稿（口语、中文、逐轮对话）
在上面会踩到几类**静默失败**——不报错、也没有告警，但材料不见了或无法引用：

  1. 段落/标题命中 SECTION_MAP 的 weight=0 项（附录／致谢／参考文献）
     → 该节**整节丢弃**。出处：kb_engine.chunk_document._emit()
        `if ww <= 0 and sec != "References": return []`
  2. 章节正文（拼接压平后）< 40 字符 → 丢弃。出处：同一函数 `if len(body) < 40`
     —— 访谈里「A：嗯。B：对。」这样的短轮次小节会整节消失。
  3. 中文段落在句末标点后没有空白 → split_long 找不到切点
     → 一个上万字的**巨型分块**（向量被整篇平均，检索精度下降）。
  4. 编码不在 utf-8-sig/utf-8/gb18030 之内 → 走 `errors="replace"` **静默乱码**。
     注意 gb18030 几乎能解出任何字节序列：Big5 稿会被"成功"解码成乱码而不报错。
  5. 短段落（≤80 字符）以「结果／背景／方法／讨论／总结…」开头 → 被
     match_section_prefix 当成章节标题：**首词被吃掉、section 被切换**。
     口语里「结果他就走了。」「总结这次访谈…」非常常见。

本工具的做法：**调用引擎自己的函数**（read_document / extract_meta / chunk_document /
match_section_prefix / clean）做干跑，并复刻 _ingest_file 的块内 sha1 去重，
因此预测结果与真实入库一致；但**不建库、不下载模型、不改原稿**。

用法
====
  python _interview_check.py <文件或目录> [--out 报告.md] [--json 结果.json]
  python _interview_check.py <目录> --no-timecode-check --no-speaker-check
  python _interview_check.py --selftest          # 自测（造 7 类样本并断言检查器能发现）
  python _interview_check.py <目录> --engine <kb_engine.py 路径>

退出码：0 = 无 ERROR；1 = 有 ERROR（整节丢弃/乱码/巨型分块等硬问题）
控制台中文可能因代码页显示为乱码，**以报告文件为准**。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".markdown", ".docx"}
ALL_EXTS = TEXT_EXTS | {".pdf"}

ENGINE_ENV = "KB_ENGINE"


# ── 引擎定位（不硬编码项目路径）─────────────────────────────────────────────
def find_engine(explicit: str | None = None) -> Path:
    """① --engine　② 环境变量 KB_ENGINE　③ ~/.dsh/profiles/*/node_modules/dsh-kb-rag/
    ④ ~/.dsh 下任意深度的 dsh-kb-rag；命中多个时取 mtime 最新。"""
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
    spec = importlib.util.spec_from_file_location("kb_engine_for_check", str(path))
    if spec is None or spec.loader is None:
        raise SystemExit("无法载入引擎：%s" % path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Findings:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, code: str, sev: str, msg: str, detail=None) -> None:
        if any(i["code"] == code for i in self.items):
            for i in self.items:
                if i["code"] == code:
                    i["msg"] += " " + msg
            return
        self.items.append({"code": code, "sev": sev, "msg": msg, "detail": detail})


# ── 段落 → 章节分组（复刻 chunk_document 的主循环，用于诊断"哪一节会消失"）──
def group_sections(text: str, mod) -> list[dict]:
    """返回 [{section, weight, body, source}]，source='#' 表示来自 Markdown 标题，
    'para' 表示被判成标题的正文段落（误判嫌疑）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[dict] = []
    cur = {"section": "Front matter", "weight": 1.0, "paras": [], "source": "init"}
    for p in paras:
        for j, seg in enumerate(mod.split_inline_headings(p)):
            hit = mod.match_section_prefix(seg, allow_long=(j > 0))
            if hit is not None:
                name, w, rest = hit
                out.append(cur)
                src = "#" if seg.lstrip().startswith("#") else "para"
                cur = {"section": name, "weight": w, "paras": ([rest] if rest else []),
                       "source": src}
            else:
                cur["paras"].append(seg)
    out.append(cur)
    for s in out:
        s["body"] = mod.clean(" ".join(s["paras"]))
        s["chars"] = len(s["body"])
    return out


# 编码：与引擎 read_document 完全相同的顺序（utf-8-sig → utf-8 → gb18030 → replace）
ENGINE_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
MOJIBAKE_CHARS = re.compile(r"[\u3400-\u4dbf\ue000-\uf8ff\ufffd]")


def probe_encoding(path: Path) -> dict:
    raw = path.read_bytes()
    chosen = None
    for enc in ENGINE_ENCODINGS:
        try:
            raw.decode(enc)
            chosen = enc
            break
        except UnicodeDecodeError:
            continue
    others = []
    for enc in ("big5", "big5hkscs", "cp932", "utf-16"):
        try:
            raw.decode(enc)
            others.append(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    text = raw.decode(chosen) if chosen else raw.decode("utf-8", errors="replace")
    return {"engine_encoding": chosen, "decoded_by_others": others, "bytes": len(raw),
            "bom": raw[:3] == b"\xef\xbb\xbf",
            "weird_chars": len(MOJIBAKE_CHARS.findall(text)), "chars": len(text)}


# 敏感信息：只报类型/计数/行号/掩码，不落原文
PII_PATTERNS = [
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("固话", re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("长数字串(银行卡/账号)", re.compile(r"(?<!\d)\d{13,19}(?!\d)")),
    ("地址触发词", re.compile(r"住在|家住|地址是|门牌|小区|号楼")),
]


def mask(s: str) -> str:
    if len(s) <= 4:
        return s[0] + "***"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def scan_pii(text: str) -> list[dict]:
    out = []
    claimed: list[tuple[int, int]] = []
    for name, pat in PII_PATTERNS:
        hits = []
        for m in pat.finditer(text):
            a, b = m.span()
            if any(not (b <= x or a >= y) for x, y in claimed):
                continue
            claimed.append((a, b))
            hits.append({"line": text.count("\n", 0, m.start()) + 1, "masked": mask(m.group(0))})
        if hits:
            out.append({"type": name, "count": len(hits), "samples": hits[:5]})
    return out


TIMECODE_RE = re.compile(r"[\[\(]?\b(\d{1,2}):(\d{2})(?::(\d{2}))\b[\]\)]?")
SPEAKER_RE = re.compile(r"^\s*([^\s：:，,。]{1,12})\s*[：:]\s*\S")


def split_point_stats(text: str, mod) -> dict:
    """用引擎的 clean() 归一化后统计句末标点（split_long 唯一可用的切点）。"""
    norm = mod.clean(text)
    return {"sentence_ends": len(re.findall(r"[。；！？!?;]", norm)),
            "sentence_ends_with_space": len(re.findall(r"[。；！？!?;]\s", norm))}


def check_file(path: Path, mod, opts) -> dict:
    f = Findings()
    res = {"path": str(path), "name": path.name, "findings": [], "stats": {}, "meta": {},
           "sections": [], "dropped": [], "report_ok": False}

    enc = probe_encoding(path)
    res["stats"]["encoding"] = enc
    if enc["engine_encoding"] is None:
        f.add("E_ENC_UNKNOWN", "E",
              "编码不在引擎支持范围（utf-8-sig/utf-8/gb18030）内 → 会走 errors='replace' "
              "**静默乱码**，入库文本不可用。",
              {"bytes": enc["bytes"], "decoded_by_others": enc["decoded_by_others"]})
    elif not enc["engine_encoding"].startswith("utf-8"):
        note = {"engine_encoding": enc["engine_encoding"]}
        if enc["decoded_by_others"]:
            note["also_decodable_as"] = enc["decoded_by_others"]
        f.add("W_ENC_NON_UTF8", "W",
              "引擎会按 %s 解码（不是 UTF-8）。gb18030 几乎能解出任何字节序列，"
              "**误判时不报错**——请人工确认没有乱码。建议统一转成 UTF-8 再入库。" % enc["engine_encoding"],
              note)
    if enc["weird_chars"] > 0:
        f.add("W_WEIRD_CHARS", "W",
              "文本含 %d 个私用区/扩展区/替换字符（乱码或复制残留的典型信号），建议人工抽查。"
              % enc["weird_chars"])

    try:
        text, pdf_meta = mod.read_document(path)
    except Exception as e:
        f.add("E_READ_FAIL", "E", "read_document 失败：%s: %s" % (type(e).__name__, e))
        res["findings"] = f.items
        return res
    if not text.strip():
        f.add("E_EMPTY_TEXT", "E", "提取不到任何文本（引擎会判 no text extracted 并跳过该文件）。")
        res["findings"] = f.items
        return res

    title, authors, year, journal, doi = mod.extract_meta(path, text, pdf_meta)
    fn_title = mod._filename_title(path.stem)
    fn_author = mod._filename_author(path.stem)
    res["meta"] = {"title": title, "authors": authors, "year": year, "journal": journal,
                   "doi": doi, "from_filename": {"title": fn_title, "authors": fn_author}}
    if title and fn_title and title.strip() != fn_title.strip():
        f.add("W_META_TITLE_FROM_BODY", "W",
              "title 取自正文启发式而非文件名 → 可能误取首行（如「访谈时间：…」）。"
              "建议文件名写成『受访者代号 - 2024 - 会话名』，让元数据可预测。",
              {"title": title, "filename_title": fn_title})
    if not (authors or year):
        f.add("W_META_THIN", "W",
              "authors / year 都为空 → 检索结果无法标注来源。建议用文件名命名法"
              "『受访者代号 - 2024 - 会话名』（引擎按 `^作者 - 年份 - 标题$` 自动解析）。")
    if doi:
        f.add("W_META_DOI", "W",
              "访谈稿抽出了 DOI『%s』——访谈材料不应有 DOI，多半是正文前 3000 字符里恰好"
              "出现了 10.xxxx/yyy 形式的字符串被误当标识符，请核对。" % doi)
    if year is not None and not (1900 <= year <= 2100):
        f.add("W_META_YEAR", "W", "year=%r 不像年份，建议核对。" % year)

    # ── 分块干跑（与 _ingest_file 同一条路径）──
    paras = (pdf_meta or {}).get("_paras")
    raw_chunks = mod.chunk_document(text, paras=paras)
    seen, chunks, dup = set(), [], 0
    for c in raw_chunks:
        h = hashlib.sha1(c[2].encode("utf-8")).hexdigest()
        if h in seen:
            dup += 1
            continue
        seen.add(h)
        chunks.append(c)

    lens = [len(c[2]) for c in chunks]
    sec_dist: dict[str, dict] = {}
    for sec, w, t, *_ in chunks:
        d = sec_dist.setdefault(sec, {"n": 0, "weight": w, "chars": 0})
        d["n"] += 1
        d["chars"] += len(t)
    res["sections"] = [{"section": k, **v} for k, v in
                       sorted(sec_dist.items(), key=lambda kv: -kv[1]["chars"])]
    res["stats"].update({
        "chars": len(text),
        "paras": len([p for p in re.split(r"\n\s*\n", text) if p.strip()]),
        "chunks": len(chunks), "dup_chunks_dropped": dup,
        "chunk_chars_min": min(lens) if lens else 0,
        "chunk_chars_max": max(lens) if lens else 0,
        "chunk_chars_avg": round(sum(lens) / len(lens)) if lens else 0,
        "positive_chunks": sum(1 for c in chunks if c[1] > 0),
        "null_pages": sum(1 for c in chunks if c[5] is None and c[6] is None),
        "split_points": split_point_stats(text, mod),
        "timecodes": [m.group(0) for m in TIMECODE_RE.finditer(text)],
        "speakers": [m.group(1) for m in
                     (SPEAKER_RE.match(ln) for ln in text.splitlines()) if m],
    })

    if not chunks:
        f.add("E_NO_CHUNKS", "E", "分块结果为 0 → 入库后该文件在检索里完全不存在。")
    elif res["stats"]["positive_chunks"] == 0:
        f.add("E_BLIND_DOC", "E",
              "没有任何 weight>0 的分块 → 整篇不可检索（盲文档）；连引擎的"
              "『整篇不可检索兜底』都没救回来。")
    if any("(rescued)" in s["section"] for s in res["sections"]):
        f.add("E_RESCUED", "E",
              "触发了『整篇不可检索兜底』（section 带 (rescued) 后缀）：全篇曾被判定为无正文，"
              "内容被按权重 1.0 救援写回。section 语义已不可信——请检查标题里是否出现"
              "『附录／致谢／参考文献／references』。")

    mega = [c for c in chunks if len(c[2]) > 1200]
    if mega:
        sp = res["stats"]["split_points"]
        f.add("E_MEGA_CHUNK", "E",
              "有 %d 个分块超过 1200 字符（最长 %d）→ 中文长段落未被切开。引擎按"
              "「句末标点后紧跟空白」切分（split_long），中文正文里「。」后通常没有空格，"
              "所以切不开。本次：句末标点 %d 处，其后有空白 %d 处。"
              % (len(mega), max(len(c[2]) for c in mega),
                 sp["sentence_ends"], sp["sentence_ends_with_space"]),
              [{"section": c[0], "chars": len(c[2]), "head": c[2][:60] + "…"} for c in mega[:5]])
    elif res["stats"]["split_points"]["sentence_ends"] and \
            res["stats"]["split_points"]["sentence_ends_with_space"] == 0:
        f.add("I_NO_SPLIT_POINT", "I",
              "句末标点后没有任何空白（无 split_long 切点），但本次没有产生 >1200 的块——"
              "说明各节都不长，暂时安全。材料变长后会变成巨型分块，建议现在就按"
              "『每轮一行 + 轮间空行 + 轮末带标点』排版。")

    # ── 章节级静默丢弃（weight=0 / 正文<40 字符）──
    groups = group_sections(text, mod)
    zero_secs, tiny_secs, false_heads, long_heads, academic = [], [], [], [], []
    dropped_chars = 0
    for g in groups:
        if g["source"] == "para" and g["paras"]:
            rest_head = g["body"][:20]
            if re.search(r"[。！？，、；：]", rest_head):
                false_heads.append({"section": g["section"], "weight": g["weight"],
                                    "head": rest_head})
        if g["source"] == "#":
            s = g["section"]
            if len(s) >= 40:
                long_heads.append({"section": s[:60], "chars": len(s)})
        if g["weight"] <= 0 and g["section"] != "References":
            if g["chars"]:
                zero_secs.append({"section": g["section"], "chars": g["chars"]})
                dropped_chars += g["chars"]
        elif 0 < g["chars"] < 40:
            tiny_secs.append({"section": g["section"], "chars": g["chars"],
                              "body": g["body"][:40]})
            dropped_chars += g["chars"]
        elif g["weight"] > 0 and g["source"] == "para":
            academic.append({"section": g["section"], "weight": g["weight"]})

    if zero_secs:
        f.add("E_ZERO_WEIGHT_SECTION", "E",
              "有 %d 节命中 weight=0 的章节（附录／致谢／参考文献，非 References 本体）"
              "→ **整节丢弃**（不是降权，是不进库），共 %d 字符。"
              % (len(zero_secs), sum(s["chars"] for s in zero_secs)), zero_secs[:8])
    if tiny_secs:
        f.add("E_TINY_SECTION", "E",
              "有 %d 节的正文不足 40 字符 → 引擎 `if len(body) < 40: return []` "
              "**整节丢弃**，共 %d 字符。访谈里短轮次小节（「A：嗯。」「B：对。」）"
              "会整节消失。修法：把这类短节并进相邻小节，或补足内容。" % (len(tiny_secs),
              sum(s["chars"] for s in tiny_secs)), tiny_secs[:8])
    if false_heads:
        f.add("E_FALSE_HEADING", "E",
              "有 %d 处**正文段落**被当成章节标题（首词被吃掉、section 被切换）。口语转写稿里"
              "很常见（「结果他就走了。」命中『结果』；「总结这次访谈…」命中『总结』）。"
              "修法：每轮以「说话人：」开头（如 `A03：结果他就走了。`）——段落首词不再是"
              "章节名，判定不再触发，同时获得了说话人归属。" % len(false_heads),
              false_heads[:8])
    if long_heads:
        f.add("W_HEADING_TRUNCATED", "W",
              "%d 个 Markdown 标题超过 40 字符 → 引擎只取前 40 字符作 `section`，"
              "过滤时要用截断后的值。" % len(long_heads), long_heads[:5])
    if academic and opts.warn_academic:
        f.add("I_ACADEMIC_SECTION", "I",
              "有 %d 节被判成学术章节名（同时改变 section 名与权重，如「背景」→Introduction、"
              "「实验结果…」→Methods 权重 ×1.2）。访谈材料里这通常不是你想要的。"
              % len(academic), academic[:8])
    res["dropped"] = zero_secs + tiny_secs

    # ── 锚点与说话人 ──
    tc = res["stats"]["timecodes"]
    if opts.timecode_check:
        if not tc:
            f.add("W_NO_TIMECODE", "W",
                  "全文没有时间码。访谈材料的引用锚点只能是时间码——txt/md 的 "
                  "page_start/page_end 恒为 NULL，页码这条路不存在。建议写成 `[00:12:35]`，"
                  "或放进小节标题 `## [00:12:35] 受访者A·家庭背景`（section 就会带时间码）。")
        elif res["stats"]["null_pages"] == len(chunks) and chunks:
            f.add("I_ANCHOR_TIMECODE", "I",
                  "时间码 %d 处（首个 %s，末个 %s）；page 字段 %d/%d 全为 NULL"
                  "（文本类文件必然如此）——引用请用时间码。"
                  % (len(tc), tc[0], tc[-1], res["stats"]["null_pages"], len(chunks)))
    spk = res["stats"]["speakers"]
    if opts.speaker_check:
        if not spk:
            f.add("W_NO_SPEAKER", "W",
                  "没有可识别的说话人标签（形如「A03：」）。检索命中后无法判断是谁说的，"
                  "而访谈研究的证据必须能归属到人。")
        else:
            uniq = sorted(set(spk))
            f.add("I_SPEAKER", "I", "说话人标签 %d 行、%d 人：%s"
                  % (len(spk), len(uniq), "、".join(uniq[:8])))

    pii = scan_pii(text)
    if pii:
        f.add("W_PII", "W",
              "检出可能的可识别个人信息 %d 类，**建议先脱敏再入库**（检索片段会进入"
              "云端模型 prompt）。" % len(pii), pii)

    if not re.match(r"^.+?\s*-\s*(?:19|20)\d{2}\s*-\s*.+$", path.stem) and not fn_author:
        f.add("W_FILENAME", "W",
              "文件名不符合引擎的元数据解析习惯，authors/year 可能为空。"
              "建议『受访者代号 - 2024 - 首次访谈』（正则 `^作者 - 年份 - 标题$`）。")

    res["findings"] = f.items
    res["report_ok"] = True
    return res


def render_report(results: list[dict], engine: Path, mod, out: Path, opts) -> dict:
    tot = {"files": len(results), "E": 0, "W": 0, "I": 0, "chunks": 0, "chars": 0,
           "dropped_chars": 0, "dropped_files": 0}
    body_lines: list[str] = []
    for r in results:
        items = r.get("findings") or []
        for it in items:
            tot[it["sev"]] += 1
        st = r.get("stats", {})
        tot["chunks"] += st.get("chunks", 0)
        tot["chars"] += st.get("chars", 0)
        tot["dropped_chars"] += sum(d.get("chars", 0) for d in (r.get("dropped") or []))
        if r.get("dropped"):
            tot["dropped_files"] += 1
        badge = "❌" if any(i["sev"] == "E" for i in items) else (
            "⚠️" if any(i["sev"] == "W" for i in items) else "✅")
        body_lines.append("---\n")
        body_lines.append("## %s %s" % (badge, r["name"]))
        body_lines.append("")
        if not r.get("report_ok"):
            for it in items:
                body_lines.append("- **%s** %s" % (it["code"], it["msg"]))
            body_lines.append("")
            continue
        m = r["meta"]
        if r.get("dropped"):
            body_lines.append("> ⚠️ **本节材料会被静默丢弃 %d 处**，共 %d 字符。"
                              % (len(r["dropped"]), sum(d.get("chars", 0) for d in r["dropped"])))
            body_lines.append("")
        body_lines.append("| 项 | 值 |")
        body_lines.append("| --- | --- |")
        body_lines.append("| 字符数 / 段落数 | %s / %s |" % (st["chars"], st["paras"]))
        body_lines.append("| 分块数（去重后）/ 其中 weight>0 | %s / %s |"
                          % (st["chunks"], st["positive_chunks"]))
        body_lines.append("| 块长 min / avg / max | %s / %s / %s |"
                          % (st["chunk_chars_min"], st["chunk_chars_avg"], st["chunk_chars_max"]))
        body_lines.append("| page 字段全 NULL 的块 | %s / %s |"
                          % (st["null_pages"], st["chunks"]))
        body_lines.append("| 句末标点 / 其后有空白 | %s / %s |"
                          % (st["split_points"]["sentence_ends"],
                             st["split_points"]["sentence_ends_with_space"]))
        body_lines.append("| 时间码 / 说话人 | %s 处 / %s 个 |"
                          % (len(st["timecodes"]), len(set(st["speakers"]))))
        body_lines.append("| 预测 title | %s |" % (m["title"] or "（空）"))
        body_lines.append("| 预测 authors / year | %s / %s |"
                          % (m["authors"] or "（空）", m["year"] if m["year"] else "（空）"))
        body_lines.append("| 预测 doi / journal | %s / %s |"
                          % (m["doi"] or "（空）", m["journal"] or "（空）"))
        body_lines.append("| 编码 | %s%s |"
                          % (st["encoding"]["engine_encoding"] or "**不可识别**",
                             "（含 BOM）" if st["encoding"]["bom"] else ""))
        body_lines.append("")
        if r["sections"]:
            body_lines.append("**section 分布（干跑结果）**")
            body_lines.append("")
            body_lines.append("| section | 块数 | 权重 | 字符数 |")
            body_lines.append("| --- | --- | --- | --- |")
            for s in r["sections"]:
                body_lines.append("| `%s` | %d | %s | %d |"
                                  % (s["section"], s["n"], s["weight"], s["chars"]))
            body_lines.append("")
        if r.get("dropped"):
            body_lines.append("**将被丢弃的节**")
            body_lines.append("")
            for d in r["dropped"][:10]:
                body_lines.append("- `%s`（%d 字符）%s"
                                  % (d.get("section"), d.get("chars", 0),
                                     ("— " + d.get("body", "")) if d.get("body") else ""))
            body_lines.append("")
        if items:
            body_lines.append("**发现**")
            body_lines.append("")
            for it in items:
                icon = {"E": "❌", "W": "⚠️", "I": "ℹ️"}[it["sev"]]
                body_lines.append("- %s **%s** %s" % (icon, it["code"], it["msg"]))
                if it.get("detail"):
                    d = it["detail"]
                    seq = d if isinstance(d, list) else [d]
                    for x in seq[:8]:
                        body_lines.append("  - `%s`" % json.dumps(x, ensure_ascii=False)[:220])
            body_lines.append("")
        else:
            body_lines.append("未发现问题。")
            body_lines.append("")

    head = [
        "# 访谈材料入库前自检报告",
        "",
        "- 引擎：`%s`（VERSION %s / PARSER_REV %d / SCHEMA_VERSION %s）"
        % (engine, mod.VERSION, mod.PARSER_REV, mod.SCHEMA_VERSION),
        "- 支持扩展名：%s" % ", ".join(sorted(mod.SUPPORTED_EXTS)),
        "- 检查方式：调用引擎 read_document / extract_meta / chunk_document / "
        "match_section_prefix 干跑，并复刻 _ingest_file 的块内 sha1 去重；"
        "**未入库、未下载模型、未改原稿**",
        "- **合计**：%d 个文件；❌ %d / ⚠️ %d / ℹ️ %d；预测 %d 块 / %d 字符；"
        "%d 个文件存在静默丢弃（合计 %d 字符）"
        % (tot["files"], tot["E"], tot["W"], tot["I"], tot["chunks"], tot["chars"],
           tot["dropped_files"], tot["dropped_chars"]),
        "",
    ]
    out.write_text("\n".join(head + body_lines), encoding="utf-8")
    return tot


# ── 自测 ─────────────────────────────────────────────────────────────────────
FIXTURE_CLEAN_MD = """# A03 - 2024 - 首次访谈

## [00:00:10] 开场与知情同意

A03：我先说明一下，这次访谈完全是自愿参加的，任何问题你都可以不回答，随时可以停下来，也不会有任何影响。
B01：好的，我明白，那我们开始吧，我会尽量把当时的情况讲清楚。

## [00:03:20] 家庭背景

A03：你小时候家里一共几口人，父母当时是做什么工作的，家里的日子过得怎么样？
B01：五口人，我排行老二，父亲在供销社上班，母亲在家种地，日子不算宽裕但也能过得去。
"""

FIXTURE_PLAIN_TXT = """访谈记录 未整理
今天聊了很多内容。他说了不少关于家里的事情。我记录了主要的意思。
后来又谈到工作。他换过几次工作。每次都是自己决定的。这里没有空行分隔。
"""

FIXTURE_APPENDIX = """# A04 - 2024 - 首次访谈

## [00:00:05] 开场

A04：我们开始吧，先聊一聊你早年的经历，想到什么就说什么，不用着急。

## 附录：受访者基本情况表

此表记录了受访者的年龄段、职业类别与居住地类型，以及访谈时长与录音文件编号，用于后续交叉分析。
"""

FIXTURE_MEGA = "# A05 - 2024 - 首次访谈\n\n" + "".join(
    "他讲到这一段的时候停顿了很久，然后又接着说下去，语气比之前慢了些。" for _ in range(60))

FIXTURE_FALSE_HEAD = """# A06 - 2024 - 首次访谈

结果他就直接走了，后来我打电话他也没有接。

总结这次访谈，我觉得整体气氛还是可以的，他比较放松。
"""

FIXTURE_TINY = """# A09 - 2024 - 首次访谈

## [00:00:01] 开始

A09：嗯。
B03：对。
"""

FIXTURE_PII = """# A07 - 2024 - 首次访谈

## [00:00:02] 联系方式

A07：方便留个联系方式吗？以后可能需要补充访谈，我会提前跟你约时间。
B02：可以，手机 13812345678，邮箱 someone@example.com，我住在朝阳区。
"""


def selftest() -> int:
    print("== _interview_check.py 自测 ==")
    engine = find_engine()
    mod = load_engine(engine)
    print("引擎：%s" % engine)
    print("VERSION %s / PARSER_REV %d" % (mod.VERSION, mod.PARSER_REV))
    tmp = Path(tempfile.mkdtemp(prefix="ivcheck_"))
    checks: list[tuple[str, bool, str]] = []
    opts = argparse.Namespace(timecode_check=True, speaker_check=True, warn_academic=True)

    def add_check(name, cond, info=""):
        checks.append((name, bool(cond), info))

    try:
        (tmp / "A03 - 2024 - 首次访谈.md").write_text(FIXTURE_CLEAN_MD, encoding="utf-8")
        (tmp / "未整理记录.txt").write_text(FIXTURE_PLAIN_TXT, encoding="utf-8")
        (tmp / "A04 - 2024 - 首次访谈.md").write_text(FIXTURE_APPENDIX, encoding="utf-8")
        (tmp / "A05 - 2024 - 首次访谈.md").write_text(FIXTURE_MEGA, encoding="utf-8")
        (tmp / "A06 - 2024 - 首次访谈.md").write_text(FIXTURE_FALSE_HEAD, encoding="utf-8")
        (tmp / "A07 - 2024 - 首次访谈.md").write_text(FIXTURE_PII, encoding="utf-8")
        (tmp / "A09 - 2024 - 首次访谈.md").write_text(FIXTURE_TINY, encoding="utf-8")
        (tmp / "A08 - 2024 - 首次访谈.txt").write_bytes(
            "他說的內容我記下來了，這是繁體中文測試。".encode("big5"))

        files = sorted([p for p in tmp.iterdir() if p.suffix.lower() in ALL_EXTS])
        results = [check_file(p, mod, opts) for p in files]

        def get(name):
            return next(x for x in results if x["name"] == name)

        def codes(name):
            return [i["code"] for i in get(name)["findings"]]

        def sev(name):
            return {i["sev"] for i in get(name)["findings"]}

        r1 = get("A03 - 2024 - 首次访谈.md")
        add_check("① 规范稿无 ERROR（不误报）", "E" not in sev("A03 - 2024 - 首次访谈.md"),
                  "codes=%s" % codes("A03 - 2024 - 首次访谈.md"))
        add_check("①a section 带时间码",
                  all("[" in s["section"] for s in r1["sections"]),
                  "sections=%s" % [s["section"] for s in r1["sections"]])
        add_check("①b 元数据取自文件名（authors/year）",
                  r1["meta"]["authors"] == "A03" and r1["meta"]["year"] == 2024,
                  "meta=%s" % r1["meta"])
        add_check("①c 说话人识别 ≥4 行",
                  len(r1["stats"]["speakers"]) >= 4, "speakers=%s" % r1["stats"]["speakers"])

        r2 = get("未整理记录.txt")
        add_check("② 无标题纯文本 → section 全为 Body",
                  bool(r2["sections"]) and all(s["section"] == "Body" for s in r2["sections"]),
                  "sections=%s" % [s["section"] for s in r2["sections"]])
        add_check("②a 触发无时间码告警", "W_NO_TIMECODE" in codes("未整理记录.txt"), "")

        add_check("③ 附录标题 → E_ZERO_WEIGHT_SECTION",
                  "E_ZERO_WEIGHT_SECTION" in codes("A04 - 2024 - 首次访谈.md"),
                  "codes=%s" % codes("A04 - 2024 - 首次访谈.md"))
        add_check("③a 附录节确实不在分块里",
                  all("附录" not in s["section"] for s in get("A04 - 2024 - 首次访谈.md")["sections"]),
                  "sections=%s" % [s["section"] for s in get("A04 - 2024 - 首次访谈.md")["sections"]])

        add_check("④ 无切点长文 → E_MEGA_CHUNK",
                  "E_MEGA_CHUNK" in codes("A05 - 2024 - 首次访谈.md"),
                  "codes=%s" % codes("A05 - 2024 - 首次访谈.md"))
        mega_len = get("A05 - 2024 - 首次访谈.md")["stats"]["chunk_chars_max"]
        add_check("④a 确认块长 >1200", mega_len > 1200, "max=%s" % mega_len)

        add_check("⑤ 正文段落以『结果/总结』开头 → E_FALSE_HEADING",
                  "E_FALSE_HEADING" in codes("A06 - 2024 - 首次访谈.md"),
                  "codes=%s" % codes("A06 - 2024 - 首次访谈.md"))
        add_check("⑤a 两处都被识别",
                  len([i for i in get("A06 - 2024 - 首次访谈.md")["findings"]
                       if i["code"] == "E_FALSE_HEADING"][0]["detail"]) >= 2, "")

        add_check("⑥ 短轮次小节 → E_TINY_SECTION",
                  "E_TINY_SECTION" in codes("A09 - 2024 - 首次访谈.md"),
                  "codes=%s" % codes("A09 - 2024 - 首次访谈.md"))
        add_check("⑥a 短节确实没有进分块",
                  all("开始" not in s["section"] for s in get("A09 - 2024 - 首次访谈.md")["sections"]),
                  "sections=%s" % [s["section"] for s in get("A09 - 2024 - 首次访谈.md")["sections"]])

        add_check("⑦ 手机号/邮箱 → W_PII", "W_PII" in codes("A07 - 2024 - 首次访谈.md"),
                  "codes=%s" % codes("A07 - 2024 - 首次访谈.md"))
        pii = next(i for i in get("A07 - 2024 - 首次访谈.md")["findings"] if i["code"] == "W_PII")
        masked = " ".join(s["masked"] for d in pii["detail"] for s in d["samples"])
        add_check("⑦a 详情里不含完整手机号", "13812345678" not in masked, "masked=%s" % masked)

        add_check("⑧ Big5 文件 → W_ENC_NON_UTF8",
                  "W_ENC_NON_UTF8" in codes("A08 - 2024 - 首次访谈.txt"),
                  "codes=%s" % codes("A08 - 2024 - 首次访谈.txt"))

        out = tmp / "report.md"
        tot = render_report(results, engine, mod, out, opts)
        add_check("⑨ 报告写出且标题正确",
                  out.is_file() and out.read_text("utf-8").startswith("# 访谈材料入库前自检报告"),
                  "tot=%s" % tot)
        add_check("⑨a 报告里合计含丢弃字符数", tot["dropped_chars"] > 0,
                  "dropped_chars=%s" % tot["dropped_chars"])
        add_check("⑨b 报告不含完整手机号",
                  "13812345678" not in out.read_text("utf-8"), "")
        add_check("⑨c dropped_files 统计到 3 个（附录/短节）",
                  tot["dropped_files"] >= 2, "dropped_files=%s" % tot["dropped_files"])
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
    # 控制台多为 GBK（Windows 中文）：不可编码字符会让 print 抛 UnicodeEncodeError，
    # 把一次成功的检查变成崩溃。这里降级为替换字符，报告文件仍是完整 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="访谈材料入库前自检（只读；用引擎函数做干跑）")
    ap.add_argument("target", nargs="?", help="待检查的文件或目录")
    ap.add_argument("--out", help="Markdown 报告输出路径（默认 <target>/入库前自检报告.md）")
    ap.add_argument("--json", help="同时输出机读 JSON 结果")
    ap.add_argument("--engine", help="kb_engine.py 路径（默认自动定位）")
    ap.add_argument("--no-timecode-check", action="store_true")
    ap.add_argument("--no-speaker-check", action="store_true")
    ap.add_argument("--no-academic-warn", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.target:
        ap.print_help()
        return 2

    engine = find_engine(args.engine)
    mod = load_engine(engine)
    target = Path(args.target)
    if target.is_dir():
        files = sorted([p for p in target.rglob("*") if p.is_file()
                        and p.suffix.lower() in ALL_EXTS and not p.name.startswith("~$")])
    elif target.is_file():
        files = [target]
    else:
        print("路径不存在：%s" % target)
        return 2
    if not files:
        print("未找到可检查的文本文件（支持 %s）" % ", ".join(sorted(ALL_EXTS)))
        return 2

    opts = argparse.Namespace(timecode_check=not args.no_timecode_check,
                              speaker_check=not args.no_speaker_check,
                              warn_academic=not args.no_academic_warn)
    print("engine : %s (VERSION %s / rev%d)" % (engine, mod.VERSION, mod.PARSER_REV))
    print("files  : %d" % len(files))
    results = []
    for p in files:
        r = check_file(p, mod, opts)
        results.append(r)
        errs = [i["code"] for i in (r.get("findings") or []) if i["sev"] == "E"]
        mark = "ERR " if errs else ("WARN" if r.get("findings") else "OK  ")
        print("  [%s] %-46s chunks=%-4s dropped=%s"
              % (mark, p.name[:46], (r.get("stats") or {}).get("chunks", "-"),
                 len(r.get("dropped") or [])))
        for c in errs:
            print("         %s" % c)

    out = Path(args.out) if args.out else (
        (target if target.is_dir() else target.parent) / "入库前自检报告.md")
    tot = render_report(results, engine, mod, out, opts)
    print("\ntotal  : E=%d W=%d I=%d chunks=%d chars=%d dropped_files=%d dropped_chars=%d"
          % (tot["E"], tot["W"], tot["I"], tot["chunks"], tot["chars"],
             tot["dropped_files"], tot["dropped_chars"]))
    print("report : %s" % out)
    if args.json:
        Path(args.json).write_text(
            json.dumps({"engine": str(engine), "totals": tot, "files": results},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("json   : %s" % args.json)
    print("(console CJK may be mojibake; read the report file)")
    return 1 if tot["E"] else 0


if __name__ == "__main__":
    sys.exit(main())
