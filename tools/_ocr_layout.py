# -*- coding: utf-8 -*-
"""版式预检与分类：在正式 OCR 之前先"看清这篇文章长什么样"。

要解决的问题
------------
中文期刊/书籍的版面差异极大，用一种读法通吃必然出错：

  * 单栏 vs 双栏 vs 三栏（双栏若按行读会左右交错，逐字引文将永远搜不到）；
  * 标题/摘要**通栏**、正文分栏（同一页两种栏宽）——第 2 页起才分栏也很常见；
  * 书籍有**页眉/页脚**（页眉里常印着页码！）与**页下注**（小字、被横线隔开）；
  * 竖排古籍：从右到左、从上到下；
  * 注释体系：①②③ 页下注 / [1] 尾注 / （作者，年）夹注——决定了引用该怎么抓。

本模块先做几何与文本两层预检，给出**版式档案（layout profile）**，
再由 OCR 主程序按档案选择读法。预检只抽样若干页，成本很低。

用法（独立预检）：
    python -B _ocr_layout.py <pdf> [--pages 3] [--dpi 200] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# ── 注释与引用格式的识别规则（顺序即优先级，命中即止）────────────────────
NOTE_MARKERS = [
    ("圈码页下注", re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮]")),
    ("方括号顺序编码", re.compile(r"\[\d{1,3}\]")),
    ("圆括号数字注", re.compile(r"（\d{1,2}）|\(\d{1,2}\)")),
    ("作者-年份夹注", re.compile(r"[（(][\u4e00-\u9fff]{2,4}[，,]\s*(?:19|20)\d{2}[a-z]?[）)]")),
    ("文中尾注编号", re.compile(r"(?<![\d.])[1-9]\d?(?=\s|$)", re.M)),
]
CITE_PATTERNS = [
    ("期刊论文", re.compile(r"《[^》]{2,40}》[^。；]{0,40}?(?:19|20)\d{2}\s*年\s*第\s*\d+\s*期")),
    ("专著", re.compile(r"《[^》]{2,40}》[^。；]{0,30}?(?:北京|上海|南京|天津|广州|成都|武汉|香港|台北|北京大学出版社|商务印书馆|中华书局|三联书店)")),
    ("出版社+年", re.compile(r"[^\s，。]{2,20}出版社[，,]\s*(?:19|20)\d{2}\s*年")),
    ("页码", re.compile(r"第\s*\d{1,4}\s*[—\-－~]\s*\d{1,4}\s*页|第\s*\d{1,4}\s*页")),
]
SECTION_HINTS = {
    "脚注区": re.compile(r"(脚注|注释|注\s*[：:]|①|\[\d+\])"),
    "尾注区": re.compile(r"^\s*(注\s*释|参考文献|引文|References)\s*$", re.M),
    "摘要区": re.compile(r"(摘\s*要|内容提要|Abstract)"),
    "关键词区": re.compile(r"(关键词|关键字|Key\s*words?)"),
    "基金区": re.compile(r"(基金项目|国家社会科学基金|教育部人文社会科学)"),
    "作者简介": re.compile(r"(作者简介|作者单位|【作者)"),
}


@dataclass
class Box:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str
    score: float = 0.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def boxes_from_result(result) -> list[Box]:
    """rapidocr 结果 → Box 列表。"""
    out: list[Box] = []
    for it in result or []:
        try:
            box, text = it[0], it[1]
            score = float(it[2]) if len(it) > 2 else 0.0
        except (IndexError, TypeError):
            continue
        if not text or not str(text).strip():
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        out.append(Box(min(xs), max(xs), min(ys), max(ys), str(text).strip(), score))
    return out


# ─────────────────────────────────────────────────────────────────────────
# 几何层：栏数、通栏元素、页眉页脚、页下注带
# ─────────────────────────────────────────────────────────────────────────
def detect_columns(boxes: list[Box], W: float, max_cols: int = 3) -> dict:
    """判定栏数。返回 {cols, gutters, col_of_box, full_width_ids}。"""
    if not boxes:
        return {"cols": 1, "gutters": [], "col_of_box": {}, "full_width_ids": set()}
    full = {i for i, b in enumerate(boxes) if b.w > W * 0.55}       # 通栏元素（标题/摘要）
    normal = [b for i, b in enumerate(boxes) if i not in full]
    if len(normal) < 6:
        return {"cols": 1, "gutters": [], "col_of_box": {}, "full_width_ids": full}

    def gutter_count(x: float) -> int:
        """有多少"普通框"横跨 x —— 分栏线处应接近 0。"""
        return sum(1 for b in normal if b.x0 < x < b.x1)

    best = {"cols": 1, "gutters": [], "score": None}
    for cols in (2, 3):
        cands = ([0.5] if cols == 2 else [1 / 3, 2 / 3])
        xs = [W * c for c in cands]
        counts = [gutter_count(x) for x in xs]
        # 分栏线处越空越好；同时左右两侧都要有足量框
        sides = []
        for x in xs:
            sides.append((sum(1 for b in normal if b.cx < x), sum(1 for b in normal if b.cx > x)))
        if any(c > max(1, len(normal) * 0.05) for c in counts):
            continue
        if any(min(l, r) < 5 for l, r in sides):
            continue
        score = sum(counts)
        if best["score"] is None or score < best["score"]:
            best = {"cols": cols, "gutters": xs, "score": score}

    if best["cols"] == 1:
        return {"cols": 1, "gutters": [], "col_of_box": {}, "full_width_ids": full}

    gutters = best["gutters"]
    col_of_box = {}
    for i, b in enumerate(boxes):
        if i in full:
            col_of_box[i] = -1                       # 通栏
            continue
        col_of_box[i] = sum(1 for g in gutters if b.cx > g)
    return {"cols": best["cols"], "gutters": gutters, "col_of_box": col_of_box,
            "full_width_ids": full}


def norm_head(text: str) -> str:
    """页眉归一：去掉数字（页码逐页变化）、空白与标点，只留下固定的刊名/书名部分。"""
    t = re.sub(r"\d+", "", text or "")
    return re.sub(r"[\s\u3000·・、，。：:；;（）()\[\]【】\-—–_/|]+", "", t).strip()


def top_candidates(boxes: list[Box], H: float, max_lines: int = 2, max_len: int = 34) -> list[int]:
    """页面上缘、与下方有明显空白的**短行**——候选页眉（还须跨页复现才确认）。"""
    if not boxes:
        return []
    order = sorted(range(len(boxes)), key=lambda i: boxes[i].y0)
    if len(order) < 6:
        return []
    gaps = [(boxes[order[i + 1]].y0 - boxes[order[i]].y1, i) for i in range(len(order) - 1)]
    med = statistics.median([max(g, 0) for g, _ in gaps]) or 1.0
    out: list[int] = []
    for i in range(min(max_lines, len(order))):
        b = boxes[order[i]]
        if b.y1 > H * 0.16 or len(b.text) > max_len:
            continue
        below_gap = gaps[i][0] if i < len(gaps) else 0
        if below_gap > med * 1.4:
            out.append(order[i])
    return out


def head_similar(a: str, b: str) -> float:
    """两个页眉的归一化相似度——容忍 OCR 误识（「示例文集」/「染谦淇全集」）。"""
    na, nb = norm_head(a), norm_head(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, na, nb).ratio()


def match_pattern(text: str, patterns, threshold: float = 0.75) -> bool:
    """页眉是否与已知的刊名/书名模式相符（精确或模糊）。"""
    if not patterns:
        return False
    for p in patterns:
        if head_similar(text, p) >= threshold:
            return True
    return False


def find_rule_line(boxes: list[Box], W: float, H: float, median_h: float) -> float | None:
    """找分隔横线：**极扁的框**（高度远小于行高、宽度可观）。

    中文期刊的页下注常用一条横线与正文隔开；若只靠"字号更小 + 大空白"，
    字号相同、仅以横线分隔的刊物会漏检。
    """
    cands = [b for b in boxes
             if b.y0 > H * 0.40 and b.h < max(6.0, median_h * 0.35) and b.w > W * 0.25]
    return max(cands, key=lambda b: b.w).y0 if cands else None


def detect_bands(boxes: list[Box], H: float, W: float) -> dict:
    """识别页脚/页下注带，并给出页眉**候选**（页眉由 probe 用跨页复现确认）。

    页眉页脚的关键不是"在顶部/底部",而是**与正文之间有一段明显更大的空白**：
    只按 y<10% 判定会把正文首行误当页眉（双栏尤甚，两栏首行都在顶部）。

    页下注的几何特征：正文与小字之间有一段明显更大的**行间空白**，
    且小字部分的字高显著小于正文。
    """
    res = {"header": set(), "footer": set(), "footnote": set(), "body": set()}
    if not boxes:
        return res, []
    order = sorted(range(len(boxes)), key=lambda i: boxes[i].y0)
    if len(order) < 8:
        res["body"] = set(range(len(boxes)))
        return res, []
    gaps = [(boxes[order[i + 1]].y0 - boxes[order[i]].y1, i) for i in range(len(order) - 1)]
    med_gap = statistics.median([max(g, 0) for g, _ in gaps]) or 1.0

    # 页脚：页面下缘、与正文有明显空白的**极短行**（多为页码）
    bot_start = None
    for g, i in reversed(gaps):
        if boxes[order[i]].y0 > H * 0.86 and g > med_gap * 1.5:
            bot_start = i + 1
            break
    if bot_start is not None and len(order) - bot_start <= 2:
        cand = [order[j] for j in range(bot_start, len(order))]
        if all(len(boxes[j].text) <= 12 for j in cand):
            res["footer"] = set(cand)

    # 页下注：① 横线分隔（中文期刊最常见）② 或"大空白 + 字号更小"
    median_h = statistics.median([b.h for b in boxes]) or 1.0
    rule_y = find_rule_line(boxes, W, H, median_h)
    cut_y = None
    if rule_y is not None:
        below = [j for j in order if boxes[j].y0 > rule_y and j not in res["footer"]]
        if len(below) >= 2:
            cut_y = rule_y
    if cut_y is None:
        cand = [(g, i) for g, i in gaps
                if g > med_gap * 1.8 and boxes[order[i]].y0 > H * 0.45 and order[i] not in res["footer"]]
        if cand:
            _g, i = max(cand)
            y = boxes[order[i + 1]].y0
            below = [j for j in order if boxes[j].y0 >= y and j not in res["footer"]]
            if below:
                h_body = statistics.median([b.h for b in boxes if b.y0 < y]) or 1
                h_note = statistics.median([boxes[j].h for j in below]) or 1
                if h_note < h_body * 0.92:
                    cut_y = y
    if cut_y is not None:
        res["footnote"] = {j for j in order
                           if boxes[j].y0 >= cut_y and j not in res["footer"]}
    for i in range(len(boxes)):
        if i not in res["header"] and i not in res["footer"] and i not in res["footnote"]:
            res["body"].add(i)
    return res, top_candidates(boxes, H)


def detect_vertical(boxes: list[Box]) -> bool:
    """竖排判定：文字框普遍"高而窄"且行距按 x 排列。"""
    if len(boxes) < 10:
        return False
    ratios = [b.w / max(1.0, b.h) for b in boxes]
    med = statistics.median(ratios)
    return med < 0.7 and statistics.median([b.h for b in boxes]) > statistics.median([b.w for b in boxes]) * 1.6


def page_profile(boxes: list[Box], W: float, H: float) -> dict:
    """单页版式档案。"""
    col = detect_columns(boxes, W)
    bands, hc = detect_bands(boxes, H, W)
    return {
        "n_boxes": len(boxes),
        "cols": col["cols"],
        "gutters": [round(g / W, 3) for g in col["gutters"]],
        "header": len(bands["header"]),
        "header_candidates": [boxes[i].text[:40] for i in hc],
        "footer": len(bands["footer"]),
        "footnote": len(bands["footnote"]),
        "body": len(bands["body"]),
        "vertical": detect_vertical(boxes),
        "median_line_h": round(statistics.median([b.h for b in boxes]), 1) if boxes else 0,
        "mean_score": round(statistics.mean([b.score for b in boxes]), 3) if boxes else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────
# 文本层：注释体系与引用格式
# ─────────────────────────────────────────────────────────────────────────
def detect_note_system(texts: list[str]) -> dict:
    """判定注释编号体系。

    优先级：圈码页下注 > 方括号顺序编码 > 圆括号数字注 > 作者-年份夹注。
    兜底的"文中尾注编号"只在**别的方式都没命中**且计数足够时才作结论，
    否则它会因为文中任意一个孤立数字而"命中"，把真实体系挤掉。
    """
    joined = "\n".join(texts)
    strong = []
    for name, pat in NOTE_MARKERS:
        if name == "文中尾注编号":
            continue
        n = len(pat.findall(joined))
        if n:
            strong.append((name, n))
    strong.sort(key=lambda x: -x[1])
    weak = len(NOTE_MARKERS[-1][1].findall(joined))

    sections = {}
    for name, pat in SECTION_HINTS.items():
        n = len(pat.findall(joined))
        if n:
            sections[name] = n
    has_endnote = bool(re.search(r"^\s*(注\s*释|参考文献|References)\s*$", joined, re.M))

    if strong and strong[0][1] >= 2:
        primary = strong[0][0]
    elif strong:
        primary = strong[0][0] + "（仅 1 处，待核）"
    elif weak >= 5 or has_endnote:
        primary = "文中尾注编号（弱信号）"
    else:
        primary = "未识别（可能无注释或为纯文本）"
    return {
        "markers": [{"type": t, "count": n} for t, n in strong[:4]],
        "weak_number_hits": weak,
        "primary": primary,
        "sections": sections,
        "has_circled_markers": bool(re.search(r"[①②③④⑤⑥⑦⑧⑨⑩]", joined)),
        "has_endnote_section": has_endnote,
    }


def detect_citation_style(texts: list[str]) -> dict:
    joined = "\n".join(texts)
    out = {}
    for name, pat in CITE_PATTERNS:
        n = len(pat.findall(joined))
        if n:
            out[name] = n
    return out


# ─────────────────────────────────────────────────────────────────────────
# 文档级预检
# ─────────────────────────────────────────────────────────────────────────
def sample_indices(total: int, n: int) -> list[int]:
    """抽样页码：首页若干 + 均匀分布 + 末页（版面常分段变化）。"""
    if total <= n:
        return list(range(total))
    idx = set(range(min(3, total))) | {total - 1}
    remaining = max(1, n - len(idx))
    step = max(1, max(1, total - 3) // remaining)
    idx.update(range(3, total - 1, step))
    return sorted(idx)[:max(n, 4)]


def probe_document(pdf: Path, dpi: int = 200, samples: int = 6, engine=None) -> dict:
    import numpy as np
    import pymupdf as fitz
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()

    doc = fitz.open(str(pdf))
    idxs = sample_indices(doc.page_count, samples)
    per_page, texts, boxes_by_page = [], [], {}
    for i in idxs:
        page = doc[i]
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        try:
            res, _ = engine(img)
        except Exception as exc:                                   # noqa: BLE001
            res = None
            per_page.append({"page": i + 1, "error": f"{type(exc).__name__}: {exc}"})
            continue
        boxes = boxes_from_result(res)
        prof = page_profile(boxes, float(pix.width), float(pix.height))
        prof["page"] = i + 1
        per_page.append(prof)
        boxes_by_page[i + 1] = boxes
        texts.append("\n".join(b.text for b in boxes))
    doc.close()

    # ── 页眉的**跨页复现**确认：页眉（刊名/书名）会在多页重复，正文首行不会 ──
    head_hits: dict[str, int] = {}
    for prof in per_page:
        for t in prof.get("header_candidates", []):
            k = norm_head(t)
            if k:
                head_hits[k] = head_hits.get(k, 0) + 1
    need = max(2, int(len(per_page) * 0.3))
    patterns = sorted(k for k, v in head_hits.items() if v >= need)
    for prof in per_page:
        boxes = boxes_by_page.get(prof.get("page"), [])
        confirmed = []
        for j, b in enumerate(boxes):
            if b.text[:40] not in (prof.get("header_candidates") or []):
                continue
            # 与 order_page 使用**同一套**判定规则：跨页复现的刊名/书名（精确或模糊），
            # 或极短且含数字的行（通常就是孤立的页码）
            if match_pattern(b.text, patterns) or (len(b.text) <= 12 and re.search(r"\d", b.text)):
                confirmed.append(j)
        prof["header_confirmed"] = [boxes[j].text[:40] for j in confirmed]
        prof["header_confirmed_ids"] = confirmed
        prof["header"] = len(confirmed)

    cols = [p.get("cols", 1) for p in per_page if "error" not in p]
    ncols_mode = max(set(cols), key=cols.count) if cols else 1
    mixed = len(set(cols)) > 1
    notes = detect_note_system(texts)
    cites = detect_citation_style(texts)

    n_header = sum(1 for p in per_page if p.get("header"))
    n_foot = sum(1 for p in per_page if p.get("footnote"))
    vert = any(p.get("vertical") for p in per_page)

    layout = classify_layout(ncols_mode, mixed, n_header, n_foot, len(per_page), vert, notes)
    return {
        "file": pdf.name,
        "pages_total": None,
        "sampled": idxs and [i + 1 for i in idxs],
        "layout_class": layout,
        "columns": {"mode": ncols_mode, "mixed": mixed,
                    "gutters": sorted({g for p in per_page for g in p.get("gutters", [])})},
        "running_head_pages": f"{n_header}/{len(per_page)}",
        "running_head_patterns": patterns,
        "footnote_pages": f"{n_foot}/{len(per_page)}",
        "vertical": vert,
        "note_system": notes,
        "citation_style": cites,
        "per_page": per_page,
        "warnings": build_warnings(per_page, mixed, n_foot, notes),
    }


def classify_layout(ncols, mixed, n_header, n_foot, n_pages, vert, notes) -> str:
    if vert:
        return "竖排（右起竖读）"
    parts = []
    parts.append({1: "单栏", 2: "双栏", 3: "三栏"}.get(ncols, f"{ncols}栏"))
    if mixed:
        parts.append("页间栏数不统一")
    if n_header >= max(1, n_pages * 0.5):
        parts.append("有页眉")
    if n_foot >= max(1, n_pages * 0.4):
        parts.append("有页下注")
    if notes.get("has_endnote_section"):
        parts.append("含尾注/参考文献节")
    return "·".join(parts)


def build_warnings(per_page, mixed, n_foot, notes) -> list[str]:
    w = []
    low = [p["page"] for p in per_page if p.get("mean_score", 1) < 0.75]
    if low:
        w.append(f"第 {low} 页平均识别置信度低于 0.75，需人工复核")
    few = [p["page"] for p in per_page if p.get("n_boxes", 99) < 5]
    if few:
        w.append(f"第 {few} 页文字框过少（可能是插图页或纯图扫描）")
    if mixed:
        w.append("各页栏数不一致：需按页使用各自的栏参数，不能用单一参数通吃")
    if n_foot:
        w.append("检出页下注带：注释已与正文分离，引用时须区分")
    if notes.get("primary") == "未识别":
        w.append("未识别出注释编号体系：可能无注释或以文中夹注形式出现")
    return w


# ─────────────────────────────────────────────────────────────────────────
# 按版式档案排阅读顺序
# ─────────────────────────────────────────────────────────────────────────
def order_page(boxes: list[Box], W: float, H: float, profile: dict | None = None) -> list[tuple[str, str]]:
    """按档案返回 [(区域, 文本)]：区域 ∈ {页眉, 正文, 页下注, 页脚}。

    页眉的确认规则（满足其一）：
      * 文本与预检学到的 `running_head_patterns`（跨页复现的书名/刊名）匹配；
      * 极短（≤12 字，通常就是孤立的页码）。
    只靠几何会把两栏的首行正文误判成页眉，故必须有内容判据。
    """
    if not boxes:
        return []
    prof = profile or {}
    cols = prof.get("cols") or detect_columns(boxes, W)["cols"]
    gutters = [W * g for g in prof.get("gutters", [])] or detect_columns(boxes, W)["gutters"]
    bands, hc = detect_bands(boxes, H, W)
    patterns = set(prof.get("running_head_patterns") or [])
    # 页眉判定：① 与跨页复现的刊名/书名相符；② 或极短**且含数字**（页码）。
    # ② 必须要求含数字——否则文章标题（如「示例人物与社会主义」8 字）也会被当成页眉。
    header_ids = {i for i in hc
                  if match_pattern(boxes[i].text, patterns)
                  or (len(boxes[i].text) <= 12 and bool(re.search(r"\d", boxes[i].text)))}
    for i in prof.get("header_confirmed_ids", []):
        if i in hc:
            header_ids.add(i)

    if detect_vertical(boxes):
        # 竖排：从右到左，每列从上到下
        cols_x = []
        for b in sorted(boxes, key=lambda b: -b.cx):
            placed = False
            for col in cols_x:
                if abs(col[-1].cx - b.cx) < W * 0.04:
                    col.append(b); placed = True; break
            if not placed:
                cols_x.append([b])
        out = []
        for col in cols_x:
            col.sort(key=lambda b: b.y0)
            for b in col:
                out.append(("正文", b.text))
        return out

    def seq(ids):
        return sorted(ids, key=lambda i: (round(boxes[i].y0 / 8), boxes[i].cx))

    ordered: list[tuple[str, str]] = []
    for i in seq(header_ids):
        ordered.append(("页眉", boxes[i].text))

    body = {i for i in bands["body"] if i not in header_ids}
    full = {i for i in body if boxes[i].w > W * 0.55}
    for i in seq(full):                       # 通栏元素（标题/摘要）先读
        ordered.append(("正文", boxes[i].text))
    rest = [i for i in body if i not in full]
    if cols and cols > 1 and gutters:
        for c in range(cols):
            lo = gutters[c - 1] if c > 0 else -1e9
            hi = gutters[c] if c < len(gutters) else 1e9
            grp = [i for i in rest if lo < boxes[i].cx <= hi]
            for i in seq(grp):
                ordered.append(("正文", boxes[i].text))
    else:
        for i in seq(rest):
            ordered.append(("正文", boxes[i].text))

    for i in seq(bands["footnote"]):
        ordered.append(("页下注", boxes[i].text))
    for i in seq(bands["footer"]):
        ordered.append(("页脚", boxes[i].text))
    return ordered


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    p = Path(a.pdf).resolve()
    prof = probe_document(p, dpi=a.dpi, samples=a.pages)
    print("=" * 100)
    print(f"版式预检：{p.name}")
    print("=" * 100)
    print(f"  版面分类：**{prof['layout_class']}**")
    print(f"  栏数：{prof['columns']['mode']}｜页间是否不统一：{prof['columns']['mixed']}"
          f"｜分栏线（页宽占比）：{prof['columns']['gutters']}")
    print(f"  页眉页：{prof['running_head_pages']}｜页下注页：{prof['footnote_pages']}"
          f"｜竖排：{prof['vertical']}")
    print(f"  注释体系：{prof['note_system']['primary']}"
          f"｜标记计数 {prof['note_system']['markers']}")
    print(f"  引用格式：{prof['citation_style']}")
    print(f"  章节线索：{prof['note_system']['sections']}")
    for w in prof["warnings"]:
        print(f"  ⚠️ {w}")
    print("\n  逐页明细：")
    for q in prof["per_page"]:
        if "error" in q:
            print(f"    第{q['page']}页：{q['error']}")
            continue
        print(f"    第{q['page']:>4}页｜{q['cols']}栏｜框{q['n_boxes']:>4}｜页眉{q['header']}"
              f"｜正文{q['body']}｜页下注{q['footnote']}｜页脚{q['footer']}"
              f"｜置信 {q['mean_score']}")
    if a.json:
        Path(a.json).write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写出档案：{a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
