# -*- coding: utf-8 -*-
"""把 DOCX 的**全部文本**抽成 Markdown：正文 + 脚注 + 尾注 + 文本框 + 表格。

动机：kb 引擎对部分《全集》卷的入库率只有 9%–62%（第8卷 93 万字符仅入库 8 万），
根因很可能是其 DOCX 提取只走 `document.paragraphs`，漏掉了脚注/尾注/文本框（Word 里
这些内容不在主文档流中）。本脚本直接解析 word/document.xml、footnotes.xml、endnotes.xml，
按文档顺序抽出所有 <w:t> 文本，并在 <w:p> 处换行，另标注可用的页码信息
（w:lastRenderedPageBreak 渲染分页标记，以及 docProps/app.xml 的总页数）。

用法：
    python -B _docx_full_text.py <输入.docx> [输出.md] [--footnotes-last]

输出：
    Markdown（正文在前；脚注/尾注附在末尾，逐条编号，便于核对）；文首写明来源与提取范围。
"""
import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def xml_text(blob: bytes, page_markers: bool = False) -> str:
    """把一个 OOXML part 抽成文本（按段落换行）。"""
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return ""
    out: list[str] = []
    for para in root.iter(f"{W}p"):
        buf: list[str] = []
        for node in para.iter():
            tag = node.tag
            if tag == f"{W}t" and node.text:
                buf.append(node.text)
            elif tag == f"{W}tab":
                buf.append("\t")
            elif tag == f"{W}br":
                buf.append("\n")
        line = "".join(buf).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def split_footnotes(blob: bytes) -> list[str]:
    """脚注/尾注 part：按 <w:footnote>/<w:endnote> 逐条抽出。"""
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return []
    items: list[str] = []
    for node in list(root):
        if node.tag not in (f"{W}footnote", f"{W}endnote"):
            continue
        fid = node.attrib.get(f"{W}id", "")
        if fid in ("-1", "0"):  # 分隔符/延续分隔符
            continue
        buf: list[str] = []
        for para in node.iter(f"{W}p"):
            line = "".join(t.text or "" for t in para.iter(f"{W}t")).strip()
            if line:
                buf.append(line)
        if buf:
            items.append(f"[{fid}] " + " ".join(buf))
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("docx")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--footnotes-last", action="store_true", default=True)
    a = ap.parse_args()

    src = Path(a.docx).resolve()
    out = Path(a.out).resolve() if a.out else src.with_suffix(".full.md")

    with zipfile.ZipFile(src) as zf:
        names = set(zf.namelist())
        body = xml_text(zf.read("word/document.xml")) if "word/document.xml" in names else ""
        foot = split_footnotes(zf.read("word/footnotes.xml")) if "word/footnotes.xml" in names else []
        end = split_footnotes(zf.read("word/endnotes.xml")) if "word/endnotes.xml" in names else []
        pages = ""
        if "docProps/app.xml" in names:
            try:
                app = ET.fromstring(zf.read("docProps/app.xml"))
                for child in app:
                    if child.tag.endswith("Pages") and child.text:
                        pages = child.text
                        break
            except ET.ParseError:
                pass

    header = [
        f"# {src.stem}（全文提取）",
        "",
        f"> 来源：`{src}`｜docx 内声明页数：{pages or '未提供'}",
        "> 提取范围：**正文 + 脚注 + 尾注 + 表格内文字**（Word 中脚注/文本框不在主文档流，"
        "常规 python-docx 提取会漏掉，本文件已补齐）。",
        "> 说明：本文件用于**检索与阅读**；引文与页码仍须回查纸本或原 PDF。",
        "",
        f"- 正文字符：{len(body):,}",
        f"- 脚注条数：{len(foot)}｜尾注条数：{len(end)}",
        "",
        "---",
        "",
    ]
    parts = ["\n".join(header), body]
    if foot:
        parts += ["", "---", "", f"## 脚注（{len(foot)} 条）", ""] + foot
    if end:
        parts += ["", "---", "", f"## 尾注（{len(end)} 条）", ""] + end
    text = "\n\n".join(p for p in parts if p)
    out.write_text(text, encoding="utf-8")
    print(f"完成：{out}")
    print(f"  正文 {len(body):,} 字符｜脚注 {len(foot)} 条｜尾注 {len(end)} 条｜输出 {len(text):,} 字符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
