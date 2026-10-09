#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_interview_experiment.py — 访谈库最小验证实验（入库 → 检索 → 中英对照）

它回答三个问题（在**独立实验库**里，绝不碰任何正式库）
======================================================
1. **能不能入库、代价多大**：几个转写稿 → 文档数/块数/向量数/耗时；
   顺便算出 CPU 上"每千块嵌入耗时"的量级（用于估算大批量重灌的时间）。
2. **中文提问能不能命中**：给每个问题配一个"预期命中的独特片段"，
   算出 Top-1 / Top-5 命中率。
3. **中文 vs 英文提问**：同一批问题各问一遍。文献库的经验是"中文提问先转英文"，
   但引擎 BM25 **只对 CJK 二元组建索引**（见 kb_engine._library_cjk_share 注释），
   在纯中文访谈库上这条经验应当相反——本实验就是来量它的。

⚠️ 这个实验的定位（别过度解读）
===============================
内置样本只有 3 篇 / 6 个问题 / 6 个分块。**这个规模只能验证"机制通不通"，
不能得出检索质量结论**。要谈质量，必须用你自己的转写稿和你自己的问题：
    --src <你的转写稿目录> --questions <questions.csv>
questions.csv 格式（UTF-8，首行表头）：
    zh_question,expected_phrase,en_question
    家里靠什么补贴家用,半夜里挑着担子去镇上卖菜,how did the family earn extra money
expected_phrase 必须**只出现在你希望命中的那一处**（先在转写稿里搜一遍确认唯一）。

用法
====
  python _interview_experiment.py --selftest                 # 内置样本跑一遍并自检
  python _interview_experiment.py --lab "<某目录>\\interview-lab"     # 建实验库并出报告
  python _interview_experiment.py --lab ... --src "<转写稿>" --questions q.csv --top-k 5

实验库位置由 --lab 指定；不指定则用 <当前目录>\\.interview-lab\\<时间戳>。
**已有的库目录会被拒绝**（防止误灌正式库）：如果 --lab 下已有 kb.sqlite，除非加 --reuse。
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ENGINE_ENV = "KB_ENGINE"


def find_engine(explicit=None):
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if os.environ.get(ENGINE_ENV):
        cands.append(Path(os.environ[ENGINE_ENV]))
    home = Path.home()
    cands += sorted((home / ".dsh" / "profiles").glob("*/node_modules/dsh-kb-rag/kb_engine.py"))
    cands += sorted((home / ".dsh").glob("**/dsh-kb-rag/kb_engine.py"))
    seen, uniq = set(), []
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r in seen or not r.is_file():
            continue
        seen.add(r)
        uniq.append(r)
    if not uniq:
        raise SystemExit("找不到 kb_engine.py。请用 --engine 指定，或设环境变量 %s。" % ENGINE_ENV)
    uniq.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return uniq[0]


def load_engine(path):
    spec = importlib.util.spec_from_file_location("kb_engine_for_exp", str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 内置样本：3 场访谈，中文口语，带时间码/说话人，每处一个独特片段 ──
SAMPLES: dict[str, str] = {
    "A03 - 2024 - 首次访谈.md": """# A03 - 2024-03-11 - 首次访谈

## [00:00:10] 开场与知情同意

A03：[00:00:10] 先说明一下，这次访谈完全自愿，任何问题都可以不回答，随时可以停下来。
B01：好的，我明白，那我们开始吧，我会尽量把当时的情况讲清楚。

## [00:03:20] 家庭背景

A03：你小时候家里一共几口人，父母当时是做什么工作的？
B01：五口人，我排行老二，父亲在供销社上班，母亲在家种地，日子过得不算宽裕。

## [00:12:35] 早年谋生

A03：家里那时候靠什么补贴家用？
B01：半夜里挑着担子去镇上卖菜，天不亮就得往回赶，回来还得去上学。
""",
    "A04 - 2024 - 首次访谈.md": """# A04 - 2024-05-06 - 首次访谈

## [00:02:00] 转学经历

A04：[00:02:00] 你中学转过学吗，当时具体是什么情况？
B02：中学那几年赶上搬家，转过一次学，新学校用的是方言讲课，我一句都听不懂。

## [00:20:10] 工作变动

A04：后来工作换过几次，都是什么原因？
B02：换过四次，最后一次是厂子改制，我跟着师傅去了南边的城市打工。
""",
    "A05 - 2024 - 首次访谈.md": """# A05 - 2024-02-18 - 首次访谈

## [00:05:00] 婚嫁与分家

A05：[00:05:00] 你结婚以后是跟父母一起住吗？
B03：结婚第二年就分家了，分到一间半瓦房，锅碗瓢盆都是我们自己置办的。

## [00:30:00] 对子女的期望

A05：你对孩子读书这件事怎么看？
B03：我那时候就一个念头，砸锅卖铁也要让孩子把书念完。
""",
}

# (中文问题, 预期片段, 英文问题, 预期来自哪个文件)
QUESTIONS: list[tuple[str, str, str, str]] = [
    ("家里那时候靠什么补贴家用", "半夜里挑着担子去镇上卖菜",
     "how did the family earn extra money", "A03 - 2024 - 首次访谈.md"),
    ("父亲当时是做什么工作的", "供销社",
     "what did the father do for a living", "A03 - 2024 - 首次访谈.md"),
    ("中学转学是什么情况", "新学校用的是方言讲课",
     "what happened when he transferred schools", "A04 - 2024 - 首次访谈.md"),
    ("工作为什么换了好几次", "厂子改制",
     "why did he change jobs so many times", "A04 - 2024 - 首次访谈.md"),
    ("结婚以后住在哪里", "分到一间半瓦房",
     "where did they live after getting married", "A05 - 2024 - 首次访谈.md"),
    ("对孩子读书有什么想法", "砸锅卖铁也要让孩子把书念完",
     "what were his hopes for his children's education", "A05 - 2024 - 首次访谈.md"),
]


def squash(s: str) -> str:
    return "".join((s or "").split())


def build_samples(root: Path, src: Path | None) -> Path:
    """把样本（内置或 --src 拷贝）放到 root/transcripts/。返回该目录。"""
    dst = root / "transcripts"
    dst.mkdir(parents=True, exist_ok=True)
    if src is not None:
        n = 0
        for p in sorted(src.rglob("*")):
            if p.is_file() and p.suffix.lower() in {".txt", ".md", ".markdown", ".docx"}:
                shutil.copy2(p, dst / p.name)
                n += 1
        if not n:
            raise SystemExit("--src 下没有可入库的文本文件：%s" % src)
    else:
        for name, content in SAMPLES.items():
            (dst / name).write_text(content, encoding="utf-8", newline="\n")
    return dst


def load_questions(path: Path | None):
    if path is None:
        return [{"zh": q, "expect": e, "en": en, "file": f} for q, e, en, f in QUESTIONS]
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            row = [c.strip() for c in row]
            if not row or row[0].lower() in ("zh_question", "question") or len(row) < 2:
                continue
            out.append({"zh": row[0], "expect": row[1],
                        "en": row[2] if len(row) > 2 else "",
                        "file": row[3] if len(row) > 3 else ""})
    if not out:
        raise SystemExit("questions CSV 里没有有效行（需要 zh_question,expected_phrase[,en_question]）")
    return out


def ingest(mod, kb_root: Path, paths: list[str], force: bool = False) -> dict:
    t0 = time.time()
    resp = mod.cmd_ingest({"kb_root": str(kb_root), "paths": paths, "force": force})
    resp["_elapsed_s"] = round(time.time() - t0, 2)
    resp["_forced"] = bool(force)
    return resp


def search(mod, kb_root: Path, query: str, top_k: int, snippet: int) -> dict:
    return mod.cmd_search({"kb_root": str(kb_root), "query": query, "top_k": top_k,
                           "snippet": snippet, "depth": "quick"})


def evaluate(mod, kb_root: Path, questions, top_k: int, snippet: int) -> list[dict]:
    rows = []
    for q in questions:
        # 'zh'/'en' 保存**问题原文**（供报告显示），结果放 'zh_res'/'en_res'——
        # 曾经把结果写回 row[lang]，导致问题原文被结果 dict 覆盖（报告首列变成 dict）。
        row = {"zh": q["zh"], "en": q["en"], "expect": q["expect"], "file": q["file"]}
        for lang in ("zh", "en"):
            text = q[lang]
            if not text:
                row[lang + "_res"] = None
                continue
            t0 = time.time()
            try:
                resp = search(mod, kb_root, text, top_k, snippet)
                res = resp.get("results") or []
                hit_idx = [i for i, r in enumerate(res)
                           if squash(q["expect"]) in squash(r.get("snippet", ""))]
                row[lang + "_res"] = {
                    "ms": round((time.time() - t0) * 1000),
                    "n": len(res),
                    "top1": bool(hit_idx) and hit_idx[0] == 0,
                    "topk": bool(hit_idx),
                    "hit_rank": (hit_idx[0] + 1) if hit_idx else None,
                    "verdict": resp.get("verdict"),
                    "no_hit": resp.get("no_hit"),
                    "max_score": resp.get("max_score"),
                    "top1_file": (res[0].get("file") if res else None),
                    "top1_section": (res[0].get("section") if res else None),
                    "lang_note": resp.get("lang_note"),
                    "error": resp.get("error"),
                }
            except Exception as e:
                row[lang + "_res"] = {"error": "%s: %s" % (type(e).__name__, e)}
        rows.append(row)
    return rows


def render_report(engine, mod, lab: Path, kb_root: Path, ing: dict, rows: list[dict],
                  dev: dict | None, out: Path, pure: dict | None = None) -> dict:
    tot = ing.get("totals") or {}
    docs = tot.get("added", 0) + tot.get("updated", 0)
    chunks = tot.get("chunks", 0)
    vecs = tot.get("vectors", 0)
    el = ing.get("_elapsed_s") or 0
    per_1k = round(el / chunks * 1000, 1) if chunks else None
    extrapolable = bool(chunks) and chunks >= 200

    def rate(lang, key):
        vals = [r[lang + "_res"][key] for r in rows if r.get(lang + "_res")]
        return (sum(1 for v in vals if v), len(vals))

    L = ["# 访谈库最小验证实验报告", "",
         "- 引擎：`%s`（VERSION %s / PARSER_REV %d）" % (engine, mod.VERSION, mod.PARSER_REV),
         "- 实验库：`%s`（**独立目录，未触碰任何正式库**）" % kb_root,
         "- 样本目录：`%s`" % (lab / "transcripts"),
         "- ⚠️ 规模仅为**机制验证**：%d 篇 / %d 块 / %d 个问题。要谈检索质量必须换真实语料与问题。"
         % (docs, chunks, len(rows)),
         ""]
    L += ["## 一、入库代价", "", "| 项 | 值 |", "| --- | --- |",
          "| 文档数 | %d |" % docs,
          "| 分块数 | %d |" % chunks,
          "| 向量数 | %d |" % vecs,
          "| 首次入库耗时（**含模型加载**） | %s s |" % el,
          "| 首次「每千块」 | %s |" % (
              ("%s s" % per_1k) if extrapolable else
              "**不可外推**（样本仅 %d 块，耗时被模型加载主导）" % chunks),
          "| 错误 / 跳过 / 重复 | %s / %s / %s |"
          % (tot.get("errors"), tot.get("skipped"), tot.get("duplicates")), ""]
    if pure:
        L += ["**纯处理速率**（force 重灌一次，模型已在内存）：%s s / %s 块 → "
              "**每千块 %s s**；这是可用来估算大批量重灌的量级（单机 CPU，"
              "样本仍偏小，误差可能有数倍）。" % (pure["s"], pure["chunks"], pure["per_1k"]), ""]
    if chunks and vecs == 0:
        L += ["> ❌ **向量数为 0**：嵌入模型没跑起来（模型未缓存或离线）。"
              "此时检索只剩 BM25 关键词路，命中率不代表正常表现。", ""]
    if dev:
        L += ["**设备**：`%s`" % json.dumps(dev, ensure_ascii=False)[:300], ""]

    L += ["## 二、检索命中（中英对照）", "",
          "| 问题（中文） | 预期片段 | 中文 Top-1 | 中文 Top-5 | 英文 Top-1 | 英文 Top-5 |",
          "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        def mark(lang, key):
            v = r.get(lang + "_res")
            if not v:
                return "—"
            if v.get("error"):
                return "错误"
            return "✅" if v.get(key) else "❌"
        L.append("| %s | %s | %s | %s | %s | %s |"
                 % (r["zh"], r["expect"], mark("zh", "top1"), mark("zh", "topk"),
                    mark("en", "top1"), mark("en", "topk")))
    L.append("")
    z1, n1 = rate("zh", "top1")
    zk, _ = rate("zh", "topk")
    e1, ne = rate("en", "top1")
    ek, _ = rate("en", "topk")
    L += ["| 汇总 | 中文 | 英文 |", "| --- | --- | --- |",
          "| Top-1 命中 | %d/%d | %d/%d |" % (z1, n1, e1, ne),
          "| Top-5 命中 | %d/%d | %d/%d |" % (zk, n1, ek, ne), ""]
    if n1 and ne:
        if z1 > e1:
            L += ["> **结论**：在中文访谈语料上，**中文提问优于英文提问**"
                  "（Top-1 %d/%d vs %d/%d）。这与『文献库先转英文术语』的经验相反——"
                  "因为引擎 BM25 只对 CJK 二元组建索引，中文语料上英文查询两条路都吃亏。" % (z1, n1, e1, ne), ""]
        elif z1 == e1:
            L += ["> 本次中英命中率相同（样本太小，不足以区分）。", ""]
        else:
            L += ["> ⚠️ 本次**英文提问反而更好**（%d/%d vs %d/%d）——与预期相反，"
                  "请结合 `lang_note` 与具体结果排查（例如查询词恰好与某段文字重叠）。"
                  % (e1, ne, z1, n1), ""]

    L += ["## 三、逐题明细", ""]
    for r in rows:
        L += ["### %s" % r["zh"], ""]
        L += ["- 预期片段：`%s`（来自 `%s`）" % (r["expect"], r["file"] or "未指定"), ""]
        for lang, label in (("zh", "中文"), ("en", "英文")):
            v = r.get(lang + "_res")
            if not v:
                continue
            if v.get("error"):
                L += ["- %s：错误 %s" % (label, v["error"])]
                continue
            L += ["- %s查询 `%s` → 命中位次 %s，verdict=%s，最高分=%s，Top-1 文件=%s"
                  % (label, r[lang], v.get("hit_rank"), v.get("verdict"),
                     v.get("max_score"), v.get("top1_file"))]
            if v.get("lang_note"):
                L += ["  - 引擎语言提示：%s" % v["lang_note"]]
        L.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    return {"docs": docs, "chunks": chunks, "vecs": vecs, "elapsed_s": el,
            "per_1k_s": per_1k, "extrapolable": extrapolable,
            "pure_s": (pure or {}).get("s"), "pure_per_1k_s": (pure or {}).get("per_1k"),
            "zh_top1": z1, "zh_n": n1, "en_top1": e1, "en_n": ne,
            "zh_topk": zk, "en_topk": ek, "errors": tot.get("errors")}


def run(lab: Path, src: Path | None, questions_path: Path | None, top_k: int,
        snippet: int, engine: Path, mod, reuse: bool, quiet=False) -> tuple[dict, Path]:
    kb_root = lab / "kb"
    if (kb_root / "kb.sqlite").exists() and not reuse:
        raise SystemExit(
            "实验库已存在：%s\n为防止误灌，请换一个 --lab 目录，或加 --reuse 复用它。"
            % (kb_root / "kb.sqlite"))
    lab.mkdir(parents=True, exist_ok=True)
    tdir = build_samples(lab, src)
    questions = load_questions(questions_path)
    if not quiet:
        print("lab    : %s" % lab)
        print("samples: %s" % tdir)
        print("kb_root: %s" % kb_root)
    ing = ingest(mod, kb_root, [str(tdir)])
    if not ing.get("ok", True) and ing.get("error"):
        raise SystemExit("入库失败：%s" % ing["error"])
    tot = ing.get("totals") or {}
    if not quiet:
        print("ingest : added=%s chunks=%s vectors=%s errors=%s  %.2fs"
              % (tot.get("added"), tot.get("chunks"), tot.get("vectors"),
                 tot.get("errors"), ing.get("_elapsed_s") or 0))
    # 纯处理速率：样本小时首次入库被"模型加载开销"主导，那个数字**不可外推**。
    # 所以再 force 灌一次（模型已在内存）测真正的处理速率。
    pure = None
    if (tot.get("chunks") or 0) < 200:
        ing2 = ingest(mod, kb_root, [str(tdir)], force=True)
        t2 = ing2.get("totals") or {}
        pure = {"s": ing2.get("_elapsed_s"),
                "chunks": t2.get("chunks"),
                "per_1k": round((ing2.get("_elapsed_s") or 0) / (t2.get("chunks") or 1) * 1000, 1)}
        if not quiet:
            print("re-ingest(force, 模型已加载): %.2fs / %s 块 → 每千块 %ss"
                  % (pure["s"], pure["chunks"], pure["per_1k"]))
    rows = evaluate(mod, kb_root, questions, top_k, snippet)
    if not quiet:
        for r in rows:
            z = (r.get("zh_res") or {}).get("hit_rank")
            e = (r.get("en_res") or {}).get("hit_rank")
            print("  %-18s 中文#%s  英文#%s   expect=%s" % (r["zh"][:18], z, e, r["expect"]))
    dev = None
    try:
        dev = mod.device_report()
    except Exception:
        pass
    out = lab / "实验结果.md"
    summary = render_report(engine, mod, lab, kb_root, ing, rows, dev, out, pure)
    return {"ingest": ing, "rows": rows, "summary": summary, "report": out}, out


def selftest() -> int:
    print("== _interview_experiment.py 自测（= 用内置样本真跑一遍实验）==")
    engine = find_engine()
    mod = load_engine(engine)
    print("引擎：%s VERSION %s rev%d" % (engine, mod.VERSION, mod.PARSER_REV))
    checks: list[tuple[str, bool, str]] = []
    tmp = Path(tempfile.mkdtemp(prefix="ivexp_"))

    def add_check(name, cond, info=""):
        checks.append((name, bool(cond), info))

    try:
        lab = tmp / "lab"
        res, out = run(lab, None, None, 5, 1500, engine, mod, reuse=False, quiet=True)
        s = res["summary"]
        add_check("① 入库 3 篇文档", s["docs"] == 3, "docs=%s" % s["docs"])
        add_check("② 分块数 >0", (s["chunks"] or 0) > 0, "chunks=%s" % s["chunks"])
        add_check("③ 向量数 == 分块数（嵌入链路通）",
                  s["vecs"] == s["chunks"] and (s["chunks"] or 0) > 0,
                  "vecs=%s chunks=%s" % (s["vecs"], s["chunks"]))
        add_check("④ 无入库错误", (s["errors"] or 0) == 0, "errors=%s" % s["errors"])
        add_check("⑤ 中文提问 Top-5 全中", s["zh_topk"] == s["zh_n"] and s["zh_n"] > 0,
                  "zh_topk=%s/%s" % (s["zh_topk"], s["zh_n"]))
        add_check("⑥ 中文提问 Top-1 命中过半", s["zh_top1"] * 2 > s["zh_n"],
                  "zh_top1=%s/%s" % (s["zh_top1"], s["zh_n"]))
        add_check("⑦ 中文命中率 高于/不低于 英文",
                  s["zh_top1"] >= s["en_top1"],
                  "zh=%s/%s en=%s/%s" % (s["zh_top1"], s["zh_n"], s["en_top1"], s["en_n"]))
        add_check("⑧ 报告与实验库写出",
                  out.is_file() and (lab / "kb" / "kb.sqlite").is_file(), str(out))
        add_check("⑨ 实验库与正式库隔离（kb_root 在 lab 下）",
                  str(lab / "kb") in (out.read_text("utf-8")), "")
        add_check("⑩ 未指定 --lab 时不得写入工作区（仅用临时目录）",
                  "ivexp_" in str(lab), str(lab))
        # 已存在的实验库必须被拒绝（防误灌）
        refused = False
        try:
            run(lab, None, None, 5, 1500, engine, mod, reuse=False, quiet=True)
        except SystemExit:
            refused = True
        add_check("⑪ 已有实验库时拒绝再灌（除非 --reuse）", refused, "")
        # 计入耗时量级（区分"含模型加载"与"纯处理"）
        add_check("⑫ 首次耗时被标为不可外推（样本<200块）",
                  s.get("extrapolable") is False, "extrapolable=%s" % s.get("extrapolable"))
        add_check("⑬ 给出模型已加载后的纯处理速率",
                  (s.get("pure_per_1k_s") or 0) > 0, "pure=%s" % s.get("pure_per_1k_s"))
        rpt = out.read_text("utf-8")
        add_check("⑭ 报告含全部问题原文（结果 dict 未覆盖问题文本）",
                  all(r["zh"] in rpt for r in res["rows"]),
                  "missing=%s" % [r["zh"] for r in res["rows"] if r["zh"] not in rpt])
        add_check("⑭a 报告无 dict 泄漏（'{'ms'' 不出现）", "'ms':" not in rpt, "")
        add_check("⑭b 报告含预期片段与命中位次",
                  all(r["expect"] in rpt for r in res["rows"]) and "命中位次" in rpt, "")
        print("首次入库 %.2fs（含模型加载，%s 块，不可外推）" % (s["elapsed_s"], s["chunks"]))
        print("纯处理   %.2fs / %s 块 → 每千块 %ss" % (s["pure_s"], s["chunks"], s["pure_per_1k_s"]))
        print("中文 Top-1 %s/%s，Top-5 %s/%s；英文 Top-1 %s/%s，Top-5 %s/%s"
              % (s["zh_top1"], s["zh_n"], s["zh_topk"], s["zh_n"],
                 s["en_top1"], s["en_n"], s["en_topk"], s["en_n"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, good, _ in checks if good)
    for name, good, info in checks:
        print("%s %s" % ("PASS" if good else "FAIL", name))
        if not good and info:
            print("     %s" % info)
    print("\n自测：%d/%d 通过" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="访谈库最小验证实验（独立实验库）")
    ap.add_argument("--lab", help="实验目录（默认 <cwd>/.interview-lab/<时间戳>）")
    ap.add_argument("--src", help="用你自己的转写稿目录（默认用内置样本）")
    ap.add_argument("--questions", help="questions.csv：zh_question,expected_phrase[,en_question]")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--snippet", type=int, default=1500, help="返回片段宽度（需覆盖预期片段）")
    ap.add_argument("--reuse", action="store_true", help="复用已存在的实验库")
    ap.add_argument("--engine", help="kb_engine.py 路径（默认自动定位）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    engine = find_engine(args.engine)
    mod = load_engine(engine)
    lab = Path(args.lab) if args.lab else (
        Path.cwd() / ".interview-lab" / time.strftime("%Y%m%d-%H%M%S"))
    res, out = run(lab, Path(args.src) if args.src else None,
                   Path(args.questions) if args.questions else None,
                   args.top_k, args.snippet, engine, mod, args.reuse)
    s = res["summary"]
    print("\n--- 汇总 ---")
    print("docs=%s chunks=%s vecs=%s" % (s["docs"], s["chunks"], s["vecs"]))
    print("首次入库 %.2fs（含模型加载，样本小则**不可外推**）；纯处理每千块 %ss"
          % (s["elapsed_s"], s["pure_per_1k_s"]))
    print("中文 Top-1 %s/%s  Top-5 %s/%s" % (s["zh_top1"], s["zh_n"], s["zh_topk"], s["zh_n"]))
    print("英文 Top-1 %s/%s  Top-5 %s/%s" % (s["en_top1"], s["en_n"], s["en_topk"], s["en_n"]))
    print("report : %s" % out)
    print("(console CJK may be mojibake; read the report file)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
