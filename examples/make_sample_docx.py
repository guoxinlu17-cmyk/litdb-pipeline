# -*- coding: utf-8 -*-
"""生成一份**合成**的样例 docx，用来演示「路线②：docx 页眉法（免 OCR）」。

为什么要合成而不是放真书
------------------------
这份 docx 模拟的是一种真实存在的素材形态：**一页一节，且每节的页眉里印着书上的页码**。
（很多由扫描件转换而来的 docx 就是这个样子。）

真实书籍的扫描件涉及版权，不能进仓库；而合成样例**结构完全一致**，
演示效果一样，且不依赖任何外部文件。

它生成的结构
------------
    [第 1 页正文] → [带页眉的节] → [第 2 页正文] → [带页眉的节] → ……

页眉里写的是**书上印的页码**，刻意从 101 起（不是 1），
这样演示时能看清「PDF 页序 ≠ 印刷页码」这件事。

用法：
    python examples/make_sample_docx.py [输出路径] [--pages 6] [--start 101]
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"

# 合成正文：措辞刻意通用，不含任何真实文本。
# 每页取不同的一组，避免各页雷同（真实素材的页面当然是各不相同的）。
SAMPLE_PARAS = [
    "本页第 {n} 页。这套流程只守两条线：页码是主线，以及宁可留空不可猜测。",
    "第二段。素材体检先回答三个问题：多少页、有没有文字层、版式是什么样。",
    "第三段。无页码的节一律归入其前一个带页码的页——只归并，不插值。",
    "第四段。引号配对要在全文一次做完；在小窗口里配对会整体错开一位。",
    "第五段。省略号不能当噪声删掉，否则前后两截会被强行接成一句。",
    "第六段。最危险的不是「读不出来」，而是「读错但看起来对」。",
    "第七段。页码偏移量必须逐卷自证：命中页里要真的印着那个数字。",
    "第八段。交叉验证的意思是：两条**独立路径**算出的页码一致，才允许写进论文。",
    "第九段。校勘与回填分开做，未核实的标「待回查纸本」，不冒充已核实。",
    "第十段。写库之前先备份；写完立刻核对关键计数，不增反减就要停下来查。",
]


def doc_xml(pages: list[tuple[int, str, str]]) -> str:
    """pages: [(印刷页码, 页眉文字, 正文)]，最后一个节用 body 级 sectPr。"""
    body = []
    for i, (printed, head, text) in enumerate(pages):
        paras = "".join(
            f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in text.split("\n"))
        sect = (f'<w:sectPr><w:headerReference w:type="default" r:id="rIdH{i + 1}"/>'
                f'<w:pgSz w:w="11906" w:h="16838"/></w:sectPr>')
        if i == len(pages) - 1:
            body.append(paras)            # 最后一页正文
            body.append(sect)             # body 级 sectPr 收尾
        else:
            body.append(paras)
            body.append(f"<w:p><w:pPr>{sect}</w:pPr></w:p>")
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{"".join(body)}</w:body></w:document>')


def header_xml(head: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:hdr xmlns:w="{W}" xmlns:r="{R}">'
            f'<w:p><w:r><w:t xml:space="preserve">{head}</w:t></w:r></w:p></w:hdr>')


def content_types(n: int) -> str:
    hdrs = "".join(
        f'<Override PartName="/word/header{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
        for i in range(1, n + 1))
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{CT}">'
            f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            f'{hdrs}</Types>')


def root_rels() -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG}">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="word/document.xml"/>'
            f'</Relationships>')


def doc_rels(n: int) -> str:
    rels = "".join(
        f'<Relationship Id="rIdH{i}" Type="{R}/header" Target="header{i}.xml"/>'
        for i in range(1, n + 1))
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG}">{rels}</Relationships>')


def build(out: Path, npages: int, start: int, corpus: str) -> Path:
    pages = []
    for i in range(npages):
        printed = start + i
        # 每页取 4 段，按页号错开，避免各页雷同
        picked = [SAMPLE_PARAS[(i * 3 + k) % len(SAMPLE_PARAS)] for k in range(4)]
        text = "\n".join(s.format(n=printed) for s in picked)
        # 页眉形如「644 某某文集」——页码在前，后面跟集名
        pages.append((printed, f"{printed} {corpus}", text))

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types(npages))
        z.writestr("_rels/.rels", root_rels())
        z.writestr("word/document.xml", doc_xml(pages))
        z.writestr("word/_rels/document.xml.rels", doc_rels(npages))
        for i, (_p, head, _t) in enumerate(pages, 1):
            z.writestr(f"word/header{i}.xml", header_xml(head))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生成合成样例 docx（一页一节 + 页眉带印刷页码）")
    ap.add_argument("out", nargs="?", default=str(Path(__file__).parent / "sample" / "示例文集 第1卷.docx"))
    ap.add_argument("--pages", type=int, default=6, help="页数，默认 6")
    ap.add_argument("--start", type=int, default=101, help="首页的印刷页码，默认 101")
    ap.add_argument("--corpus", default="示例文集", help="文集名，会写进页眉")
    a = ap.parse_args()

    p = build(Path(a.out), a.pages, a.start, a.corpus)
    print(f"已生成合成样例：{p}")
    print(f"  {a.pages} 页｜印刷页码 {a.start}–{a.start + a.pages - 1}｜页眉形如「{a.start} {a.corpus}」")
    print(f"  刻意让印刷页码不从 1 开始——用来演示「PDF 页序 ≠ 印刷页码」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
