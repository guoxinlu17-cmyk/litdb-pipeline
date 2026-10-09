# -*- coding: utf-8 -*-
"""用「印刷页码 + 偏移量」定位 OCR 卷，核对引文是否落在预期页上（v2：修正匹配算法）。

v1 的缺陷：用 `SequenceMatcher(引文, 整页文本)` 算相似度——15 字引文对 800 字整页，
比值必然趋近 0，导致全部误判为"未命中"。v2 改为：
  1) **页号证据**：预期 PDF 页的 OCR 文本里是否出现该印刷页码（正文页眉/页脚常带页码）；
  2) **包含判定**：归一化后引文是否为该页文本的子串；
  3) **滑窗模糊匹配**：以引文长度在页内滑动，取最佳窗口的相似度（容忍 OCR 单字误识）。

用法：
    python -B _verify_page_offset.py [--list]
"""
import argparse
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import lit_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
LIT = Path(lit_dir())
OFFSETS = {"第1卷": 7, "第4卷": 7, "第5卷": 13}
PAGE_SPLIT = re.compile(r"^--- 第 (\d+) 页 ---$", re.MULTILINE)
CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{CJK}])[ \t]+(?=[{CJK}])")


def norm(text: str) -> str:
    text = SPACE_BETWEEN_CJK.sub("", text or "")
    return re.sub(r"[\s，。、；：（）《》“”‘’\"'.,;:()\[\]·—–\-]+", "", text)


def load_pages(volume: str) -> dict[int, str]:
    candidates = sorted(LIT.glob(f"*{volume}*.OCR.md"))
    if not candidates:
        raise FileNotFoundError(f"未找到 {volume} 的 OCR 文本")
    text = candidates[0].read_text(encoding="utf-8", errors="replace")
    parts = PAGE_SPLIT.split(text)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def best_window_ratio(needle: str, haystack: str) -> tuple[float, str]:
    """在 haystack 上以 needle 长度滑窗，返回最佳相似度与对应窗口片段。"""
    n = len(needle)
    if n == 0 or not haystack:
        return 0.0, ""
    step = max(1, n // 4)
    best, best_win = 0.0, ""
    for start in range(0, max(1, len(haystack) - n + 1), step):
        window = haystack[start:start + n]
        ratio = SequenceMatcher(None, needle, window).ratio()
        if ratio > best:
            best, best_win = ratio, window
    return best, best_win


# 用例从**外部 JSON** 读：(卷, 印刷页码起, 引文片段, 页码跨度, 说明)
# 仓库不内置——「引文+精确页码跨度+引用来源+版本异文」= 未发表研究的论据链。
#   cases.json 形如： [["第1卷", 100, "引文片段", 3, "自注"]]
CASES_FILE = (Path(sys.argv[1]) if len(sys.argv) > 1
              else Path(__file__).with_name("page_offset_cases.json"))
if not CASES_FILE.exists():
    sys.exit(f"缺少用例文件：{CASES_FILE}\n"
             '请新建 JSON： [["第1卷", 100, "引文片段", 3, "自注"]]\n'
             f"然后：python -B {Path(__file__).name} <cases.json>")
CASES = [tuple(x) for x in json.loads(CASES_FILE.read_text(encoding="utf-8"))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        for vol, page, snippet, span, src in CASES:
            print(f"{vol} 印刷 {page}–{page + span - 1} → PDF {page + OFFSETS[vol]}…：{snippet[:26]}…｜{src}")
        return 0

    cache: dict[str, dict[int, str]] = {}
    hits = 0
    for vol, printed, snippet, span, src in CASES:
        cache.setdefault(vol, load_pages(vol))
        offset = OFFSETS[vol]
        target = printed + offset
        pages = cache[vol]

        # ① 页号证据：预期页是否带该印刷页码
        page_text = pages.get(target, "")
        page_number_on_page = bool(re.search(rf"(?<!\d){printed}(?!\d)", page_text))

        # ② 在引文页码跨度内逐页做滑窗比对，找出最佳页
        needle = norm(snippet)
        best_page, best_ratio, best_window = None, 0.0, ""
        for printed_p in range(printed, printed + span):
            pdf_p = printed_p + offset
            page_norm = norm(pages.get(pdf_p, ""))
            if not page_norm:
                continue
            if needle and needle in page_norm:
                best_page, best_ratio, best_window = pdf_p, 1.0, needle
                break
            ratio, window = best_window_ratio(needle, page_norm)
            if ratio > best_ratio:
                best_page, best_ratio, best_window = pdf_p, ratio, window

        within = best_page is not None and target <= best_page <= target + span - 1
        if best_ratio >= 0.7 and within:
            verdict = "✅ 命中（落在引文页码范围内）"
            hits += 1
        elif best_ratio >= 0.7:
            verdict = "⚠️ 命中但超出所引范围"
        elif best_ratio >= 0.45:
            verdict = "⚠️ 近似"
        else:
            verdict = "❌ 未命中"

        print(f"\n=== {vol}：印刷第 {printed}–{printed + span - 1} 页（→ PDF 第 {target}–{target + span - 1} 页）｜{verdict} ===")
        print(f"  说明：{src}")
        print(f"  ① 页号证据：PDF 第 {target} 页含印刷页码 {printed} → {'✅' if page_number_on_page else '否'}")
        if best_page:
            print(f"  ② 内容定位：最佳页 PDF 第 {best_page} 页（印刷第 {best_page - offset} 页）｜相似度 {best_ratio:.2f}")
            print(f"     匹配窗口：{best_window[:60]}")
        head = re.sub(r"\s+", " ", page_text.strip())[:110]
        print(f"     预期页开头：{head}")

    print(f"\n合计：命中 {hits} / {len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
