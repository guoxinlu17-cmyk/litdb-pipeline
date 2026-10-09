# -*- coding: utf-8 -*-
"""从知识库正文中抽取「被引文献」，汇总生成二级文献数据库。

思路：库内全部文献的脚注/参考文献/夹注里，写着它们各自引用的一手与二手文献。
把这些引用点抽出来、归一化、去重并按被引次数排序，就得到一份"二级文献"清单——
用于判断哪些经典/论文在您的研究领域被反复引证，值得优先读原文。

输出（默认写入 05_文献数据库/）：
    二级文献数据库.json   结构化记录
    二级文献数据库.csv    便于 Excel 打开
    二级文献数据库.md     人读报告（统计 + Top 榜 + 分来源清单 + 局限说明）

注意：**全部条目都是"候选"**，来自正则抽取，题录字段可能有误（尤其作者与刊名）。
引用前须回查原刊；本文件只用于检索线索与阅读优先级判断。

用法：
    python -B _build_second_level_db.py [--kb-root <path>] [--outdir <path>] [--min-len 6]
"""
import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import db_dir, kb_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DEFAULT_KB = kb_dir()
DEFAULT_OUT = db_dir()

# 参考文献/注释类章节的信号词
REF_SECTION = re.compile(r"参考文献|注释|引用|书目|references|bibliography|notes|征引", re.IGNORECASE)

# —— 中文·专著：作者：《书名》，出版地：出版社，年份 ——
PAT_BOOK_CN = re.compile(
    r"([\u4e00-\u9fff·]{2,6})(?:著|编|主编)?[：:]\s*《([^》]{2,60})》"
    r"[，,]\s*([^，,。；;]{0,24}?)[：:]\s*([^，,。；;]{0,24}?)[，,]\s*(1[89]\d{2}|20\d{2})\s*年?"
)
# —— 中文·论文（人文样式）：作者：《题名》，《刊名》年份第N期 ——
PAT_JOURNAL_CN = re.compile(
    r"([\u4e00-\u9fff·]{2,6})?[：:]?\s*《([^》]{2,60})》[，,]\s*《([^》]{2,40})》\s*"
    r"(1[89]\d{2}|20\d{2})\s*年\s*(?:第\s*(\d+)\s*期)?"
)
# —— 中文·GB/T 样式：作者. 题名[J]. 刊名, 年, 卷(期): 页. ——
PAT_GB = re.compile(
    r"([\u4e00-\u9fff·A-Za-z ,\.]{2,40})\.\s*([^\.\[\]]{4,80})\[([JMDCN])\]\.\s*"
    r"([^,，]{2,40})[，,]\s*(1[89]\d{2}|20\d{2})"
)
# —— 英文·论文：Author, "Title," Journal, vol. x (Year): pages ——
PAT_EN_ARTICLE = re.compile(
    r"([A-Z][A-Za-z\.\- ]{2,40}),\s*[\"“]([^\"”]{4,90})[\"”][,，]?\s*"
    r"([A-Z][A-Za-z&\.\- ]{2,60})?,?\s*(?:vol\.\s*\d+)?\s*\(?(1[89]\d{2}|20\d{2})\)?"
)
# —— 英文·专著：Author, Title (City: Publisher, Year) ——
PAT_EN_BOOK = re.compile(
    r"([A-Z][A-Za-z\.\- ]{2,40}),\s*([A-Z][^()]{4,80})\s*\(([^():]{2,40}):\s*([^(),]{2,40}),\s*(1[89]\d{2}|20\d{2})\)"
)

PATTERNS = [
    ("专著", PAT_BOOK_CN, ("author", "title", "place", "publisher", "year")),
    ("期刊论文", PAT_JOURNAL_CN, ("author", "title", "journal", "year", "issue")),
    ("GB题录", PAT_GB, ("author", "title", "kind", "journal", "year")),
    ("英文论文", PAT_EN_ARTICLE, ("author", "title", "journal", "year")),
    ("英文专著", PAT_EN_BOOK, ("author", "title", "place", "publisher", "year")),
]

NORM = re.compile(r"[\s《》〈〉\"'“”‘’·,，.。:：;；、\-—_()（）\[\]【】]+")

# OCR 文本常在汉字之间插入空格（如"示例人物的憧 憬与困窘"），会打断正则匹配；
# 匹配前先去掉「汉字/汉字」「汉字/中文标点」之间的空白，保留拉丁词之间的空格。
CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CJK_SPACE_1 = re.compile(rf"(?<=[{CJK}])[ \t]+(?=[{CJK}])")
CJK_SPACE_2 = re.compile(rf"(?<=[{CJK}])[ \t]+(?=[，。、；：（）《》“”‘’！？])")
CJK_SPACE_3 = re.compile(rf"(?<=[，。、；：（）《》“”‘’！？])[ \t]+(?=[{CJK}])")


def normalize_ocr_spaces(text: str) -> str:
    """去掉 OCR 造成的字间空格，便于正则抽取（仅影响匹配，不改写原文件）。"""
    if not text:
        return ""
    text = CJK_SPACE_1.sub("", text)
    text = CJK_SPACE_2.sub("", text)
    return CJK_SPACE_3.sub("", text)

# 引文前缀/后缀噪声（"参见<某作者>"、"收入<某编者>编"、"转引自…" 等）
PREFIX_NOISE = re.compile(r"^(参见|参看|见|引自|转引自|收入|载|收录于|参见氏著|详见|又见|同前引|同上引)")
SUFFIX_NOISE = re.compile(r"(编|著|主编|译|撰|等)$")


def clean_name(text: str) -> str:
    name = PREFIX_NOISE.sub("", (text or "").strip())
    name = SUFFIX_NOISE.sub("", name).strip()
    return name


def clean_title(text: str) -> str:
    title = PREFIX_NOISE.sub("", (text or "").strip())
    return title.strip("《》〈〉\"'“”")


def norm_title(text: str) -> str:
    return NORM.sub("", text or "").lower()


def sentence_of(text: str, start: int, end: int) -> str:
    """取匹配所在的句子（用于人工复核原文片段）。"""
    left = max(text.rfind("\n", 0, start), text.rfind("。", 0, start), text.rfind("；", 0, start))
    right = len(text)
    for mark in ("\n", "。", "；"):
        idx = text.find(mark, end)
        if idx != -1:
            right = min(right, idx)
    return text[left + 1:right + 1].strip()[:220]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-root", default=DEFAULT_KB)
    ap.add_argument("--outdir", default=DEFAULT_OUT)
    ap.add_argument("--min-len", type=int, default=6, help="题名最短长度（过滤噪声）")
    a = ap.parse_args()

    db = Path(a.kb_root) / "kb.sqlite"
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        print(f"知识库不存在：{db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.id AS chunk_id, COALESCE(c.section,'') AS section, COALESCE(c.text,'') AS text,
               d.path AS doc_path, COALESCE(d.title,'') AS doc_title, COALESCE(d.year,'') AS doc_year
        FROM chunks c JOIN docs d ON d.id = c.doc_id
        WHERE LENGTH(COALESCE(c.text,'')) > 40
    """).fetchall()
    conn.close()
    print(f"扫描块数：{len(rows):,}")

    records: dict[str, dict] = {}
    per_doc = Counter()
    kind_counter = Counter()
    scanned_chunks = 0

    for row in rows:
        # OCR 文本常有字间空格，匹配前做归一（不改写原文件，只影响抽取）
        text = normalize_ocr_spaces(row["text"])
        section = row["section"] or ""
        looks_like_refs = bool(REF_SECTION.search(section)) or bool(REF_SECTION.search(text[:120]))
        for kind, pattern, fields in PATTERNS:
            for m in pattern.finditer(text):
                groups = m.groups()
                data = {k: (groups[i] or "").strip() if i < len(groups) else "" for i, k in enumerate(fields)}
                title = data.get("title", "")
                if len(title) < a.min_len:
                    continue
                year = data.get("year", "")
                if year and not (1800 <= int(year) <= 2030):
                    continue
                key = norm_title(title) + "|" + (year or "")
                if len(key) < 6:
                    continue
                rec = records.get(key)
                snippet = sentence_of(text, m.start(), m.end())
                if rec is None:
                    rec = {
                        "key": key, "kind": kind, "title": clean_title(title), "author": clean_name(data.get("author", "")),
                        "journal": data.get("journal", "").strip(), "publisher": clean_name(data.get("publisher", "")),
                        "place": data.get("place", "").strip(), "year": year, "issue": data.get("issue", ""),
                        "cited_by": [], "raw": snippet, "in_ref_section": looks_like_refs,
                    }
                    records[key] = rec
                else:
                    if not rec["author"] and data.get("author"):
                        rec["author"] = clean_name(data["author"])
                    if not rec["journal"] and data.get("journal"):
                        rec["journal"] = data["journal"].strip()
                    rec["in_ref_section"] = rec["in_ref_section"] or looks_like_refs
                src = row["doc_path"].split("\\")[-1]
                if src not in rec["cited_by"]:
                    rec["cited_by"].append(src)
                    per_doc[src] += 1
                kind_counter[kind] += 1
        scanned_chunks += 1

    items = sorted(records.values(), key=lambda r: (-len(r["cited_by"]), r["title"]))
    for it in items:
        it["cited_count"] = len(it["cited_by"])

    (outdir / "二级文献数据库.json").write_text(
        json.dumps({"generated": __import__("time").strftime("%Y-%m-%d %H:%M"),
                    "kb_root": str(db), "entries": items}, ensure_ascii=False, indent=2), encoding="utf-8")

    with (outdir / "二级文献数据库.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["被引次数", "类型", "作者", "题名", "刊名/出版社", "年份", "期", "来源文献", "原文片段"])
        for it in items:
            writer.writerow([it["cited_count"], it["kind"], it["author"], it["title"],
                             it["journal"] or it["publisher"], it["year"], it["issue"],
                             "；".join(it["cited_by"][:4]), it["raw"]])

    by_kind = Counter(it["kind"] for it in items)
    multi = [it for it in items if it["cited_count"] >= 2]
    lines = [
        "# 二级文献数据库（从库内文献的被引情况汇总）",
        "",
        f"- 生成时间：{__import__('time').strftime('%Y-%m-%d %H:%M')}｜数据源：`{db}`",
        f"- 扫描分块：**{scanned_chunks:,}**｜抽取候选条目：**{len(items):,}**（去重后）",
        f"- 被 ≥2 篇库内文献引用的条目：**{len(multi):,}**",
        "- ⚠️ **全部为机器抽取的候选**：题名/作者/刊名可能有误，引用前须回查原刊；本表用于发现线索与判断阅读优先级。",
        "",
        "## 一、按类型分布",
        "",
        "| 类型 | 条数 |",
        "| --- | --- |",
    ]
    for kind, n in by_kind.most_common():
        lines.append(f"| {kind} | {n} |")
    lines += ["", "## 二、被引最多的条目（Top 40）", "",
              "| 被引 | 类型 | 作者 | 题名 | 刊名/出版社 | 年份 |", "| --- | --- | --- | --- | --- | --- |"]
    for it in items[:40]:
        title = it["title"][:60] + ("…" if len(it["title"]) > 60 else "")
        lines.append(f"| {it['cited_count']} | {it['kind']} | {it['author'][:14]} | {title} | "
                     f"{(it['journal'] or it['publisher'])[:26]} | {it['year']} |")
    lines += ["", "## 三、产出这些引用的来源文献（Top 20）", "",
              "| 来源文献 | 抽出的候选条目数 |", "| --- | --- |"]
    for doc, n in per_doc.most_common(20):
        lines.append(f"| {doc[:70]} | {n} |")
    lines += ["", "## 四、使用与局限", "",
              "- 来源是各文献的脚注/文末参考文献/夹注，因此**年代分布**反映的是研究领域的引证习惯，而非某一年的出版量。",
              "- 中文人文类引用常省略刊名或出版社，仅凭正则无法完全还原；同一著作的不同版本可能被计为同一条（按题名+年份去重）。",
              "- 建议用法：先看 Top 40 判断「哪些是领域内公认的必读文献」，再回查原刊核对题录；"
              "本库不替代知网/原刊的核对。",
              ""]
    (outdir / "二级文献数据库.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"完成：{outdir}\\二级文献数据库.{{json,csv,md}}")
    print(f"  候选条目 {len(items):,}（其中被 ≥2 篇引用 {len(multi):,}）｜类型分布 {dict(by_kind)}")
    print("  Top 5：")
    for it in items[:5]:
        print(f"    ×{it['cited_count']}  [{it['kind']}] {it['author']} 《{it['title'][:40]}》 {it['year']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
