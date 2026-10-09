# -*- coding: utf-8 -*-
"""把已定位的印刷页码写进论文的卷次标注（保留「待回查纸本」标记）。

规则：
  * 只改【《示例文集》第 X 卷…】这一层标注，正文一字不动；
  * 页码插在「卷」字之后、原有附加语（如「，1930 年」）之前；
  * 所有页码一律带「（待回查纸本）」——它们出自 OCR 定位，定稿前必须按纸本核对；
  * 逐字引语用「第 N 页」，转述/线索用「约第 N 页」；
  * 运行前自动备份为 `*.bak-<时间戳>`。

用法：
    python -B _insert_page_numbers.py            # 试运行，只打印将做的改动
    python -B _insert_page_numbers.py --apply    # 真正写入（先备份）
"""
import argparse
import csv
import io
import re
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cite_page_candidates as C  # noqa: E402

from _paths import db_dir  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
DB = Path(db_dir())
PAPERS = {p.stem: p for p in C.DEFAULT_PAPERS}

# ── 人工登记的「线索页码」 ────────────────────────────────────────────────
# CSV 里状态非 ✅、但已有充分证据的页码（转述类 / OCR 误识类），在此逐条登记。
#   key   = (论文 stem, 引注序号)
#   value = (要插入的页码文本, 依据说明)
#
# ⚠️ 本仓库留空 —— 这是给你填自己项目的地方。格式示例（**合成数据**）：
#     ("my-paper", 3): ("约第 42 页", "原文「……」（此处省略）在印刷 41–42 页，语义对应"),
#     ("my-paper", 7): ("约第 88 页", "同 my-paper 第 3 处，OCR 误识，靠上下文断定"),
MANUAL: dict[tuple[str, int], tuple[str, str]] = {}

# ── 「同一句在卷中两见」这类特例 ──────────────────────────────────────────
# 当引注标记里含 SPECIAL_MARKER 时，改用下面的固定页码与依据，**不依赖序号**。
# 例（合成）：某句在第 5 卷两见，需人工指定双页码 → SPECIAL_PAGES = "第 129、279 页"。
# 留空则这条特例永不触发。
SPECIAL_MARKER = ""   # 例：改成你项目里标识该特例的字样
SPECIAL_PAGES  = ""   # 例："第 129、279 页"
SPECIAL_BASIS  = ""   # 例："同一句在第 N 卷两见（两处引述）"


def seq_rows() -> dict[str, dict[int, dict]]:
    rows = list(csv.DictReader(io.open(DB / "引文页码候选.csv", encoding="utf-8-sig")))
    out: dict[str, dict[int, dict]] = {}
    for r in rows:
        out.setdefault(r["来源论文"], {})[int(r["序号"])] = r
    # 无扫描件各卷（第2/3/6/7/8卷）：由 docx 逐页索引定出的页码
    full_csv = DB / "无扫描卷页码候选.csv"
    if full_csv.exists():
        for r in csv.DictReader(io.open(full_csv, encoding="utf-8-sig")):
            page = (r.get("印刷页码") or "").strip()
            if not page.isdigit():
                continue
            out.setdefault(r["论文"], {})[int(r["序号"])] = {
                "状态": "✅ 候选(卷内页)", "印刷页码候选": f"第{page}页",
                "抽取方式": r.get("方式", ""), "相似度": r.get("相似度", ""),
                "页号自证": r.get("自证", ""), "来源": "docx逐页索引（未OCR）",
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    by_paper = seq_rows()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    total = 0
    seq = 0          # ⚠️ 序号是**跨两篇论文连续**的全局序号，与 CSV 对应；不能每篇重置

    for paper in C.DEFAULT_PAPERS:
        text = paper.read_text(encoding="utf-8")
        rows = by_paper.get(paper.stem, {})
        edits: list[tuple[int, int, str, str]] = []   # (seq, 插入位置, 插入文本, 说明)
        for m in C.CITE_RE.finditer(text):
            if C.CN_NUM.get(m.group(1)) is None:
                continue
            seq += 1
            marker = m.group(0)
            page_txt, basis = None, ""
            r = rows.get(seq)
            if r and r["状态"].startswith("✅"):
                page_txt = r["印刷页码候选"]
                basis = f"{r['抽取方式']}｜相似度 {r['相似度']}｜{r['页号自证']}"
            elif (paper.stem, seq) in MANUAL:
                page_txt, basis = MANUAL[(paper.stem, seq)]
            # 特例：标记里含 SPECIAL_MARKER 时改用固定页码（须先在文件头配置）
            if SPECIAL_MARKER and SPECIAL_MARKER in marker:
                page_txt, basis = SPECIAL_PAGES, SPECIAL_BASIS
            if not page_txt:
                continue
            vm = re.search(r"第\s*[一二三四五六七八1-8]\s*卷", marker)
            if not vm:
                continue
            anchor = m.start() + vm.end()
            suffix = "；" if (SPECIAL_MARKER and SPECIAL_MARKER in marker) else "，"
            edits.append((seq, anchor, f"{suffix}{page_txt}（待回查纸本）", basis))

        print(f"\n{'='*92}\n{paper.name}：将插入 {len(edits)} 处页码\n{'='*92}")
        for s, pos, ins, basis in edits:
            ctx = text[max(0, pos - 34):pos + 30].replace("\n", " ")
            print(f"  [{s:>2}] …{ctx}…")
            print(f"        → 插入「{ins}」   依据：{basis}")

        if not edits:
            continue
        if a.apply:
            backup = paper.with_suffix(paper.suffix + f".bak-{stamp}")
            shutil.copy2(paper, backup)
            new = text
            for s, pos, ins, _b in sorted(edits, key=lambda x: -x[1]):
                new = new[:pos] + ins + new[pos:]
            paper.write_text(new, encoding="utf-8")
            print(f"  ✅ 已写入（原文件备份：{backup.name}）")
        total += len(edits)

    print(f"\n合计可插入 {total} 处。")
    if not a.apply:
        print("（试运行：未写入任何文件。加 --apply 生效。）")
    return 0


def selftest() -> int:
    """自测：在临时目录里造夹具库 + 两篇夹具论文，跑一遍 dry-run 与 apply。

    它改的是**论文正文**，所以自测必须证明三件事：
      ① dry-run 一个字都不写；② 写入后**除插入点外逐字不变**；③ 只改该改的标记。
    """
    import contextlib
    import io as _io
    import tempfile
    global DB, SPECIAL_MARKER, SPECIAL_PAGES, SPECIAL_BASIS
    # 自测临时启用「特例」通路（合成夹具），测完还原
    SPECIAL_MARKER, SPECIAL_PAGES, SPECIAL_BASIS = "引他人语", "第 129、279 页", "同一句在第 5 卷两见（合成夹具）"
    checks: list[tuple[str, bool, str]] = []

    def add(name, cond, info=""):
        checks.append((name, bool(cond), str(info)))

    P1 = ("开头一句废话。\n\n"
          "他说过【《示例文集》第五卷，1930 年】这样的话，后面还有。\n\n"
          "第二处引注【《示例文集》第三卷】没有候选页码。\n\n"
          "第三处【《示例文集》第五卷，引他人语】是特例。\n\n"
          "第四处【《示例文集》第二卷】同样没有页码。\n\n"
          "这不是引注【随便一个方括号内容】应被忽略。\n")
    P2 = "另一篇的开头。\n\n这里引用【《示例文集》第一卷】作为结尾。\n"
    CSV = ("来源论文,序号,状态,印刷页码候选,抽取方式,相似度,页号自证\n"
           "夹具论文A,1,✅ 候选,第 63 页,keyword,0.95,命中页印着 63\n"
           "夹具论文B,5,✅ 候选,第 88 页,vector,0.91,命中页印着 88\n")

    tmp = Path(tempfile.mkdtemp(prefix="insertpn-selftest-"))
    try:
        dbdir = tmp / "db"
        dbdir.mkdir()
        (dbdir / "引文页码候选.csv").write_text(CSV, encoding="utf-8-sig")
        p1 = tmp / "夹具论文A.md"
        p2 = tmp / "夹具论文B.md"
        p1.write_text(P1, encoding="utf-8")
        p2.write_text(P2, encoding="utf-8")
        p1_orig, p2_orig = p1.read_text(encoding="utf-8"), p2.read_text(encoding="utf-8")

        saved_db, saved_papers, saved_argv = DB, C.DEFAULT_PAPERS, sys.argv
        try:
            DB, C.DEFAULT_PAPERS = dbdir, [p1, p2]
            # ① dry-run
            sys.argv = ["_insert_page_numbers.py"]
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                main()
            out1 = buf.getvalue()
            add("① dry-run 不改动任何文件",
                p1.read_text(encoding="utf-8") == p1_orig
                and p2.read_text(encoding="utf-8") == p2_orig
                and not list(tmp.glob("*.bak-*")), "")
            add("② dry-run 计划：A 篇 2 处、B 篇 1 处",
                "将插入 2 处" in out1 and "将插入 1 处" in out1,
                out1.splitlines()[0] if out1 else "")
            # ② apply
            sys.argv = ["_insert_page_numbers.py", "--apply"]
            buf2 = _io.StringIO()
            with contextlib.redirect_stdout(buf2):
                main()
            t1 = p1.read_text(encoding="utf-8")
            t2 = p2.read_text(encoding="utf-8")
            baks = sorted(tmp.glob("*.bak-*"))
            add("③ 写入前各留一份备份（2 个）", len(baks) == 2,
                "、".join(b.name for b in baks))
            add("④ 备份内容 = 原文",
                baks and any(b.read_text(encoding="utf-8") == p1_orig for b in baks),
                "")
            ins1 = "，第 63 页（待回查纸本）"
            ins2 = "；第 129、279 页（待回查纸本）"
            ins3 = "，第 88 页（待回查纸本）"
            add("⑤ A 篇插入两处（普通 + 特例）",
                t1.count(ins1) == 1 and t1.count(ins2) == 1, "")
            add("⑥ B 篇插入一处（全局序号跨论文连续：第 5 处 → 第 88 页）",
                t2.count(ins3) == 1, t2.replace("\n", " ")[:80])
            # ③ 除插入点外逐字不变
            sub1 = t1.replace(ins1, "").replace(ins2, "")
            sub2 = t2.replace(ins3, "")
            add("⑦ A 篇除插入点外**逐字不变**", sub1 == p1_orig,
                "" if sub1 == p1_orig else "长度 %d vs %d" % (len(sub1), len(p1_orig)))
            add("⑧ B 篇除插入点外**逐字不变**", sub2 == p2_orig, "")
            # ④ 位置：在「卷」之后、附加语之前
            add("⑨ 插入位置在「卷」之后、「，1930 年」之前",
                "第五卷，第 63 页（待回查纸本），1930 年" in t1, "")
            add("⑩ 特例用「；」且为多页形式",
                "第五卷；第 129、279 页（待回查纸本），引他人语" in t1, "")
            add("⑪ 无候选页码的标记（第三卷/第二卷）未被改动",
                "【《示例文集》第三卷】" in t1 and "【《示例文集》第二卷】" in t1, "")
            add("⑫ 非引注方括号未受影响",
                "【随便一个方括号内容】" in t1, "")
        finally:
            DB, C.DEFAULT_PAPERS, sys.argv = saved_db, saved_papers, saved_argv
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, c, _ in checks if c)
    print("_insert_page_numbers.py 自测：")
    for name, c, info in checks:
        print("  %s %s" % ("PASS" if c else "FAIL", name))
        if not c and info:
            print("       %s" % info)
    print("自测：%d/%d 通过" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


def cli() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    return main()


if __name__ == "__main__":
    raise SystemExit(cli())
