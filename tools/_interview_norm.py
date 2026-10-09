#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_interview_norm.py — 访谈转写稿：规范模板生成 + 入库前规范化

解决的问题（都是 kb_engine 分块器造成的静默失败，见 _interview_check.py）
=========================================================================
| 症状 | 引擎原因 | 本工具的修法 |
| --- | --- | --- |
| 整节材料不进库 | 标题/行首命中 weight=0（附录/致谢/参考文献） | `--fix-headings zero`：改成「（附录）…」——关键词不在行首即不命中 |
| 短轮次小节消失 | 章节正文 < 40 字符被丢弃 | `--merge-tiny`：把该节段落并进相邻的、正文足够的节 |
| 一个上万字的巨型分块 | 超长段落里中文句末标点后无空白，split_long 无切点 | `--split-sentences`：超长段落按句末标点补换行（引擎 `clean()` 把换行折成空格 → 就成为切点；**只在标点后补空白，不增删任何文字**） |
| 一行一轮、段落结构不清 | 段落由空行界定 | `--split-lines`（默认开）：每个非空行独立成段，版面规范化 |
| 正文段落被当标题、首词被吃掉 | 短段落以「结果/总结/背景…」开头 | `--fix-false-heading`：加说话人前缀（`A03：`），首词不再是章节名 |
| 时间码进不了 section，无法按时段过滤 | section 只来自标题行 | `--sectionize`：按时间码自动分节（每节 ≥800 字符，不会被短节规则丢弃） |
| 静默乱码 | 编码不在 utf-8/gb18030 内走 replace | 统一转 UTF-8；源编码用 `--decode-as` 强制指定 |

三条纪律
========
1. **不覆盖原稿**：写 `--out DIR`；不加 `--apply` 只出计划（dry-run）。
2. **任何改写都记账**：逐文件列出「标题改写/加前缀/并节/切段/转编码」的条数。
3. **不假装能修**：修不了的情况（如整篇只有一个小节、且不足 40 字符）明确记为
   「无法自动修复」，绝不悄悄留个坑。

用法
====
  python _interview_norm.py template --out "<项目>\\转写稿" --code A03 \\
         --date 2024-03-11 --session 首次访谈 --apply
  python _interview_norm.py fix --in "<项目>\\转写稿" --out "<项目>\\转写稿_规范化"          # 看计划
  python _interview_norm.py fix --in ... --out ... --apply --sectionize --plan 计划.md      # 落盘
  python _interview_norm.py fix --in ... --out ... --decode-as big5 --apply                 # 非 UTF-8 源
  python _interview_norm.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".markdown"}
ENGINE_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
SPEAKER_RE = re.compile(
    r"^\s*(?:[\[\(【]?\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\]\)】]?\s*)?([^\s：:，,。]{1,12})\s*[：:]\s*\S")
HEADING_RE = re.compile(r"^(\s*#{1,6})\s*(.+?)\s*$")
TC_LEAD_RE = re.compile(r"^\s*[\[\(【]?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*[\]\)】]?")
ZERO_WEIGHT_HEAD = ("附录", "致谢", "参考文献", "引用文献")
ZERO_WEIGHT_HEAD_EN = ("references", "bibliography", "appendix", "acknowledg")
ACADEMIC_HEAD = ("摘要", "引言", "背景", "概述", "方法", "实验", "结果", "讨论",
                 "结论", "总结", "小结")
NUM_PREFIX = r"^\s*(?:\d+(?:\.\d+)*[\.\)、]?|[一二三四五六七八九十百]+[、．.])\s*"
TIMECODE_PAREN_RE = re.compile(r"[（(【\[]?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*[）)】\]]")


def find_engine(explicit=None):
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if os.environ.get("KB_ENGINE"):
        cands.append(Path(os.environ["KB_ENGINE"]))
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
        return None
    uniq.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return uniq[0]


def load_engine(path):
    spec = importlib.util.spec_from_file_location("kb_engine_for_norm", str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def decode_text(raw: bytes, decode_as=None):
    if decode_as:
        try:
            return raw.decode(decode_as), decode_as, False
        except (UnicodeDecodeError, LookupError):
            return None, decode_as, False
    for enc in ENGINE_ENCODINGS:
        try:
            return raw.decode(enc), enc, False
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)", True


def encoding_suspect(raw: bytes, engine_enc) -> list[str]:
    """引擎会按 gb18030 解，但 big5/cp932 也能解 → 可能误判成乱码。"""
    if engine_enc and engine_enc.startswith("utf-8"):
        return []
    others = []
    for enc in ("big5", "big5hkscs", "cp932", "utf-16"):
        try:
            raw.decode(enc)
            others.append(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return others


# ── 结构模型 ────────────────────────────────────────────────────────────────
def parse_blocks(text: str, split_lines: bool):
    """[{'heading': str|None, 'paras': [str]}]。split_lines=True：每非空行各自成段
    （口语转写稿常态：一行一轮）。False：连续行并成一段。"""
    sections = [{"heading": None, "paras": []}]
    for line in text.splitlines():
        if not line.strip():
            continue
        m = HEADING_RE.match(line)
        if m and m.group(1).strip().startswith("#"):
            sections.append({"heading": line.rstrip(), "paras": []})
            continue
        cur = sections[-1]
        if split_lines or not cur["paras"]:
            cur["paras"].append(line.strip())
        else:
            cur["paras"][-1] = cur["paras"][-1] + " " + line.strip()
    return [s for s in sections if s["heading"] is not None or s["paras"]]


def body_of(sec, mod) -> str:
    if not sec["paras"]:
        return ""
    joined = " ".join(sec["paras"])
    return mod.clean(joined) if mod is not None else joined.strip()


def line_keyword(line: str):
    """行首（去编号前缀后）命中的类别：'zero' / 'academic' / None。"""
    s = re.sub(NUM_PREFIX, "", line.strip())
    s = re.sub(r"^#{1,6}\s*", "", s).strip()
    for kw in ZERO_WEIGHT_HEAD:
        if s.startswith(kw):
            return "zero"
    low = s.lower()
    for kw in ZERO_WEIGHT_HEAD_EN:
        if low.startswith(kw):
            return "zero"
    for kw in ACADEMIC_HEAD:
        if s.startswith(kw):
            return "academic"
    return None


def neutralize(line: str) -> str:
    """行首关键词挪进括号：『附录：X』→『（附录）X』；保留 Markdown 标题标记。"""
    m = HEADING_RE.match(line)
    marks, s = (m.group(1) + " ", m.group(2)) if m else ("", line.strip())
    for kw in ZERO_WEIGHT_HEAD + ACADEMIC_HEAD:
        if s.startswith(kw):
            return "%s（%s）%s" % (marks, kw, s[len(kw):].lstrip("：: ·、"))
    low = s.lower()
    for kw in ZERO_WEIGHT_HEAD_EN:
        if low.startswith(kw):
            return "%s（%s）%s" % (marks, kw, s[len(kw):].lstrip(": ·、"))
    return line


def fmt_tc(m) -> str:
    h, mi, sec = int(m.group(1)), m.group(2), m.group(3)
    return "[%02d:%s]" % (h, mi) if sec is None else "[%02d:%s:%s]" % (h, mi, sec)


def sectionize(paras: list[str], min_chars: int):
    """按行首时间码切节：累计正文 ≥ min_chars 且遇到带时间码的行就开新节。
    每节 ≥ min_chars（远大于 40），因此不会被引擎的『正文<40 丢弃』规则吃掉。"""
    out, cur = [], {"heading": None, "paras": []}
    for p in paras:
        m = TC_LEAD_RE.match(p)
        if cur["paras"] and m and sum(len(x) for x in cur["paras"]) >= min_chars:
            out.append(cur)
            cur = {"heading": None, "paras": []}
        if not cur["paras"] and m:
            cur["heading"] = "## %s" % fmt_tc(m)
        cur["paras"].append(p)
    if cur["paras"]:
        out.append(cur)
    return out


def plan(text: str, opts, mod):
    """返回 (新文本, 变更记录)。所有改写保守、可解释、可审计。"""
    ch = {"timecode": 0, "heading": 0, "merged_tiny": 0, "split_turns": 0,
          "false_heading": 0, "skipped_false_heading": 0, "sectionized": 0,
          "split_sentences": 0, "notes": []}
    sections = parse_blocks(text, split_lines=opts.split_lines)

    # ① 时间码规范化
    if opts.fix_timecode:
        counter = {"n": 0}

        def rep(m):
            new = fmt_tc(m) if m.group(3) is not None else "[%s:%s]" % (m.group(1), m.group(2))
            if m.group(0).strip() != new:
                counter["n"] += 1
            return new

        def norm_tc(s):
            return TIMECODE_PAREN_RE.sub(rep, s)

        for sec in sections:
            if sec["heading"]:
                sec["heading"] = norm_tc(sec["heading"])
            sec["paras"] = [norm_tc(p) for p in sec["paras"]]
        ch["timecode"] = counter["n"]

    # ② 行首关键词括号化（标题行 + 正文行都管；正文行受引擎 ≤80 字符判定约束）
    if opts.fix_headings != "off":
        for sec in sections:
            if sec["heading"]:
                k = line_keyword(sec["heading"])
                if k == "zero" or (k == "academic" and opts.fix_headings == "all"):
                    new = neutralize(sec["heading"])
                    if new != sec["heading"]:
                        ch["notes"].append("标题改写：%s → %s" % (sec["heading"], new))
                        sec["heading"] = new
                        ch["heading"] += 1
            for i, p in enumerate(sec["paras"]):
                k = line_keyword(p)
                if not (k == "zero" or (k == "academic" and opts.fix_headings == "all")):
                    continue
                if len(re.sub(NUM_PREFIX, "", p.strip())) > 80:
                    continue          # 引擎对非 # 行有 80 字符上限，不会当标题
                new = neutralize(p)
                if new != p:
                    sec["paras"][i] = new
                    ch["heading"] += 1
                    ch["notes"].append("行首关键词括号化：%s…" % p.strip()[:30])

    # ③ 按时间码分节（仅对没有 Markdown 标题的稿子）
    if opts.sectionize and not any(s["heading"] for s in sections):
        flat = [p for s in sections for p in s["paras"]]
        new_secs = sectionize(flat, opts.section_min_chars)
        if len(new_secs) > 1:
            sections = new_secs
            ch["sectionized"] = len(new_secs)
            ch["notes"].append("按时间码分成 %d 节（每节 ≥%d 字符）"
                               % (len(new_secs), opts.section_min_chars))

    # ④ 过短小节并入相邻节（优先前一个正文足够的节，其次后一节；都没有则如实报告）
    if opts.merge_tiny:
        for i, sec in enumerate(sections):
            if not sec["heading"]:
                continue
            b = len(body_of(sec, mod))
            if not (0 < b < 40):
                continue
            tgt = None
            for j in range(i - 1, -1, -1):
                if len(body_of(sections[j], mod)) >= 40:
                    tgt = j
                    break
            if tgt is not None:
                sections[tgt]["paras"].extend(sec["paras"])
                ch["notes"].append("过短小节并入前一节（删标题）：%s（%d 字符）"
                                   % (sec["heading"], b))
            else:
                nxt = next((j for j in range(i + 1, len(sections))
                            if sections[j]["heading"] is not None or sections[j]["paras"]), None)
                if nxt is not None:
                    sections[nxt]["paras"] = sec["paras"] + sections[nxt]["paras"]
                    ch["notes"].append("过短小节并入后一节（删标题）：%s（%d 字符）"
                                       % (sec["heading"], b))
                else:
                    ch["notes"].append("**无法自动修复**：小节 %s 正文仅 %d 字符，且没有可并入"
                                       "的相邻节——需要人工补充内容或删掉该标题。"
                                       % (sec["heading"], b))
                    continue
            sec["heading"] = None
            sec["paras"] = []
            ch["merged_tiny"] += 1

    # ⑤ 正文段落被误判为标题 → 加说话人前缀
    last_speaker = None
    for sec in sections:
        for i, p in enumerate(sec["paras"]):
            m = SPEAKER_RE.match(p)
            if m:
                last_speaker = m.group(1)
                ch["split_turns"] += 1
                continue
            if not opts.fix_false_heading:
                continue
            probe = re.sub(NUM_PREFIX, "", p.strip())
            if len(probe) > 80:
                continue
            if line_keyword(p) is None:
                continue
            spk = last_speaker or opts.default_speaker
            if not spk:
                ch["skipped_false_heading"] += 1
                ch["notes"].append("疑似误判为标题但无说话人可用，已跳过：%s…" % p.strip()[:40])
                continue
            sec["paras"][i] = "%s：%s" % (spk, p)
            ch["false_heading"] += 1
            ch["notes"].append("加说话人前缀：%s：…" % spk)

    # ⑥ 超长段落按句末标点补换行 → 引擎 clean() 折成空格 → 成为 split_long 的切点。
    #    只插入空白，不增删任何文字（引擎检索出的块文本本来就是 clean() 归一化过的）。
    if opts.split_sentences:
        for sec in sections:
            for i, p in enumerate(sec["paras"]):
                if len(p) > opts.max_line_chars:
                    sec["paras"][i] = re.sub(r"(?<=[。；！？!?;])", "\n", p)
                    ch["split_sentences"] += 1
        if ch["split_sentences"]:
            ch["notes"].append("超长段落按句末标点补换行 %d 处（仅插入空白，不改文字）"
                               % ch["split_sentences"])

    # ⑦ 序列化：节标题 + 段间恰好一个空行（段边界 = split_long 的切点）
    out: list[str] = []
    for sec in sections:
        if sec["heading"]:
            out += [sec["heading"], ""]
        for p in sec["paras"]:
            out += [p, ""]
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n", ch


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def check_with_tool(path: Path, engine):
    """调用同目录 _interview_check.py 做事前/事后自检对比（可选）。"""
    if engine is None:
        return None
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import _interview_check as ic
        mod = ic.load_engine(engine)
        opts = argparse.Namespace(timecode_check=True, speaker_check=True, warn_academic=True)
        r = ic.check_file(Path(path), mod, opts)
        return {"E": sum(1 for i in r["findings"] if i["sev"] == "E"),
                "W": sum(1 for i in r["findings"] if i["sev"] == "W"),
                "codes": [i["code"] for i in r["findings"] if i["sev"] == "E"],
                "chunks": r["stats"].get("chunks"),
                "max_chunk": r["stats"].get("chunk_chars_max"),
                "sections": [s["section"] for s in r["sections"]]}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


# ── 模板 ─────────────────────────────────────────────────────────────────────
TEMPLATE_GUIDE = """模板使用规范（照这个格式写，几类静默失败都不会发生）

1. 文件名：受访者代号 - 年份 - 会话名 .md
   例：A03 - 2024 - 首次访谈.md
   → 引擎按 `^作者 - 年份 - 标题$` 自动解析 authors / year / title。

2. 小节标题：## [时:分:秒] 主题
   例：## [00:12:35] 家庭背景
   → section 字段直接带时间码，检索结果可见、可用 filters.section 定位；
     引用写成「[A03 2024-03-11, 00:12:35]」就能回查。

3. 每个说话轮次单独一行、单独一段（轮间空一行），以「说话人：」开头。
   例：  A03：[00:12:35] 你小时候家里几口人？
        B01：五口人，我排行老二。
   → 「说话人：」开头一次解决三件事：段落首词不是章节名（不会被误判成标题）、
     证据可归属到人、段边界成为切分点（不会产生巨型分块）。

4. 不要用这些词**开头**做标题：附录 / 致谢 / 参考文献（weight=0，整节丢弃）；
   也避开 摘要 / 引言 / 背景 / 结果 / 讨论 / 结论 / 总结 / 小结（会被映射成学术
   章节名并改变权重）。需要这类标题时写成「（附录）受访者情况表」。

5. 每节正文至少 40 字符（不足会被整节丢弃）。短促轮次要并进上一节。

6. 脱敏在入库之前做：入库后检索出的片段会进入云端模型 prompt。
"""


def make_template(out_dir: Path, code: str, date: str, session: str, apply: bool) -> Path:
    """文件名只用**年份**：`代号 - 年份 - 会话名`。
    为什么不用完整日期：引擎按 `^作者 - 年份 - 标题$` 解析，`A03 - 2024-03-11 - 首次访谈`
    会被解析成 year=2024、title=`03-11 - 首次访谈`（作者/标题全错）。完整日期放在 H1 里。"""
    m = re.search(r"(19|20)\d{2}", date or "")
    if not m:
        raise SystemExit("--date 里必须含四位年份，例如 2024-03-11")
    year = m.group(0)
    path = out_dir / ("%s - %s - %s.md" % (code, year, session))
    content = (
        "# %s - %s - %s\n\n"
        "## [00:00:00] 开场与知情同意\n\n"
        "%s：[00:00:00] \n\n"
        "B01：\n\n"
        "## [00:10:00] 小节标题（写清主题，便于按 section 过滤）\n\n"
        "%s：\n\n"
        "B01：\n" % (code, date, session, code, code))
    if apply:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return path


# ── 主流程 ───────────────────────────────────────────────────────────────────
def apply_fix(src: Path, dst: Path, opts, mod, engine=None, quiet=False) -> list[dict]:
    files = sorted([p for p in src.rglob("*") if p.is_file()
                    and p.suffix.lower() in TEXT_EXTS and not p.name.startswith("~$")])
    rename = {}
    if opts.rename_map:
        with open(opts.rename_map, encoding="utf-8-sig", newline="") as fh:
            for row in csv.reader(fh):
                row = [c.strip() for c in row if c.strip()]
                if len(row) >= 2:
                    rename[row[0]] = row[1]
    rows = []
    for p in files:
        raw = p.read_bytes()
        text, enc, replaced = decode_text(raw, opts.decode_as)
        suspect = encoding_suspect(raw, enc)
        entry = {"file": p.name, "encoding_in": enc, "decoded_with_replace": replaced,
                 "encoding_suspect": suspect, "out": None, "written": False,
                 "changes": {}, "notes": [], "check_before": None, "check_after": None}
        if text is None:
            entry["error"] = "无法按 %s 解码；请用 --decode-as 指定源编码" % opts.decode_as
            rows.append(entry)
            continue
        if suspect and not opts.decode_as:
            entry["notes"].append(
                "源编码可疑：引擎会按 %s 解，但 %s 也能解。若原文是繁体/Big5 等，"
                "**必须**用 --decode-as %s 重跑，否则输出的是乱码。"
                % (enc, "/".join(suspect), suspect[0]))
        new_text, ch = plan(text, opts, mod)
        entry["changes"] = ch
        entry["notes"] += ch["notes"]
        entry["chars_in"] = len(text)
        entry["chars_out"] = len(new_text)
        name = rename.get(p.name, p.name)
        if name != p.name and not re.match(r"^.+?\s*-\s*(?:19|20)\d{2}\s*-\s*.+$", Path(name).stem):
            entry["notes"].append("警告：新文件名不符合『作者 - 年份 - 标题』，元数据可能解析不出")
        out = dst / name
        entry["out"] = str(out)
        if engine is not None:
            entry["check_before"] = check_with_tool(p, engine)
        if opts.apply:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(new_text, encoding="utf-8", newline="\n")
            entry["written"] = True
            if engine is not None:
                entry["check_after"] = check_with_tool(out, engine)
        rows.append(entry)
        if not quiet:
            c = ch
            print("  %-42s 标题%d 前缀%d 并节%d 切段%d%s%s"
                  % (p.name[:42], c["heading"], c["false_heading"], c["merged_tiny"],
                     c["split_turns"], ("  节化%d" % c["sectionized"]) if c["sectionized"] else "",
                     ("  补换行%d" % c["split_sentences"]) if c["split_sentences"] else ""))
            if entry["check_before"] and entry["check_after"]:
                print("       自检 ERROR：%s → %s"
                      % (entry["check_before"].get("E"), entry["check_after"].get("E")))
    return rows


def render_plan(rows: list[dict], out: Path, applied: bool) -> None:
    L = ["# 访谈稿规范化%s" % ("报告（已落盘）" if applied else "计划（未落盘）"), "",
         "| 文件 | 源编码 | 标题/行首改写 | 加前缀 | 并节 | 切段 | 节化 | 输出 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        c = r.get("changes") or {}
        L.append("| %s | %s%s | %s | %s | %s | %s | %s | %s |"
                 % (r["file"], r["encoding_in"], "（可疑）" if r.get("encoding_suspect") else "",
                    c.get("heading", 0), c.get("false_heading", 0), c.get("merged_tiny", 0),
                    c.get("split_turns", 0), c.get("sectionized", 0) or "-",
                    Path(r["out"]).name if r.get("out") else "—"))
    L.append("")
    for r in rows:
        if r.get("notes") or r.get("error"):
            L += ["## %s" % r["file"], ""]
            if r.get("error"):
                L.append("- ❌ %s" % r["error"])
            for n in r.get("notes", []):
                L.append("- %s" % n)
            if r.get("check_before") or r.get("check_after"):
                L += ["", "自检对比：前 %s → 后 %s"
                      % (json.dumps(r.get("check_before"), ensure_ascii=False),
                         json.dumps(r.get("check_after"), ensure_ascii=False))]
            L.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")


# ── 自测 ─────────────────────────────────────────────────────────────────────
LONG = "他讲到这一段的时候停顿了很久，然后又接着说下去，语气比之前慢了些。"
# 口语转写稿的常态：无说话人标签的行、危险行首关键词、一行一轮、附录小节
BAD_SAMPLE = ("A07：访谈记录\n\n"
              "结果他就直接走了。后来我打电话他也没有接。\n"
              "总结这次访谈，我觉得整体气氛还是可以的，他比较放松。\n"
              + "A07：" + LONG * 7 + "\n"
              + "A07：" + LONG * 7 + "\n"
              + "A07：" + LONG * 7 + "\n\n"
              "附录：受访者基本情况表\n"
              "此表记录了受访者的年龄段、职业类别与居住地类型，以及访谈时长与录音文件编号，用于后续交叉分析。\n")
# ASR 常见的 blob：一整段 1500 字，句末标点后没有空白 → 巨型分块
BLOB_SAMPLE = "# A12 - 2024 - 首次访谈\n\n## [00:00:00] 一整段\n\n" + LONG * 50 + "\n"

TINY_SAMPLE = """# A09 - 2024 - 首次访谈

## [00:00:01] 开始

A09：嗯。

B03：对。

## [00:05:00] 家庭背景

A09：你小时候家里一共几口人，父母当时是做什么工作的，家里的日子过得怎么样呢？
"""

SOLO_TINY = """# A10 - 2024 - 首次访谈

## [00:00:01] 短节

A10：嗯。
"""

TC_SAMPLE = """[00:00:00] A11：我们开始吧，先聊一聊你早年的经历，想到什么就说什么，不用着急。
[00:01:00] B04：我小时候住在城郊，家里有五口人，父亲在供销社上班，母亲在家种地。
[00:06:30] A11：那你后来上学的情况呢，中间有没有中断过，还是说一直都在读书。
[00:09:00] B04：中学那几年正好赶上搬家，所以转过一次学，后来又回到原来的学校读书。
[00:14:20] A11：转学那段时间对你影响大吗，你自己当时是什么感受，家里人怎么看。
[00:17:00] B04：影响挺大的，主要是原来的朋友都不在一个班了，成绩也掉了一些，不过后来慢慢就适应了，也就没那么难受了。
"""


def selftest() -> int:
    print("== _interview_norm.py 自测 ==")
    engine = find_engine()
    if engine is None:
        print("FAIL 找不到 kb_engine.py")
        return 1
    mod = load_engine(engine)
    print("引擎：%s" % engine)
    checks: list[tuple[str, bool, str]] = []
    tmp = Path(tempfile.mkdtemp(prefix="ivnorm_"))

    def add_check(name, cond, info=""):
        checks.append((name, bool(cond), info))

    def ns(**kw):
        d = dict(apply=True, decode_as=None, split_lines=True, merge_tiny=True,
                 fix_headings="zero", fix_false_heading=True, fix_timecode=True,
                 default_speaker=None, rename_map=None, sectionize=False,
                 section_min_chars=800, split_sentences=False, max_line_chars=600)
        d.update(kw)
        return argparse.Namespace(**d)

    try:
        src = tmp / "src"
        src.mkdir(parents=True)
        (src / "A07 - 2024 - 记录.md").write_text(BAD_SAMPLE, encoding="utf-8")
        (src / "A09 - 2024 - 首次访谈.md").write_text(TINY_SAMPLE, encoding="utf-8")
        (src / "A10 - 2024 - 首次访谈.md").write_text(SOLO_TINY, encoding="utf-8")
        (src / "A11 - 2024 - 首次访谈.txt").write_text(TC_SAMPLE, encoding="utf-8")
        (src / "A12 - 2024 - 首次访谈.md").write_text(BLOB_SAMPLE, encoding="utf-8")
        (src / "A08 - 2024 - 首次访谈.txt").write_bytes(
            "他說的內容我記下來了，這是繁體中文測試，內容要夠長才能通過四十字符的限制。".encode("big5"))

        # ---- 模板 ----
        tpl = make_template(tmp / "tpl", "A03", "2024-03-11", "首次访谈", apply=True)
        add_check("① 模板文件名 =『代号 - 年份 - 会话名』（只用年份）",
                  tpl.name == "A03 - 2024 - 首次访谈.md", tpl.name)
        meta_t = mod.extract_meta(tpl, tpl.read_text("utf-8"), None)
        add_check("①a 模板元数据可解析：authors/year",
                  meta_t[1] == "A03" and meta_t[2] == 2024, "meta=%s" % (meta_t,))
        chk = check_with_tool(tpl, engine)
        add_check("①b 未填写的模板不产生任何可检索块（安全失败，不会污染库）",
                  chk and chk["chunks"] == 0 and "E_NO_CHUNKS" in (chk.get("codes") or []),
                  "chk=%s" % chk)

        # ---- 落盘 ----
        dst = tmp / "dst"
        rows = apply_fix(src, dst, ns(), mod, engine, quiet=True)
        add_check("② 6 个文件都有输出", len(rows) == 6 and all(r["out"] for r in rows),
                  "rows=%s" % [(r["file"], r["out"]) for r in rows])
        g = {r["file"][:3]: r for r in rows}

        add_check("③ 危险行首关键词被括号化", g["A07"]["changes"]["heading"] >= 1,
                  json.dumps(g["A07"]["changes"], ensure_ascii=False))
        add_check("④ 误判段落加了说话人前缀", g["A07"]["changes"]["false_heading"] >= 2,
                  json.dumps(g["A07"]["changes"], ensure_ascii=False))
        add_check("⑤ 过短小节被并入相邻节", g["A09"]["changes"]["merged_tiny"] == 1,
                  json.dumps(g["A09"]["changes"], ensure_ascii=False))
        add_check("⑥ 唯一过短节 → 如实报告『无法自动修复』",
                  g["A10"]["changes"]["merged_tiny"] == 0
                  and any("无法自动修复" in n for n in g["A10"]["notes"]),
                  json.dumps(g["A10"]["notes"], ensure_ascii=False))
        add_check("⑦ Big5 源：未被静默当作 UTF-8，且给出编码警告",
                  g["A08"]["encoding_in"] != "utf-8"
                  and (bool(g["A08"]["encoding_suspect"]) or g["A08"]["decoded_with_replace"]),
                  "enc_in=%s suspect=%s replace=%s" % (g["A08"]["encoding_in"],
                                                       g["A08"]["encoding_suspect"],
                                                       g["A08"]["decoded_with_replace"]))
        dst5 = tmp / "dst5"
        rows5 = apply_fix(src, dst5, ns(decode_as="big5"), mod, engine, quiet=True)
        g5 = next(r for r in rows5 if r["file"].startswith("A08"))
        txt5 = Path(g5["out"]).read_text("utf-8")
        add_check("⑦a --decode-as big5 解出正确繁体（无乱码）",
                  "他說的內容我記下來了" in txt5, "head=%s" % txt5[:40])

        # ---- ERROR 归零 ----
        add_check("⑧ A07 修复前有 ERROR", (g["A07"]["check_before"] or {}).get("E", 0) > 0,
                  "before=%s" % g["A07"]["check_before"])
        add_check("⑧a A07 修复后 ERROR 归零", (g["A07"]["check_after"] or {}).get("E") == 0,
                  "after=%s" % g["A07"]["check_after"])
        add_check("⑧b A07 修复后最大块 ≤1200",
                  ((g["A07"]["check_after"] or {}).get("max_chunk") or 0) <= 1200,
                  "after=%s" % g["A07"]["check_after"])
        add_check("⑨ A09 修复前有 ERROR、修复后归零",
                  (g["A09"]["check_before"] or {}).get("E", 0) > 0
                  and (g["A09"]["check_after"] or {}).get("E") == 0,
                  "before=%s after=%s" % (g["A09"]["check_before"], g["A09"]["check_after"]))

        # ---- 原文不丢字（逐行核对；唯一允许的改写是"关键词移进括号"）----
        def strip_kw(s):
            s = re.sub(NUM_PREFIX, "", s.strip())
            for kw in ZERO_WEIGHT_HEAD + ACADEMIC_HEAD + ZERO_WEIGHT_HEAD_EN:
                if s.lower().startswith(kw.lower()):
                    return s[len(kw):].lstrip("：: ·、")
            return s

        fixed = Path(g["A07"]["out"]).read_text("utf-8")
        sfixed = squash(fixed)
        lost = []
        for line in [l.strip() for l in BAD_SAMPLE.splitlines() if l.strip()]:
            if HEADING_RE.match(line):
                continue
            if squash(line) in sfixed or squash(strip_kw(line)) in sfixed:
                continue
            lost.append(line[:30])
        add_check("⑩ 原文每一行都在输出里（含被加前缀/被括号化的行）", not lost, "lost=%s" % lost)
        add_check("⑩a 没有任何净删字（本次改写只做插入）",
                  len(sfixed) >= len(squash(BAD_SAMPLE)),
                  "%d → %d" % (len(squash(BAD_SAMPLE)), len(sfixed)))
        add_check("⑩a 说话人标签未减少",
                  fixed.count("A07：") >= BAD_SAMPLE.count("A07："),
                  "%d → %d" % (BAD_SAMPLE.count("A07："), fixed.count("A07：")))
        add_check("⑩b 附录正文完整保留", squash("此表记录了受访者的年龄段") in squash(fixed), "")
        add_check("⑩c 原稿未被改动",
                  (src / "A07 - 2024 - 记录.md").read_text("utf-8") == BAD_SAMPLE, "")
        t9 = Path(g["A09"]["out"]).read_text("utf-8")
        add_check("⑩d A09 短节内容未丢",
                  squash("A09：嗯。") in squash(t9) and squash("B03：对。") in squash(t9), "")

        # ---- 按时间码分节 ----
        dst2 = tmp / "dst2"
        rows2 = apply_fix(src, dst2, ns(sectionize=True, section_min_chars=200), mod, engine,
                          quiet=True)
        g2 = {r["file"][:3]: r for r in rows2}
        add_check("⑪ 无标题稿按时间码分节", g2["A11"]["changes"]["sectionized"] >= 2,
                  json.dumps(g2["A11"]["changes"], ensure_ascii=False))
        secs = (g2["A11"]["check_after"] or {}).get("sections") or []
        add_check("⑪a 分节后 section 带时间码", any("[" in s for s in secs), "sections=%s" % secs)
        add_check("⑪b 分节后无 ERROR", (g2["A11"]["check_after"] or {}).get("E") == 0,
                  "after=%s" % g2["A11"]["check_after"])
        tc_text = Path(g2["A11"]["out"]).read_text("utf-8")
        add_check("⑪c 分节后原文未丢字",
                  all(squash(x) in squash(tc_text)
                      for x in [l.strip() for l in TC_SAMPLE.splitlines() if l.strip()]), "")

        # ---- blob 稿：超长单段 → 补换行后巨型分块消失 ----
        dst6 = tmp / "dst6"
        rows6 = apply_fix(src, dst6, ns(split_sentences=True, max_line_chars=600),
                          mod, engine, quiet=True)
        g6 = next(r for r in rows6 if r["file"].startswith("A12"))
        add_check("⑪d blob 稿修复前有 E_MEGA_CHUNK",
                  "E_MEGA_CHUNK" in ((g6["check_before"] or {}).get("codes") or []),
                  "before=%s" % g6["check_before"])
        add_check("⑪e blob 稿补换行后 ERROR 归零",
                  (g6["check_after"] or {}).get("E") == 0, "after=%s" % g6["check_after"])
        add_check("⑪f blob 稿补换行后最大块 ≤1200",
                  ((g6["check_after"] or {}).get("max_chunk") or 0) <= 1200,
                  "after=%s" % g6["check_after"])
        add_check("⑪g 补换行只插空白、文字零改动",
                  squash(Path(g6["out"]).read_text("utf-8")) == squash(BLOB_SAMPLE),
                  "chars %d → %d" % (g6["chars_in"], g6["chars_out"]))

        # ---- dry-run 不写 ----
        dst3 = tmp / "dst3"
        rows3 = apply_fix(src, dst3, ns(apply=False), mod, engine, quiet=True)
        add_check("⑫ dry-run 不创建任何输出",
                  (not dst3.exists()) and all(r["written"] is False for r in rows3),
                  "exists=%s" % dst3.exists())

        # ---- 改名映射 ----
        csv_path = tmp / "map.csv"
        csv_path.write_text("A07 - 2024 - 记录.md,A07 - 2024 - 首次访谈.md\n", encoding="utf-8")
        dst4 = tmp / "dst4"
        rows4 = apply_fix(src, dst4, ns(rename_map=csv_path), mod, engine, quiet=True)
        add_check("⑬ 改名映射生效",
                  any(Path(r["out"]).name == "A07 - 2024 - 首次访谈.md" for r in rows4),
                  "outs=%s" % [Path(r["out"]).name for r in rows4])

        # ---- 计划文件 ----
        pf = tmp / "plan.md"
        render_plan(rows, pf, True)
        add_check("⑭ 计划/报告文件写出",
                  pf.is_file() and "访谈稿规范化" in pf.read_text("utf-8"), "")
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

    ap = argparse.ArgumentParser(description="访谈转写稿规范化（不覆盖原稿）")
    sub = ap.add_subparsers(dest="cmd")

    t = sub.add_parser("template", help="生成规范转写稿骨架")
    t.add_argument("--out", required=True)
    t.add_argument("--code", required=True, help="受访者代号，如 A03")
    t.add_argument("--date", required=True, help="访谈日期，如 2024-03-11")
    t.add_argument("--session", default="首次访谈")
    t.add_argument("--apply", action="store_true")

    f = sub.add_parser("fix", help="规范化并输出副本")
    f.add_argument("--in", dest="src", required=True)
    f.add_argument("--out", dest="dst", required=True)
    f.add_argument("--apply", action="store_true", help="实际写文件（默认 dry-run）")
    f.add_argument("--decode-as", help="强制源编码（如 big5）")
    f.add_argument("--split-lines", action=argparse.BooleanOptionalAction, default=True,
                   help="每行独立成段（默认开）")
    f.add_argument("--merge-tiny", action=argparse.BooleanOptionalAction, default=True,
                   help="把正文<40字符的小节并入相邻节（默认开）")
    f.add_argument("--fix-headings", choices=["off", "zero", "all"], default="zero",
                   help="行首关键词括号化：zero=只处理 weight=0 的；all=连学术章节名一起")
    f.add_argument("--fix-false-heading", action=argparse.BooleanOptionalAction, default=True,
                   help="给被误判为标题的正文段落加说话人前缀（默认开）")
    f.add_argument("--default-speaker", help="没有可用说话人标签时的兜底前缀")
    f.add_argument("--fix-timecode", action="store_true", help="(00:12:35)【00:12:35】→[00:12:35]")
    f.add_argument("--split-sentences", action="store_true",
                   help="超长段落按句末标点补换行（修 blob 稿的巨型分块；只插空白不改文字）")
    f.add_argument("--max-line-chars", type=int, default=600,
                   help="触发补换行的段落长度阈值（默认 600）")
    f.add_argument("--sectionize", action="store_true",
                   help="无 Markdown 标题时按行首时间码自动分节")
    f.add_argument("--section-min-chars", type=int, default=800, help="分节的最小节长（默认 800）")
    f.add_argument("--rename-map", help="CSV：旧文件名,新文件名")
    f.add_argument("--plan", help="计划/报告写成 Markdown")
    f.add_argument("--json", help="计划/报告写成 JSON")

    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.cmd == "template":
        p = make_template(Path(args.out), args.code, args.date, args.session, args.apply)
        print("模板路径：%s（%s）" % (p, "已写入" if args.apply else "未写入，加 --apply 才落盘"))
        print()
        print(TEMPLATE_GUIDE)
        return 0
    if args.cmd == "fix":
        engine = find_engine()
        mod = load_engine(engine) if engine else None
        if mod is None:
            print("警告：找不到 kb_engine.py；<40 字符小节判定将退化为简单拼接")
        rows = apply_fix(Path(args.src), Path(args.dst), args, mod, engine)
        if args.plan:
            render_plan(rows, Path(args.plan), args.apply)
            print("plan   : %s" % args.plan)
        if args.json:
            Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            print("json   : %s" % args.json)
        bad = [r for r in rows if r.get("error")]
        unfixable = [r for r in rows if any("无法自动修复" in n for n in r.get("notes", []))]
        print("\nfiles  : %d（解码失败 %d，无法自动修复 %d）" % (len(rows), len(bad), len(unfixable)))
        print("mode   : %s" % ("已落盘" if args.apply else "dry-run（加 --apply 才写文件）"))
        print("out    : %s" % args.dst)
        return 1 if bad else 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
