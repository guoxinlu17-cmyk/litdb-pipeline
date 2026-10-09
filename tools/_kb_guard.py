# -*- coding: utf-8 -*-
"""知识库写操作的安全护栏：**备份 → 断言 → 事务 → 写后校验**。

为什么需要它
------------
直接改 `kb.sqlite` 的脚本已经犯过一次错：把 `(id, page_start, page_end)` 传给了
`SET page_start=?, page_end=? WHERE id=?`，于是 `WHERE id = 页码` 从未命中——若命中，
就会把 chunk_id 当成页码静默写进库里。**静默写错列比报错危险得多。**

本模块把三道护栏固定下来，所有直接改库的脚本都应经此进出：

1. `assert_pages()` —— 写入前断言取值范围（页码 1–2000、块 id > 100），不合即中止；
2. `apply()` —— 自动备份 → 单事务执行 → 写后核对关键计数（**不增反减即报警**）；
3. `report()` —— 输出变更前后的计数对照，供写进执行记录。

用法
----
    import _kb_guard as G
    g = G.KbGuard(DB, reason="回填页码")
    g.assert_pages(page_updates)                     # [(page_start, page_end, chunk_id), ...]
    g.apply(lambda c: c.executemany("UPDATE chunks SET page_start=?, page_end=? WHERE id=?",
                                    page_updates))
    print(g.report())

命令行自检：
    python -B _kb_guard.py --selftest        # 用内存库自测三道护栏
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from _paths import kb_sqlite  # 同目录共享模块（_toolkit 在 ignore_dirs 内，不进索引）
KEEP_BACKUPS = 8                      # 保留最近 N 个 .bak-* 备份

PAGE_MIN, PAGE_MAX = 1, 2000          # 合理页码范围（全集最大卷 ~1181 页）
CHUNK_ID_MIN = 100                    # 块 id 下限（我们库里的块 id 都远大于此）


class GuardError(RuntimeError):
    pass


def default_db() -> Path:
    """按 _paths.py 的规则解析目标库文件；解析不出就抛 PathError（不猜）。

    故意**不在导入时**解析：导入一个守卫模块，不该因为"这台机器上暂时还没有库"
    （新项目、可移植骨架、库正在建）就抛异常、连 import 都失败。
    """
    return Path(kb_sqlite())


def __getattr__(name):        # PEP 562：`from _kb_guard import DEFAULT_DB` 仍可用
    if name == "DEFAULT_DB":
        return default_db()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def snapshot(db: Path, reason: str, keep: int = KEEP_BACKUPS) -> Path:
    """写前备份，并只保留最近 keep 个（连同更早的手工备份一起排序）。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = db.with_name(f"{db.name}.bak-{ts}-{reason}")
    shutil.copy2(db, dst)
    olds = sorted(db.parent.glob(f"{db.name}.bak-*"), key=lambda p: p.stat().st_mtime)
    for p in olds[:-keep]:
        p.unlink(missing_ok=True)
    return dst


def assert_pages(updates, page_min=PAGE_MIN, page_max=PAGE_MAX, id_min=CHUNK_ID_MIN) -> None:
    """断言 (page_start, page_end, chunk_id) 列表合常理。"""
    if not updates:
        return
    bad = [t for t in updates
           if len(t) != 3 or t[0] is None or t[1] is None
           or not (page_min <= t[0] <= page_max) or not (page_min <= t[1] <= page_max)
           or t[2] is None or t[2] < id_min]
    if bad:
        raise GuardError(
            f"page_updates 有 {len(bad)}/{len(updates)} 条不合常理"
            f"（页码应在 {page_min}–{page_max}、块 id 应 ≥{id_min}）；样例：{bad[:3]}")
    if any(t[0] > t[1] for t in updates):
        raise GuardError("存在 page_start > page_end 的条目")


def assert_doc_updates(items) -> None:
    """断言文档级更新（dict 列表）字段名与取值合常理。"""
    allowed = {"title", "authors", "year", "journal", "doi", "kind"}
    for it in items:
        for k, v in it.items():
            if k not in allowed:
                raise GuardError(f"不认识的字段：{k}")
            if k == "year" and v is not None and not (800 <= int(v) <= 2100):
                raise GuardError(f"年份不合常理：{v}")


class KbGuard:
    """一次写操作的护栏上下文。"""

    def __init__(self, db=None, reason: str = "manual"):
        self.db = Path(db) if db is not None else default_db()
        self.reason = reason
        self.conn = sqlite3.connect(self.db, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.before = self.counts()
        self.backup: Path | None = None
        self.after: dict | None = None

    # ── 计数 ──────────────────────────────────────────────
    def counts(self) -> dict:
        q = lambda s: self.conn.execute(s).fetchone()[0]        # noqa: E731
        return {
            "docs": q("SELECT COUNT(*) FROM docs"),
            "chunks": q("SELECT COUNT(*) FROM chunks"),
            "vecs": q("SELECT COUNT(*) FROM vecs"),
            "with_page": q("SELECT COUNT(*) FROM chunks WHERE page_start IS NOT NULL"),
            "authors_filled": q("SELECT COUNT(*) FROM docs WHERE TRIM(COALESCE(authors,''))<>''"),
        }

    # ── 断言（写入前） ────────────────────────────────────
    @staticmethod
    def assert_pages(updates) -> None:
        assert_pages(updates)

    @staticmethod
    def assert_doc_updates(items) -> None:
        assert_doc_updates(items)

    # ── 执行 ──────────────────────────────────────────────
    def apply(self, fn, expect: dict | None = None) -> dict:
        """备份 → 事务执行 fn(conn) → 写后校验。expect 可指定期望变化（如 {"with_page": "+>0"}）。"""
        self.backup = snapshot(self.db, self.reason)
        with self.conn:                       # 单事务：异常自动回滚
            fn(self.conn)
        self.after = self.counts()
        problems = self.check(expect)
        if problems:
            print("⚠️ 写后校验发现问题：")
            for p in problems:
                print("   -", p)
        return self.after

    def check(self, expect: dict | None = None) -> list[str]:
        if self.after is None:
            return []
        b, a = self.before, self.after
        out = []
        for k in ("docs", "chunks", "vecs", "with_page", "authors_filled"):
            if a[k] < b[k]-0:                                  # 任何一项都不该无故减少（docs 允许删重复件）
                if k != "docs" and a[k] < b[k]:
                    out.append(f"{k} 减少：{b[k]} → {a[k]}")
        if a["vecs"] > a["chunks"]:
            out.append(f"向量数({a['vecs']}) 多于块数({a['chunks']})，异常")
        if expect:
            for k, rule in expect.items():
                if rule == "+" and not a[k] > b[k]:
                    out.append(f"期望 {k} 增加，实际 {b[k]} → {a[k]}")
                if rule == "-" and not a[k] < b[k]:
                    out.append(f"期望 {k} 减少，实际 {b[k]} → {a[k]}")
        return out

    def report(self) -> str:
        b = self.before
        a = self.after or self.counts()
        lines = [f"【{self.reason}】" + (f"备份 {self.backup.name}" if self.backup else "（未写入）")]
        for k in b:
            delta = a[k] - b[k]
            lines.append(f"  {k:<15} {b[k]:>7,} → {a[k]:>7,}" + (f"  ({delta:+,})" if delta else ""))
        return "\n".join(lines)

    def close(self) -> None:
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────
# 自测：用内存库验证三道护栏
# ─────────────────────────────────────────────────────────────────────────
def selftest() -> int:
    ok = True

    def expect_error(name, fn):
        nonlocal ok
        try:
            fn()
        except GuardError:
            print(f"  ✅ {name}：已按预期拦截")
            return
        print(f"  ❌ {name}：本该拦截却放行")
        ok = False

    print("护栏自测：")
    # ① 元组顺序写反（把 chunk_id 当页码）——本次真实踩过的坑
    expect_error("页码位置被写入 chunk_id",
                 lambda: assert_pages([(29364, 294, 294), (29365, 294, 294)]))
    # ② 页码越界
    expect_error("页码越界", lambda: assert_pages([(0, 5, 12345)]))
    # ③ page_start > page_end
    expect_error("起止颠倒", lambda: assert_pages([(300, 200, 12345)]))
    # ④ 正常数据放行
    try:
        assert_pages([(294, 294, 29364), (295, 296, 29365)])
        print("  ✅ 正常数据放行")
    except GuardError as exc:                                  # noqa: BLE001
        print(f"  ❌ 正常数据被拦：{exc}")
        ok = False
    # ⑤ 文档级断言
    expect_error("年份不合理", lambda: assert_doc_updates([{"year": 26}]))
    expect_error("字段名不认识", lambda: assert_doc_updates([{"author": "x"}]))
    # ⑥ 计数校验：向量多于块数应报警
    b = {"docs": 10, "chunks": 100, "vecs": 120, "with_page": 0, "authors_filled": 0}
    g = KbGuard.__new__(KbGuard)
    g.before, g.after = b, dict(b)
    if g.check():
        print("  ✅ 向量多于块数：已报警")
    else:
        print("  ❌ 向量多于块数：未报警")
        ok = False
    print("自测" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
