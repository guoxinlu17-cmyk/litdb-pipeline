# -*- coding: utf-8 -*-
"""把 PDF 逐页 OCR 成带页码的 Markdown（**版式感知**版）。

工作方式（三段式）
------------------
1. **预检**：先抽样若干页跑一遍 OCR，识别版式——栏数、通栏元素、页眉页脚、
   页下注带、竖排，以及注释编号体系与引用格式；产出 `<输出>.layout.json`。
2. **按版式识别**：正式 OCR 时**逐页**用预检得到的参数决定阅读顺序：
   通栏元素（标题/摘要）先读 → 各栏分别自上而下 → 页下注 → 页脚；
   竖排则从右到左。页眉与页下注分别加标记，便于后续提取页码与注释。
3. **质量报告**：统计每页文字框数、平均置信度、低置信框清单、与文档级版式不一致的页，
   写入文件头与 `<输出>.quality.json`，供人工复核。

要点：
- 逐页写入并 flush，支持 `--resume` 断点续跑；
- 页码标记 `--- 第 N 页 ---` 为**本 PDF 的页序**（与旧版一致，向后兼容）；
- 页眉单独成行 `【页眉】…`——书籍页眉里常印着印刷页码，供页码换算用；
- 页下注单独成段 `【页下注】…`——引用时须与正文区分。

用法：
    python -B _ocr_pdf_to_md.py <输入.pdf> [输出.md] [--dpi 200] [--pages 1-18] [--resume]
                                [--probe-pages 6] [--no-probe] [--profile x.layout.json]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ocr_layout as L  # noqa: E402

try:
    import pymupdf as fitz  # PyMuPDF ≥1.24
except ImportError:  # 兼容旧版
    import fitz
import numpy as np

HEADER = """# {title}

> **来源与可信度说明（务必先读）**
> - 本文件由 **OCR（页面图像识别）** 生成：源文件 `{pdf}`，共 {pages} 页。
> - OCR 引擎：rapidocr_onnxruntime（PP-OCR ONNX，中文）。
> - 生成时间：{when}
> - **版式预检结论**：{layout}
>   - 栏数 {cols}（分栏线位于页宽 {gutters} 处）｜预检抽样页 {sampled}
>   - 页眉页 {heads}｜页下注页 {foots}｜竖排 {vert}
>   - 注释编号体系：**{notes}**｜引用格式线索：{cites}
> - **阅读顺序按版式逐页决定**：通栏元素（标题/摘要）→ 各栏自上而下 → 页下注 → 页脚。
>   页眉与页下注各自单独成行，行首分别以 `【页眉】`、`【页下注】` 标出——**两者都不是正文**，
>   引用时须区分（页眉里常印着印刷页码，是页码标定的依据）。
> - **引用须回查原刊**：OCR 可能误识字词、串行或漏行；页码标记为 PDF 页序，
>   与刊物原页码的对应关系需用页眉中的印刷页码核对。
> - 质量提示：{quality}
> - 生成方式：逐页增量写入，支持 --resume 断点续跑；逐页问题清单见同名 `.quality.md`。

---
"""

PAGE_RE = re.compile(r"^--- 第 (\d+) 页 ---$", re.MULTILINE)


def parse_pages(spec: str, total: int) -> list[int]:
    if not spec:
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, min(int(b), total)))
        elif part:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < total]


def dedup_boxes(boxes: list[L.Box]) -> list[L.Box]:
    """去掉高度重叠且文字相同的重复检出（OCR 偶发）。"""
    keep: list[L.Box] = []
    for b in sorted(boxes, key=lambda x: -x.score):
        dup = False
        for k in keep:
            if b.text != k.text:
                continue
            ix = max(0, min(b.x1, k.x1) - max(b.x0, k.x0))
            iy = max(0, min(b.y1, k.y1) - max(b.y0, k.y0))
            inter = ix * iy
            union = b.w * b.h + k.w * k.h - inter
            if union > 0 and inter / union > 0.6:
                dup = True
                break
        if not dup:
            keep.append(b)
    return keep


def run_page(engine, page, dpi: int):
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    result, _ = engine(img)
    return L.boxes_from_result(result), float(pix.width), float(pix.height)


def render_page(ordered: list[tuple[str, str]]) -> str:
    """把 (区域, 文本) 序列渲染成页内文本（区域标记合并到一行）。"""
    lines, buf_kind, buf = [], None, []
    for kind, text in ordered:
        if kind == buf_kind:
            buf.append(text)
        else:
            if buf_kind and buf:
                lines.append((buf_kind, buf))
            buf_kind, buf = kind, [text]
    if buf_kind and buf:
        lines.append((buf_kind, buf))
    out = []
    for kind, texts in lines:
        if kind == "正文":
            out.append("\n".join(texts))
        elif kind == "页眉":
            out.append("【页眉】" + " ".join(texts))
        elif kind == "页下注":
            out.append("【页下注】" + " ".join(texts))
        else:
            out.append(f"【{kind}】" + " ".join(texts))
    return "\n".join(t for t in out if t.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--pages", default="")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--probe-pages", type=int, default=6)
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--profile", default="")
    a = ap.parse_args()

    pdf = Path(a.pdf).resolve()
    out = Path(a.out).resolve() if a.out else pdf.with_suffix(".OCR.md")
    doc = fitz.open(str(pdf))
    total = doc.page_count
    pages = parse_pages(a.pages, total)

    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()

    # ── 1. 预检 ────────────────────────────────────────────────────
    prof_path = Path(a.profile) if a.profile else out.with_suffix(".layout.json")
    profile = None
    if a.profile and prof_path.exists():
        profile = json.loads(prof_path.read_text(encoding="utf-8"))
        print(f"复用版式档案：{prof_path.name}")
    elif not a.no_probe:
        print(f"版式预检中（抽样 {a.probe_pages} 页）……")
        profile = L.probe_document(pdf, dpi=a.dpi, samples=a.probe_pages, engine=engine)
        profile["pages_total"] = total
        prof_path.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  版面分类：{profile['layout_class']}")
        print(f"  栏数 {profile['columns']['mode']}｜分栏线 {profile['columns']['gutters']}"
              f"｜页眉页 {profile['running_head_pages']}｜页下注页 {profile['footnote_pages']}")
        print(f"  注释体系：{profile['note_system']['primary']}｜引用格式：{profile['citation_style']}")
        for w in profile["warnings"]:
            print(f"  ⚠️ {w}")
        print(f"  档案已写出：{prof_path.name}")

    per_page_prof = {}
    if profile:
        for q in profile.get("per_page", []):
            if "page" in q and "error" not in q:
                per_page_prof[q["page"]] = q

    done: set[int] = set()
    if a.resume and out.exists():
        done = {int(m) for m in PAGE_RE.findall(out.read_text(encoding="utf-8", errors="replace"))}
        print(f"续跑模式：已完成 {len(done)} 页，将跳过")

    out.parent.mkdir(parents=True, exist_ok=True)
    fresh = not (a.resume and out.exists())
    stats: list[dict] = []
    quality_path = out.with_suffix(".quality.json")
    if not fresh and quality_path.exists():
        try:
            stats = json.loads(quality_path.read_text(encoding="utf-8"))
        except Exception:                                          # noqa: BLE001
            stats = []

    with out.open("w" if fresh else "a", encoding="utf-8") as fh:
        if fresh:
            c = (profile or {}).get("columns", {})
            ns = (profile or {}).get("note_system", {})
            fh.write(HEADER.format(
                title=pdf.stem, pdf=pdf, pages=total,
                when=time.strftime("%Y-%m-%d %H:%M"),
                layout=(profile or {}).get("layout_class", "未预检"),
                cols=c.get("mode", "?"), gutters=c.get("gutters", []),
                sampled=(profile or {}).get("sampled", []),
                heads=(profile or {}).get("running_head_pages", "—"),
                foots=(profile or {}).get("footnote_pages", "—"),
                vert=(profile or {}).get("vertical", False),
                notes=ns.get("primary", "—"),
                cites=(profile or {}).get("citation_style", {}),
                quality="；".join((profile or {}).get("warnings", [])) or "预检未发现异常",
            ))
            fh.flush()

        t0 = time.time()
        total_chars = 0
        for idx, pno in enumerate(pages, 1):
            if (pno + 1) in done:
                continue
            page = doc[pno]
            try:
                boxes, W, H = run_page(engine, page, a.dpi)
            except Exception as exc:                               # noqa: BLE001
                print(f"  第 {pno + 1} 页 OCR 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                boxes, W, H = [], 1.0, 1.0
            boxes = dedup_boxes(boxes)
            pp = per_page_prof.get(pno + 1)
            ordered = L.order_page(boxes, W, H, pp)
            body = render_page(ordered)

            low = [{"text": b.text[:40], "score": round(b.score, 2),
                    "region": next((k for k, t in ordered if t == b.text), "正文"),
                    "x_pct": round(b.cx / max(1.0, W) * 100, 1),
                    "y_pct": round(b.cy / max(1.0, H) * 100, 1)}
                   for b in boxes if b.score < 0.6][:8]
            regions = {k: sum(1 for x, _t in ordered if x == k)
                       for k in ("页眉", "正文", "页下注", "页脚")}
            mean = round(sum(b.score for b in boxes) / len(boxes), 3) if boxes else 0.0
            # 逐页问题清单——决定这一页要不要人工复核
            head_txt = " ".join(t for k, t in ordered if k == "页眉")
            issues = []
            if boxes and mean < 0.75:
                issues.append("平均置信度偏低")
            if len(boxes) < 5:
                issues.append("文字框过少（疑插图页/纯图）")
            if pp and pp.get("cols") and profile and pp["cols"] != profile["columns"]["mode"]:
                issues.append(f"栏数({pp['cols']})与文档级({profile['columns']['mode']})不一致")
            if profile and not regions["页眉"] and len(boxes) >= 8:
                issues.append("未识别出页眉")
            elif head_txt and not re.search(r"\d", head_txt):
                issues.append("页眉无页码（该页无法参与页码标定）")
            stats.append({
                "page": pno + 1, "n_boxes": len(boxes), "mean_score": mean,
                "regions": regions, "low_conf": low, "issues": issues,
            })
            total_chars += len(body)
            fh.write(f"\n--- 第 {pno + 1} 页 ---\n\n{body}\n")
            fh.flush()
            flag = "⚠️" if (boxes and stats[-1]["mean_score"] < 0.75) else "  "
            print(f"  [{idx}/{len(pages)}] 第 {pno + 1} 页：{len(boxes)} 框 / {len(body)} 字符"
                  f"｜置信 {stats[-1]['mean_score']} {flag}｜累计 {time.time() - t0:.0f}s")

    quality_path.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 3. 人类可读的质量报告：哪些页需要人工复核、为什么 ──────────────
    low_pages = [s["page"] for s in stats if s["n_boxes"] and s["mean_score"] < 0.75]
    issue_pages = [s for s in stats if s.get("issues")]
    low_conf_boxes = [d for s in stats for d in s.get("low_conf", [])]
    md = [f"# OCR 质量报告：{pdf.stem}", "",
          f"- 页面：共 {total} 页，本轮处理 {len(stats)} 页",
          f"- 版式预检：{(profile or {}).get('layout_class', '未预检')}",
          f"- **低置信页（平均 <0.75）**：{low_pages if low_pages else '无'}",
          f"- **有问题的页**：{len(issue_pages)} 页",
          f"- 低置信文字框：{len(low_conf_boxes)} 个（前 40 列于文末）", ""]
    if issue_pages:
        md += ["## 需人工复核的页", "", "| 页 | 问题 | 框数 | 平均置信 |", "| --- | --- | --- | --- |"]
        for s in issue_pages:
            md.append(f"| {s['page']} | {'；'.join(s['issues'])} | {s['n_boxes']} | {s['mean_score']} |")
        md.append("")
    md += ["## 页眉页码可读性（影响页码标定）", "",
           "| 页 | 页眉文字 |", "| --- | --- |"]
    for s in stats[:0]:
        pass
    for s in stats:
        if "页眉无页码" in "；".join(s.get("issues", [])):
            md.append(f"| {s['page']} | （页眉无数字）|")
    if not any("页眉无页码" in "；".join(s.get("issues", [])) for s in stats):
        md.append("| — | 本批各页页眉均含数字或未识别出页眉 |")
    md += ["", "## 低置信文字框（建议逐个核对原页）", "",
           "| 页 | 区域 | 位置(x%,y%) | 置信 | 文本 |", "| --- | --- | --- | --- | --- |"]
    for s in stats:
        for d in s.get("low_conf", []):
            md.append(f"| {s['page']} | {d['region']} | {d['x_pct']},{d['y_pct']} "
                      f"| {d['score']} | {d['text'][:30]} |")
    if not low_conf_boxes:
        md.append("| — | — | — | — | 无 |")
    (out.with_suffix(".quality.md")).write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"\n完成：{out}")
    print(f"本轮 {total_chars} 字符｜耗时 {time.time() - t0:.0f}s")
    print(f"质量报告：{quality_path.name} + {out.with_suffix('.quality.md').name}"
          f"｜低置信页 {low_pages if low_pages else '无'}｜有问题页 {len(issue_pages)}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
