#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键 demo：把一份「一页一节 + 页眉带印刷页码」的 docx 变成带页码标记的 Markdown。

跑通它**不需要配置任何项目**——脚本自己在临时目录搭一个最小骨架。
（真实使用时你要指向自己的项目，见 README「快速开始·第 2 步」。）

    python examples/demo.py

演示的是路线②：**docx 页眉法（免 OCR）**。选它是因为它最能说明本项目的核心主张——
**页码从哪来**：不是 PDF 页序，而是书页上印着的那个数字，取自每页的页眉。

样例里的印刷页码刻意从 101 起，所以你会看到：6 页素材，出来的页标记是 101–106。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
EXAMPLES = REPO / "examples"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CORPUS = "示例文集"
TAG = "1"


def setup_project(root: Path) -> dict[str, str]:
    """搭一个最小项目骨架，返回环境变量。与 run_selftests.py 同一套做法。"""
    (root / "01_文献").mkdir(parents=True, exist_ok=True)
    (root / "03_论文").mkdir(parents=True, exist_ok=True)
    (root / "05_文献数据库").mkdir(parents=True, exist_ok=True)
    kb = root / ".kb"
    kb.mkdir(parents=True, exist_ok=True)

    # 空壳库（0 文档）不算候选，所以塞一行进去
    con = sqlite3.connect(kb / "kb.sqlite")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS docs("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, title TEXT, sha256 TEXT, size INTEGER);")
    con.execute("INSERT INTO docs(path,title,sha256,size) VALUES (?,?,?,?)",
                ("demo.txt", "夹具文档", "0" * 64, 123))
    con.commit()
    con.close()

    (root / "项目布局.json").write_text(
        '{\n  "kb": ".kb",\n  "db": "05_文献数据库",\n'
        '  "lit": "01_文献",\n  "papers": "03_论文"\n}\n', encoding="utf-8")
    return {"LITDB_PROJECT": str(root)}


def run(cmd: list[str], env: dict[str, str]) -> int:
    r = subprocess.run(cmd, env=env, cwd=str(TOOLS), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print(r.stderr.rstrip()[:800], file=sys.stderr)
    return r.returncode


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="litdb-demo-"))
    try:
        env = dict(os.environ)
        env.update(setup_project(tmp))
        lit = tmp / "01_文献"

        print("=" * 78)
        print("① 生成合成样例（一页一节，每节页眉印着书上的页码）")
        print("=" * 78)
        sample = lit / f"{CORPUS} 第1卷.docx"
        if run([sys.executable, str(EXAMPLES / "make_sample_docx.py"),
                str(sample), "--pages", "6", "--start", "101", "--corpus", CORPUS], env) != 0:
            return 1

        print()
        print("=" * 78)
        print("② 建「逐页索引」：读每个节的页眉，把印刷页码抽出来")
        print("=" * 78)
        if run([sys.executable, "-B", str(TOOLS / "_docx_pages_index.py"),
                "--file", str(sample), "--tag", TAG], env) != 0:
            return 1

        print()
        print("=" * 78)
        print("③ 写成带印刷页码标记的 Markdown")
        print("=" * 78)
        if run([sys.executable, "-B", str(TOOLS / "_docx_pages_to_md.py"),
                TAG, "--corpus", CORPUS], env) != 0:
            return 1

        out = lit / f"{CORPUS} 第{TAG}卷.PAGES.md"
        if not out.exists():
            print(f"\n未生成预期文件：{out}", file=sys.stderr)
            return 1

        text = out.read_text(encoding="utf-8")
        marks = re.findall(r"^#### 第 (\d+) 页\s*$", text, re.M)

        print()
        print("=" * 78)
        print("④ 结果")
        print("=" * 78)
        print(f"输出文件 : {out.name}")
        print(f"页标记数 : {len(marks)}   页码范围 : {min(marks)}–{max(marks)}")
        print()
        print("注意：素材只有 6 页（PDF 页序 1–6），但页标记是 "
              f"{min(marks)}–{max(marks)}——因为是**从页眉读出的印刷页码**，不是页序。")
        print()
        print("-" * 78)
        print("输出片段（前 34 行）：")
        print("-" * 78)
        for line in text.splitlines()[:34]:
            print("  " + line)
        print("-" * 78)
        print()
        print("下一步：这份 .PAGES.md 可以直接喂给任何检索系统；")
        print("       要做「跨卷检索 + 引文定位 + 页码回填」，再接知识库引擎（见 README）。")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # 工具把逐页索引写在 tools/_docx_pages/，demo 用完清掉
        shutil.rmtree(TOOLS / "_docx_pages", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
