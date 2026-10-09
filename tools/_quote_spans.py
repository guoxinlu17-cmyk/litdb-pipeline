# -*- coding: utf-8 -*-
"""论文引号配对的通用工具（供 :mod:`_cite_page_candidates` 等复用）。

论文正文里的引号有三种写法混用：

* ASCII 直引号 ``"``（**开闭同形**）——本项目两篇论文主要用它；
* 中文弯引号 ``“…”``；
* 直角引号 ``「…」``。

原 ``pick_quote`` 用正则 ``[“"]([^“”"]{6,400})[”"]`` 抓引文，把 ``"`` 同时当作
开引号与闭引号。一旦窗口内直引号个数为奇数（或窗口从半截开始），配对就整体错位，
抓到的不是引文而是引文**前面**的正文——这是「未命中」的主因。

本模块改为**全文一次配对**：

* 直引号按其出现次序两两配对（第 1–2、3–4……）；
* 弯引号、直角引号用栈配对（支持嵌套）。

配对结果是一组 ``(start, end)``（含引号本身，``end`` 为闭引号下标 +1），据此可以
稳定地取「紧邻标记的那条引语」。
"""
from __future__ import annotations

ASCII = '"'
CURLY_OPEN, CURLY_CLOSE = "\u201c", "\u201d"
CORNER_OPEN, CORNER_CLOSE = "\u300c", "\u300d"
ALL_QUOTES = ASCII + CURLY_OPEN + CURLY_CLOSE + CORNER_OPEN + CORNER_CLOSE


def build_quote_spans(text: str) -> list[tuple[int, int]]:
    """返回全文所有成对引号的 ``(起, 止)``（含引号，``止`` 为闭引号下标 +1），按起点排序。"""
    spans: list[tuple[int, int]] = []

    # ① ASCII 直引号：按出现次序两两配对（开闭同形，只能靠次序）
    idx = [i for i, ch in enumerate(text) if ch == ASCII]
    for a, b in zip(idx[0::2], idx[1::2]):
        spans.append((a, b + 1))

    # ② 弯引号 / 直角引号：栈配对（支持嵌套）
    for op, cl in ((CURLY_OPEN, CURLY_CLOSE), (CORNER_OPEN, CORNER_CLOSE)):
        stack: list[int] = []
        for i, ch in enumerate(text):
            if ch == op:
                stack.append(i)
            elif ch == cl and stack:
                spans.append((stack.pop(), i + 1))

    spans.sort()
    return spans


def _inner(text: str, start: int, end: int) -> str:
    """引号内文（去掉首尾引号本身）。"""
    return text[start + 1:end - 1]


def _gap_text(text: str, end: int, pos: int) -> str:
    """闭引号到标记之间的字符（不含空白）。"""
    return "".join(ch for ch in text[end:pos] if not ch.isspace())


def direct_quote_before(spans: list[tuple[int, int]], text: str, pos: int,
                        max_gap: int = 4) -> tuple[str, int] | None:
    """取**紧贴标记**的那条引语。

    返回 ``(引语内文, 间隔字数)``；若标记前 ``max_gap`` 个非空白字符之内没有闭引号，
    返回 ``None``——说明该标记支持的是转述句，而不是直接引语。
    """
    best: tuple[int, int] | None = None
    for start, end in spans:
        if end > pos:
            break
        best = (start, end)
    if best is None:
        return None
    start, end = best
    gap = len(_gap_text(text, end, pos))
    if gap > max_gap:
        return None
    quote = _inner(text, start, end).strip()
    return (quote, gap) if quote else None


def last_quoted_phrase(spans: list[tuple[int, int]], text: str, pos: int,
                       window: int = 120, max_len: int = 100) -> tuple[str, int] | None:
    """标记前 ``window`` 字内、最靠后的**短引号短语**（用于转述句里夹的引语锚点）。

    返回 ``(短语, 距标记字数)``。

    关键排除：若短语与标记之间还夹着**别的引文标注**（``【…】``），说明这条短语属于上一条
    引文，不能拿来给当前标记定页——否则会把上一条的页码错安到这一条上。
    """
    best: tuple[str, int] | None = None
    for start, end in spans:
        if end > pos:
            break
        gap = len(_gap_text(text, end, pos))
        if gap > window:
            continue
        between = text[end:pos]
        if "【" in between or "】" in between:
            continue
        quote = _inner(text, start, end).strip()
        if not quote or len(quote) > max_len:
            continue
        best = (quote, gap)
    return best


def sentence_before(text: str, pos: int, max_len: int = 200) -> str:
    """标记前那一句（以。！？；或换行为界），用于转述判断与内容锚点。"""
    cut = max(text.rfind(ch, max(0, pos - max_len), pos) for ch in "。！？；\n")
    return text[cut + 1:pos].strip()
