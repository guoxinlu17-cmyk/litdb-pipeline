# -*- coding: utf-8 -*-
"""只用 docx（不 OCR）重建「逐页文本 + 印刷页码」索引。

原理：这几卷 docx 由扫描件按页转换，每一节的 sectPr 里挂一个页眉部件，而文集页眉
形如「<篇名> 5」或「6 <文集名>」——**页码就印在页眉里**。此前 _docx_full_text.py
只抽 document.xml 正文，把这些页眉连同页码一起丢掉了。

做法：
  1) 按文档顺序取每个 sectPr 的 w:headerReference(r:id) → 页眉部件；
  2) 该 sectPr 之前的正文即这一页的正文；
  3) 从页眉文字取页码（先看末尾数字，再看开头数字）；
  4) 相邻页页码应逐个 +1（跳号说明有缺页或分节异常）。

输出：_docx_pages/_docx_pages_<标签>.json

用法：
    python -B _docx_pages_index.py                 # 第 2/3/6/7/8 卷
    python -B _docx_pages_index.py 3 6             # 指定卷
    python -B _docx_pages_index.py --file "D:\\...\\某某.docx" --tag 第5卷另一版
"""
import argparse
import json
import os
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import lit_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
LIT = Path(lit_dir())
OUTDIR = Path(__file__).resolve().parent / "_docx_pages"


def _pdf_pages() -> dict[int, int | None]:
    """各卷 PDF 页数（只用于打印对照，不参与任何判断）。

    从环境变量 `LITDB_PDF_PAGES` 给，形如 `2:None,3:764,6:1033`；不给就显示「未知」。
    各卷页数是具体项目的实测指纹，不写进源码。
    """
    out: dict[int, int | None] = {}
    for item in os.environ.get("LITDB_PDF_PAGES", "").split(","):
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        try:
            out[int(k.strip())] = int(v) if v.strip().isdigit() else None
        except ValueError:
            pass
    return out


PDF_PAGES = _pdf_pages()

SECT = re.compile(r"<w:sectPr\b.*?</w:sectPr>", re.S)
HREF = re.compile(r'<w:headerReference\b[^>]*r:id="([^"]+)"')
WT = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")
BR = re.compile(r"<w:(?:br|cr)\b[^>]*/?>")


def rels(z: zipfile.ZipFile) -> dict[str, str]:
    xml = z.read("word/_rels/document.xml.rels").decode("utf-8", "replace")
    return dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', xml))


def texts(xml: str) -> str:
    out = []
    for m in re.finditer(r"<w:t[^>]*>([^<]*)</w:t>|\n", BR.sub("\n", xml)):
        out.append(m.group(1) if m.group(1) is not None else "\n")
    return "".join(out)


def page_no(head: str) -> int | None:
    """从页眉取印刷页码。

    页眉有三种排法，且 OCR 会把数字拆开：

    * ``26 示例文集``（页码在前）、``自 述 25``（页码在后）；
    * ``1 96 示例文集`` —— 其实是 196，数字被空格拆断；
    * ``散 篇 论 述 1 9 6 9 年 189`` —— 年份在前、页码在后。

    规则：把「数字与数字间的单个空格」视为同一串，取**最后一串**；紧跟「年」的那串是年份，跳过。
    另设 1–1500 的合理范围，超出即视为误识。
    """
    h = head.strip()
    if not h:
        return None
    vals: list[int] = []
    for g in re.finditer(r"\d(?:\s?\d)*", h):
        if h[g.end():g.end() + 1] == "年":      # 年份，不是页码
            continue
        vals.append(int(g.group(0).replace(" ", "")))
    if not vals:
        return None
    v = vals[-1]
    return v if 0 < v <= 1500 else None


def pick_docx(vol: int) -> str | None:
    """取该卷 docx。注意第 2 卷文件名带「(OCR)」后缀，不能一律排除。"""
    cands = [f for f in os.listdir(LIT)
             if f"第{vol}卷" in f and f.endswith(".docx") and not f.endswith(".OCR.docx")]
    return sorted(cands)[0] if cands else None


def repair_pages(pages: list[dict]) -> int:
    """用前后邻居修正被 OCR 截断的页码（如 60 → 「6」 → 62，应作 61）。

    只看相邻三个**带页码**的节：若 next − prev == 2，中间那页必然是 prev+1；
    若 next − prev == 1 而中间值既不等于 prev 也不等于 next，则它其实与 prev 同页。
    编号重启（目录→正文，跨度为大负数）不在此列，一律不修。返回修正条数。
    """
    idx = [i for i, p in enumerate(pages) if p.get("printed")]
    fixed = 0
    for k in range(1, len(idx) - 1):
        i, j, l = idx[k - 1], idx[k], idx[k + 1]
        a, b, c = pages[i]["printed"], pages[j]["printed"], pages[l]["printed"]
        if c - a == 2 and b != a + 1:
            pages[j]["printed"], pages[j]["fixed"] = a + 1, f"{b}→{a + 1}"
            fixed += 1
        elif c - a == 1 and b not in (a, c):
            pages[j]["printed"], pages[j]["fixed"] = a, f"{b}→{a}"
            fixed += 1
    return fixed


def analyse(path: Path, tag: str, pdf_pages: int | None) -> dict:
    with zipfile.ZipFile(path) as z:
        doc = z.read("word/document.xml").decode("utf-8", "replace")
        rmap = rels(z)
        hcache: dict[str, str] = {}

        def head_of(rid: str) -> str:
            tgt = rmap.get(rid)
            if not tgt:
                return ""
            name = "word/" + tgt.lstrip("/").replace("word/", "")
            if name not in hcache:
                try:
                    hcache[name] = texts(z.read(name).decode("utf-8", "replace")).strip()
                except KeyError:
                    hcache[name] = ""
            return hcache[name]

        pages = []
        prev_end = 0
        for m in SECT.finditer(doc):
            body = texts(doc[prev_end:m.start()])
            ref = HREF.search(m.group(0))
            head = head_of(ref.group(1)) if ref else ""
            prev_end = m.end()
            pages.append({"seq": len(pages) + 1, "printed": page_no(head),
                          "head": re.sub(r"\s+", " ", head), "chars": len(body), "text": body})
        tail = texts(doc[prev_end:])
        if tail.strip():
            pages.append({"seq": len(pages) + 1, "printed": None, "head": "（文末无节）",
                          "chars": len(tail), "text": tail})

    nums = [p["printed"] for p in pages]
    got = [(i + 1, n) for i, n in enumerate(nums) if n]
    steps = [b - a for (_, a), (_, b) in zip(got, got[1:])]
    offs = Counter(i - n for i, n in got)
    lens = [p["chars"] for p in pages]
    nfix = repair_pages(pages)
    nums2 = [p["printed"] for p in pages]
    got2 = [(i + 1, n) for i, n in enumerate(nums2) if n]
    steps2 = [b - a for (_, a), (_, b) in zip(got2, got2[1:])]
    print("=" * 96)
    print(f"{tag}　{path.name[:62]}")
    print(f"  节数 {len(pages)}｜PDF 页数 {pdf_pages}｜有页码的节 {len(got2)}｜截断修正 {nfix} 处")
    if lens:
        print(f"  正文长度：中位 {sorted(lens)[len(lens)//2]} 字｜最短 {min(lens)}｜最长 {max(lens)}")
    print(f"  修正前步长：{Counter(steps).most_common(4)}")
    print(f"  修正后步长：{Counter(steps2).most_common(4)}")
    print(f"  offset（节序−印刷页）分布：{offs.most_common(4)}")
    if got2:
        print(f"  页码范围 {min(n for _i, n in got2)}–{max(n for _i, n in got2)}"
              f"｜前 10 个页码：{[n for _i, n in got2[:10]]}")
    if len(pages) > 210:
        p = pages[209]
        print(f"  样例（第 210 节）：页眉「{p['head']}」正文头 40 字「{re.sub(r'\\s+', ' ', p['text'])[:40]}」")

    OUTDIR.mkdir(exist_ok=True)
    out = OUTDIR / f"_docx_pages_{tag}.json"
    out.write_text(json.dumps({"tag": tag, "file": path.name, "pages": pages},
                              ensure_ascii=False), encoding="utf-8")
    print(f"  已写出 {out.name}（{out.stat().st_size / 1024 / 1024:.1f} MB）")
    return {"tag": tag, "sections": len(pages), "withnum": len(got),
            "range": (got[0][1], got[-1][1]) if got else None,
            "offset_mode": offs.most_common(1)[0] if offs else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vols", nargs="*", type=int, default=[])
    ap.add_argument("--file")
    ap.add_argument("--tag")
    a = ap.parse_args()

    if a.file:
        p = Path(a.file)
        analyse(p, a.tag or p.stem[:24], None)
        return 0

    for v in (a.vols or [2, 3, 6, 7, 8]):
        name = pick_docx(v)
        if not name:
            print(f"第{v}卷：无 docx")
            continue
        analyse(LIT / name, str(v), PDF_PAGES.get(v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
