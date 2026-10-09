# -*- coding: utf-8 -*-
"""体检 PDF：页数、是否带文字层、文字层质量、能否识别印刷页码。

用法：python -B _pdf_scan_check.py <pdf...>
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

try:
    import fitz  # PyMuPDF
except ImportError:
    print("缺少 PyMuPDF：pip install pymupdf")
    raise SystemExit(2)


def check(path: Path) -> None:
    print("=" * 96)
    print(f"◆ {path.name}")
    print(f"  大小 {path.stat().st_size / 1024 / 1024:.1f} MB")
    try:
        doc = fitz.open(path)
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ❌ 打不开：{exc}")
        return
    n = doc.page_count
    print(f"  页数 {n}")
    # 抽样 12 页看文字层
    idxs = sorted({0, 1, 2, n // 6, n // 3, n // 2, n * 2 // 3, n - 3, n - 2, n - 1})
    print("  抽样页码 → 该页文字层字符数：")
    withtext = 0
    for i in idxs:
        if not (0 <= i < n):
            continue
        t = doc[i].get_text("text") or ""
        stripped = re.sub(r"\s+", "", t)
        withtext += 1 if len(stripped) > 50 else 0
        sample = stripped[:26]
        print(f"    第 {i + 1:>4} 页 → {len(stripped):>5} 字  {sample}")
    # 全量判断前 40 页
    probe = min(40, n)
    chars = [len(re.sub(r"\s+", "", doc[i].get_text("text") or "")) for i in range(probe)]
    has_layer = sum(1 for c in chars if c > 50)
    print(f"  前 {probe} 页中 {has_layer} 页有文字层（>50 字）")
    if has_layer >= probe * 0.8:
        print("  ✅ 整册带文字层：可直接抽取，无需 OCR")
    elif has_layer == 0:
        print("  ❌ 纯图像扫描件：需要 OCR")
    else:
        print("  ⚠️ 文字层不完整（部分页缺失），建议混用：有文字层的抽，没有的 OCR")
    # 印刷页码探测：找形如 3 位数字的独立行
    print("  印刷页码探测（前 3 页含独立数字的候选）：")
    hits = 0
    for i in range(min(30, n)):
        t = doc[i].get_text("text") or ""
        nums = re.findall(r"(?m)^\s*(\d{1,4})\s*$", t)
        if nums:
            print(f"    第 {i + 1} 页：{nums[:6]}")
            hits += 1
            if hits >= 3:
                break
    if hits == 0:
        print("    （前 30 页未发现独立数字行——可能是无页码的卷首，或文字层不含页码）")
    doc.close()


for arg in sys.argv[1:]:
    check(Path(arg))
