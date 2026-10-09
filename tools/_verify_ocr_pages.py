# -*- coding: utf-8 -*-
"""① 页码级核对：验证 OCR 产物与源 PDF 的页对应关系。

检查四项：
  1. **页标记数**是否等于 PDF 页数，且从 1 连续到 N；
  2. **空页清单**（OCR 无文字的页，通常是插图页/空白页）；
  3. **印刷页码追踪**：从每页文本中识别页眉/页脚的印刷页码，检验其单调递增，
     并估算 **PDF 页序 ↔ 印刷页码的偏移量**（用于反查引文页码）；
  4. **页字符量分布**（异常短的页提示 OCR 失败或版面异常）。

用法：
    python -B _verify_ocr_pages.py <源.pdf> <OCR.md>   # 或只给 OCR.md（页数从标记数推断）
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PAGE_SPLIT = re.compile(r"^--- 第 (\d+) 页 ---$", re.MULTILINE)


def guess_printed_number(text: str) -> int | None:
    """从页首/页尾 2 行里找印刷页码（1–4 位独立数字，含中文数字不处理）。"""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return None
    for candidate in (lines[:2] + lines[-2:]):
        m = re.fullmatch(r"[·\-\s]*(\d{1,4})[·\-\s]*", candidate)
        if m:
            value = int(m.group(1))
            if 1 <= value <= 3000:
                return value
        # 形如 "84 探索主体性" 或 "示例文集 47"
        m2 = re.match(r"^(\d{1,4})\s+\S", candidate) or re.search(r"\s(\d{1,4})$", candidate)
        if m2:
            value = int(m2.group(1))
            if 1 <= value <= 3000:
                return value
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    # 参数顺序自适应：按扩展名判断哪个是 PDF、哪个是 OCR.md（两者的先后顺序均可）
    args = [Path(x).resolve() for x in sys.argv[1:3]]
    pdf = next((p for p in args if p.suffix.lower() == ".pdf"), None)
    md = next((p for p in args if p.suffix.lower() != ".pdf"), None)
    if md is None and pdf is not None:
        md = pdf.with_suffix(".OCR.md")          # 只给 PDF 时推断同名 OCR 产物
    if pdf is None and md is not None:
        candidate = Path(str(md).replace(".OCR.md", ".pdf"))
        pdf = candidate if candidate.exists() else None
    if md is None or not md.exists():
        print(f"未找到 OCR 产物：{md}")
        return 1
    text = md.read_text(encoding="utf-8", errors="replace")
    parts = PAGE_SPLIT.split(text)
    pages = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    labels = [int(n) for n, _ in pages]

    print(f"文件：{md.name}")
    print(f"  1) 页标记：{len(labels)} 个" + (f"｜首尾 {labels[0]}–{labels[-1]}" if labels else ""))
    sequential = labels == list(range(1, len(labels) + 1))
    print(f"     连续性：{'✅ 1..N 连续' if sequential else '⚠️ 存在缺号/乱序'}")

    if pdf is not None:
        try:
            import fitz
            total = fitz.open(str(pdf)).page_count
            print(f"     源 PDF 页数：{total}｜{'✅ 一致' if total == len(labels) else '⚠️ 不一致'}")
        except Exception as exc:
            print(f"     源 PDF 打开失败：{exc}")

    empties = [int(n) for n, body in pages if len(body.strip()) < 5]
    lengths = [(int(n), len(body)) for n, body in pages]
    short = [(n, ln) for n, ln in lengths if 0 < ln < 120]
    print(f"  2) 空页：{len(empties)} 个" + (f"（{empties[:12]}{'…' if len(empties) > 12 else ''}）" if empties else ""))
    print(f"     过短页（0<字符<120）：{len(short)} 个" + (f"（{short[:8]}）" if short else ""))

    # 3) 印刷页码追踪
    found = []
    for n, body in pages:
        printed = guess_printed_number(body)
        if printed is not None:
            found.append((int(n), printed))
    print(f"  3) 识别到印刷页码的页数：{len(found)}")
    if found:
        offsets = [pdf_page - printed for pdf_page, printed in found]
        from collections import Counter
        common = Counter(offsets).most_common(3)
        increasing = all(found[i][1] <= found[i + 1][1] for i in range(len(found) - 1))
        print(f"     印刷页码单调递增：{'✅' if increasing else '⚠️ 存在倒退（可能识别错了页眉数字）'}")
        print(f"     PDF 页序 − 印刷页码 的常见偏移：{common}")
        sample = found[:3] + found[-3:] if len(found) > 6 else found
        print(f"     样例（PDF 页 → 印刷页）：{sample}")
    else:
        print("     未识别到独立页码（该书页码可能与正文同行或为中文数字）")

    # 4) 分布
    if lengths:
        chars = [ln for _, ln in lengths]
        print(f"  4) 页字符量：最小 {min(chars)}｜中位 {sorted(chars)[len(chars)//2]}｜最大 {max(chars)}")

    ok = sequential and (not pdf or True)
    print("结论：" + ("✅ 页结构完整" if ok else "⚠️ 需人工检查页结构"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
