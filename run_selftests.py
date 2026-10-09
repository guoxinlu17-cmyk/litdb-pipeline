#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑一遍全部**自带自测**的工具，汇总通过/失败。

为什么需要它
------------
本仓库的工具大多要在**已配置的项目**上运行（`tools/_paths.py` 决定去哪找素材、题录库、
论文目录）。没有项目时，部分工具在 **import 阶段**就会解析路径并报 `PathError`——
这是设计使然（宁可报错，不要猜），但对第一次克隆仓库的人不友好。

本脚本因此干两件事：
  1. 在临时目录里搭一个**最小的空项目骨架**，并把它写进 `项目布局.json`；
  2. 用该骨架作环境跑各工具的自测，跑完清理。

这样"自测能不能过"就与"你的项目长什么样"解耦了。

用法：
    python run_selftests.py            # 跑全部
    python run_selftests.py --keep     # 保留临时骨架，便于排查
退出码：0 全部通过；1 有失败。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
TOOLS = REPO / "tools"

# 控制台可能是 GBK（中文 Windows），强制 UTF-8 以免打印自测输出时炸掉
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 带 --selftest 的工具（新增自测后请同步此表）
SELFTEST_TOOLS = [
    "_paths.py",
    "_insert_page_numbers.py",
    "_interview_check.py",
    "_interview_norm.py",
    "_interview_verify.py",
    "_interview_experiment.py",
    "_kb_guard.py",
    "md2docx_unified.py",
]


def make_fixture(root: Path) -> dict[str, str]:
    """搭一个最小项目骨架，返回要设的环境变量。

    关键点：`.kb/kb.sqlite` 必须是**非空**的——`_paths` 明确把 0 文档的空壳库排除在候选之外
    （因为引擎在错误路径上会自动建空库，那种空库一旦进入探测范围就会造成"多个候选→解析失败"）。
    """
    kb = root / ".kb"
    (root / "01_文献").mkdir(parents=True, exist_ok=True)
    (root / "03_论文").mkdir(parents=True, exist_ok=True)
    (root / "05_文献数据库").mkdir(parents=True, exist_ok=True)
    kb.mkdir(parents=True, exist_ok=True)

    db = kb / "kb.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT, title TEXT, sha256 TEXT, size INTEGER
        );
        """
    )
    con.execute("INSERT INTO docs(path, title, sha256, size) VALUES (?,?,?,?)",
                ("fixture.txt", "夹具文档", "0" * 64, 123))
    con.commit()
    con.close()

    # 项目自己声明布局：多个候选目录时靠它消歧
    (root / "项目布局.json").write_text(
        '{\n  "kb": ".kb",\n  "db": "05_文献数据库",\n'
        '  "lit": "01_文献",\n  "papers": "03_论文"\n}\n',
        encoding="utf-8")

    # 只设 LITDB_PROJECT，其余交给 项目布局.json ——
    # 这既演示了推荐的配置方式，也避免 LITDB_PAPERS/LITDB_LIT 等环境变量
    # 抢在 项目布局.json 之前命中，从而干扰 _paths 自测里"未声明就应报错"那两条断言。
    return {"LITDB_PROJECT": str(root)}


def run_one(tool: str, env: dict[str, str]) -> tuple[bool, str]:
    p = TOOLS / tool
    if not p.exists():
        return False, "文件不存在"
    try:
        r = subprocess.run([sys.executable, "-B", str(p), "--selftest"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=str(TOOLS), timeout=300)
    except subprocess.TimeoutExpired:
        return False, "超时（300s）"
    tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if tail:
        msg = tail[-1]
    else:
        err = [ln for ln in (r.stderr or "").splitlines() if ln.strip()]
        msg = err[-1] if err else ""
    return r.returncode == 0, msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时项目骨架")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="litdb-selftest-"))
    try:
        env = dict(os.environ)
        env.update(make_fixture(tmp))
        print(f"临时项目骨架：{tmp}")
        print(f"共 {len(SELFTEST_TOOLS)} 个自带自测的工具\n")
        print(f"{'结果':<7}{'工具':<32}末行输出")
        print("-" * 96)
        ok = 0
        for t in SELFTEST_TOOLS:
            good, msg = run_one(t, env)
            ok += good
            print(f"{'PASS' if good else 'FAIL':<7}{t:<32}{msg[:58]}")
        print("-" * 96)
        print(f"{ok}/{len(SELFTEST_TOOLS)} 通过")
        return 0 if ok == len(SELFTEST_TOOLS) else 1
    finally:
        if args.keep:
            print(f"\n（--keep）临时骨架保留在：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
