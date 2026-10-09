# -*- coding: utf-8 -*-
"""检查全集各卷 docx 里有没有分页/页码线索（决定能否不走 OCR 就得到印刷页码）。

看法：
  * ``w:lastRenderedPageBreak`` 数 ≈ 该卷 PDF 页数 → 说明这份 docx 是「一页一段」的转换，
    其分页可能对应扫描页序；
  * ``<w:headerReference>`` / ``footer`` 部件里若出现书页页码，则能直接建「页→号」映射。
"""
import os
import re
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import lit_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
LIT = lit_dir()
PDF_PAGES = {1: 727, 2: None, 3: 764, 4: 924, 5: 1066, 6: 1033, 7: 1050, 8: 1181}

for vol in (2, 3, 6, 7, 8):
    cands = [f for f in os.listdir(LIT)
             if f"第{vol}卷" in f and f.endswith(".docx") and "OCR" not in f]
    if not cands:
        print(f"第{vol}卷：无 docx")
        continue
    path = os.path.join(LIT, cands[0])
    print("=" * 92)
    print(f"第{vol}卷　{cands[0]}")
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as exc:                                     # noqa: BLE001
        print(f"  ❌ 读取失败：{exc}")
        continue
    hf = [n for n in names if "header" in n or "footer" in n]
    breaks = len(re.findall(r'w:type="page"', xml))
    print(f"  文本 {len(xml):,} 字符｜lastRenderedPageBreak {xml.count('lastRenderedPageBreak')} 个"
          f"｜显式分页符 {breaks} 个｜sectPr {xml.count('<w:sectPr')} 个")
    print(f"  页眉/页脚部件：{hf if hf else '无'}")
    print(f"  该卷 PDF 页数（已知）：{PDF_PAGES.get(vol)}")

    # 页眉页脚里有没有页码
    for part in hf[:6]:
        try:
            with zipfile.ZipFile(path) as z:
                px = z.read(part).decode("utf-8", "replace")
        except Exception:                                        # noqa: BLE001
            continue
        texts = re.findall(r"<w:t[^>]*>([^<]{0,40})</w:t>", px)
        fld = re.findall(r"PAGE|NUMPAGES|w:instrText[^>]*>([^<]{0,40})<", px)
        if texts or fld:
            print(f"    {part}: 文本 {texts[:5]}｜域 {fld[:5]}")

    # 正文里是否夹着独立数字行（印刷页码常被当成正文抽出）
    nums = re.findall(r"<w:t[^>]*>\s*(\d{1,4})\s*</w:t>", xml)
    print(f"  正文中独立数字节点 {len(nums)} 个，示例：{nums[:12]}")
    # 是否有「<卷目> / 第 N 页」这类结构词
    # 项目特有的卷目名从环境变量 LITDB_PROBES 给（逗号分隔）；下面三个是通用页序词。
    _extra = [s for s in os.environ.get("LITDB_PROBES", "").split(",") if s.strip()]
    for probe in (*_extra, "第", "目次", "目录"):
        print(f"    「{probe}」出现 {xml.count(probe)} 次", end="")
    print()
