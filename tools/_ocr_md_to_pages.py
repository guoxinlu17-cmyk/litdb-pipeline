# -*- coding: utf-8 -*-
"""把 OCR 版卷文件（第1/4/5卷）转成与 PAGES.md 同一形式的「印刷页码」文本。

问题：这三卷的 `*.OCR.md` 里 `--- 第 N 页 ---` 的 N 是 **PDF 页序**，
印刷页 = N − 偏移（第1卷 +7、第4卷 +7、第5卷 +13）。只改标记形式会让人误读。

做法（**以已验证的偏移为准，页眉只做校验，绝不拿页眉去编造页码**）：
  1) 印刷页 = PDF 页序 − 偏移；≤0 的算卷首，不冒充页码；
  2) 另从每页开头 14 字里试读页眉页码（如「674<文集名>」「1930年<卷目>161」；
     紧跟「年」「月」的数字是年月，跳过；取**第一组**，因为 OCR 页的页码印在行首）；
  3) 两者比对：一致率即对偏移量的全卷复验；不一致的成片出现时说明偏移在主卷中变了，
     单独成表供人工判断（不成片则属 OCR 误识或该页无页码）。
  4) 顺带检查**页码单调递增**，跳号即说明 OCR 漏页。

原 `.OCR.md` 不改动（OCR 流水线的产物，其它脚本仍在用）；新文件为 `.PAGES.md`。

用法：python -B _ocr_md_to_pages.py [卷号...]
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _page_number as PN  # noqa: E402  偏移量自动标定

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

TOOLKIT = Path(__file__).resolve().parent
from _paths import LazyPath, lit_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
LIT = LazyPath(lit_dir)
# 兜底偏移（仅当自动标定失败且未手工指定时使用；正常路径一律走 _page_number 自动标定）
FALLBACK_OFFSETS = {1: 7, 4: 7, 5: 13}
NAMES = {1: "第1卷", 4: "第4卷", 5: "第5卷"}
PAGE_RE = re.compile(r"^--- 第 (\d+) 页 ---$", re.MULTILINE)

HEADER = """# 示例文集 第{vol}卷（按页重建，带印刷页码）

> **来源与可信度说明（务必先读）**
> - 本文件由 `{src}` **转换**而来：内容仍是那一次 OCR 的文本（未重跑 OCR），
>   只把页码标签从「PDF 页序」改为「书页上印的页码」。
> - 页码标记为标题行 `#### 第 N 页`：N 是**印刷页码**，由「原 PDF 页序 − {off}」换算
>   （该偏移量经页号自证与页眉逐页复验）。**使用本文件时偏移量为 0**，
>   与第 2/3/6/7/8 卷形式一致。
> - 页眉页码与换算页码一致率：**{agree}**；不一致 {bad} 页见文末「页码说明」。
> - 相邻页页码连续性：{mono}
> - 卷首（换算后 ≤0）不冒充页码，单列在说明之后。
> - 生成时间：{when}｜生成脚本：`_ocr_md_to_pages.py`
> - **引用仍须回查纸本**：文本来自 OCR，有误识。

---
"""


def head_no(head: str) -> int | None:
    """从页首 14 字里读页眉页码。页码印在行首，故取**第一组**数字；
    紧跟「年」「月」的是年月，跳过。"""
    for g in re.finditer(r"\d(?:\s?\d)*", head):
        nxt = head[g.end():g.end() + 1]
        if nxt in ("年", "月", "日"):
            continue
        v = int(g.group(0).replace(" ", ""))
        return v if 0 < v <= 1500 else None
    return None


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="OCR 文本 → 印刷页码形式（自动标定偏移）")
    ap.add_argument("vols", nargs="*", type=int, default=[])
    ap.add_argument("--offset", type=int, default=None,
                    help="手工指定偏移（PDF页序 − 印刷页）；缺省则自动标定")
    a = ap.parse_args()
    vols = a.vols or [1, 4, 5]
    for vol in vols:
        cands = sorted(LIT.glob(f"*{NAMES[vol]}*.OCR.md"))
        if not cands:
            print(f"第{vol}卷：找不到 OCR.md")
            continue
        src = cands[0]
        text = src.read_text(encoding="utf-8", errors="replace")
        marks = list(PAGE_RE.finditer(text))
        raw = []
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
            raw.append((int(m.group(1)), text[m.end():end]))

        # ── 偏移：优先手工指定，否则**自动标定**（投票 + 结构校验） ──
        cal = PN.calibrate([(pdf, re.sub(r"\s+", "", body)[:24]) for pdf, body in raw])
        if a.offset is not None:
            off, off_src = a.offset, "手工指定"
        elif cal.get("ok"):
            off, off_src = cal["offset"], (
                f"自动标定（{cal['offset_votes']} 页投票，相邻页 +1 一致性 {cal['mono_ratio']:.1%}）")
            if cal["offset_changes"]:
                print(f"  ⚠️ 第{vol}卷偏移疑似分段：{cal['offset_changes']}——请人工确认后再用")
        else:
            print(f"第{vol}卷：❌ 无法标定偏移（{cal.get('reason')}），中止该卷")
            continue
        print(f"第{vol}卷偏移 = {off}（{off_src}）")

        rows, agree, bad = [], 0, []
        for pdf, body in raw:
            printed = pdf - off
            head = re.sub(r"\s+", "", body)[:14]
            by_head = head_no(head)
            if printed > 0 and by_head is not None:
                if by_head == printed:
                    agree += 1
                else:
                    bad.append((pdf, printed, by_head, head))
            rows.append({"pdf": pdf, "printed": printed if printed > 0 else None, "body": body})

        real = [r for r in rows if r["printed"]]
        front = [r for r in rows if not r["printed"]]
        blocks: list[dict] = []
        for r in real:
            if blocks and blocks[-1]["printed"] == r["printed"]:
                blocks[-1]["text"] += "\n" + r["body"]
                continue
            blocks.append({"printed": r["printed"], "text": r["body"]})

        seq = [b["printed"] for b in blocks]
        gaps = [(a, b) for a, b in zip(seq, seq[1:]) if b - a != 1]
        mono = f"{len(blocks) - len(gaps) - 1}/{max(1, len(blocks) - 1)} 对相邻页为 +1"
        if gaps:
            mono += "；跳号：" + "、".join(f"{a}→{b}" for a, b in gaps[:8])

        out = LIT / f"示例文集 第{vol}卷 (示例人物).PAGES.md"
        extra = ""
        if bad:
            extra = ("\n## 页码说明（页眉与换算页码不一致的页）\n\n"
                     "下列各页的页眉数字与「PDF 页序 − 偏移」不同，**多为 OCR 误识或该页本无页码**；\n"
                     "若同一差数连续出现很多页，才说明偏移在卷中发生了变化。\n\n"
                     "| PDF 页 | 换算页码（采用） | 页眉读出 | 页首 14 字 |\n| --- | --- | --- | --- |\n"
                     + "\n".join(f"| {p} | {c} | {h} | {t} |" for p, c, h, t in bad[:120]) + "\n")
        with out.open("w", encoding="utf-8") as fh:
            fh.write(HEADER.format(vol=vol, src=src.name, off=off,
                                   agree=f"{agree}/{len(real)}（{agree/max(1,len(real)):.1%}）",
                                   bad=len(bad), mono=mono, when=time.strftime("%Y-%m-%d %H:%M")))
            if front:
                fh.write("\n## 卷首（无印刷页码）\n\n")
                for r in front:
                    fh.write(f"【原 PDF 第 {r['pdf']} 页】\n{r['body'].strip()}\n\n")
            for b in blocks:
                fh.write(f"\n#### 第 {b['printed']} 页\n\n{b['text'].strip()}\n")
            if extra:
                fh.write(extra)

        t = out.read_text(encoding="utf-8")
        ms = [int(x) for x in re.findall(r"^#### 第 (\d+) 页\s*$", t, re.M)]
        print(f"第{vol}卷：PDF {len(raw)} 页 → {len(blocks)} 页块（卷首 {len(front)}）"
              f"｜印刷页 {min(ms)}–{max(ms)}｜不重复 {len(set(ms))}")
        print(f"   页眉复验一致 {agree}/{len(real)}（{agree/max(1,len(real)):.1%}）｜不一致 {len(bad)}")
        print(f"   相邻页 +1 连续性：{mono}")
        print(f"   已写出 {out.name}（{out.stat().st_size/1024/1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
