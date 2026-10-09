#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_paths.py — 工具库共享路径解析（把项目路径从 90+ 处脚本里挪到一处）

**为什么需要它**：`tools` 下大量脚本把 `<项目根>\\...` 直接写在
模块级常量里（实测 94 处、约 70 个文件）。换机器、换盘符、改项目名，这些脚本就全废——
而且失败方式往往是"库是空的"或"文件找不到"，看不出真正原因。

**它只做一件事**：按固定优先级解析路径；**解析不出来就明确报错，绝不猜**（沿用"留空胜过猜测"）。

解析优先级（高 → 低）
====================
1. 显式参数（调用方把命令行参数传进来）
2. 环境变量：`KB_ROOT`（既有约定，见 kb_cli.py）、`LITDB_PROJECT`、`LITDB_LIT`、`LITDB_DBDIR`
3. 自动探测：在 `BASE` 下按**结构**找（`.kb/kb.sqlite`、`05_文献数据库`、`*/01_文献`），
   **唯一命中才采用**；命中 0 个或多个都报错并列出候选
4. 抛 `PathError`

`BASE` 默认是本目录（`tools`）的上一级，可用环境变量 `LITDB_BASE` 覆盖——
所以整套工具换到别的盘/别的机器也能用。

约定的项目结构
==============
```
<项目根>/
  .kb/kb.sqlite                  ← kb_dir() / kb_sqlite()
  05_文献数据库/                  ← db_dir()
  <任意专题>/01_文献/             ← lit_dir()
```

用法
====
```python
from _paths import kb_sqlite, db_dir, lit_dir, project_root
DB = kb_sqlite()          # 解析不出会抛 PathError，错误信息列出试过的来源
```
```
python _paths.py                     # 打印本机解析结果（诊断用）
python _paths.py --selftest          # 自测
```
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

TOOLKIT_DIR = Path(__file__).resolve().parent
_ENV_BASE = "LITDB_BASE"
BASE: Path = Path(os.environ.get(_ENV_BASE) or TOOLKIT_DIR.parent).resolve()

KB_SQLITE_NAME = "kb.sqlite"
DB_DIR_NAME = "05_文献数据库"
LIT_DIR_NAME = "01_文献"
# 项目自己在根目录声明布局（可选）。有它就不必猜、也不必在脚本里写专名。
# 例：{"kb": ".kb", "db": "05_文献数据库", "lit": "topic-a/01_文献"}
PROJECT_CONFIG_NAME = "项目布局.json"

_CACHE: dict = {}


class PathError(RuntimeError):
    """路径解析失败。消息里必须列出**试过的全部来源**，便于排障。"""


def _fail(what: str, tried: list[str], extra: str = "") -> None:
    raise PathError("%s 解析失败。已尝试：\n  - %s\n%s"
                    "请用命令行参数、环境变量，或把目录放到约定的位置。"
                    % (what, "\n  - ".join(tried), (extra + "\n") if extra else ""))


def base() -> Path:
    return BASE


def toolkit_dir() -> Path:
    """工具库目录（本文件所在目录）——永远可解析。"""
    return TOOLKIT_DIR


def kb_doc_count(sqlite_file: Path) -> int | None:
    """库内文档数；**空壳库/非库文件返回 None**。

    为什么需要它：引擎在路径写错时会 `mkdir` + 建一个 0 文档的空库（"空壳陷阱"）。
    这种空壳一旦落在 BASE 下，结构探测就会把它当成一个"项目"，于是出现多个候选、
    解析失败——**空壳库的危害不止是"看起来像库是空的"，它还会破坏自动探测**。
    所以这里明确：空壳库不是知识库，不参与候选。
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(sqlite_file).replace("\\", "/"), uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        finally:
            con.close()
        return int(n)
    except Exception:
        return None


def _looks_like_project(d: Path) -> bool:
    """项目根的结构特征：有 05_文献数据库 目录、有项目布局.json、或有一个**非空**的知识库。"""
    if (d / DB_DIR_NAME).is_dir() or (d / PROJECT_CONFIG_NAME).is_file():
        return True
    n = kb_doc_count(d / ".kb" / KB_SQLITE_NAME)
    return bool(n)


def read_project_config(proj: Path | None) -> dict:
    """读 `<项目根>/项目布局.json`（可选）。读不到/坏掉都返回 {}，不抛。"""
    if proj is None:
        return {}
    key = ("cfg", str(proj))
    if key in _CACHE:
        return _CACHE[key]
    cfg = {}
    f = Path(proj) / PROJECT_CONFIG_NAME
    if f.is_file():
        try:
            import json
            cfg = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
    _CACHE[key] = cfg
    return cfg


def _from_config(proj: Path | None, key: str) -> tuple[Path | None, str | None]:
    """从项目布局.json 取一个逻辑名对应的目录。返回 (路径, 失败说明)。"""
    cfg = read_project_config(proj)
    v = cfg.get(key)
    if not v:
        return None, None
    p = Path(v)
    p = p if p.is_absolute() else (Path(proj) / p)
    if p.is_dir():
        return p.resolve(), None
    return None, "项目布局.json 的 %s=%s 不是目录" % (key, v)


# ── 项目根 ────────────────────────────────────────────────────────────────────
def _detect_projects() -> list[Path]:
    """结构探测：BASE 下**结构吻合**的一级目录。

    分强弱两级，**结构信号强于声明文件**：
      · 强信号 = 有 `05_文献数据库` 目录，或有一个**非空**的知识库
      · 弱信号 = 只有一个 `项目布局.json`
    弱信号只在没有任何强信号时才作为候选。这样"可移植项目包/模板目录"这类
    只带配置文件、不带数据的目录，不会把真实项目挤成"多候选"而解析失败。
    """
    strong, weak = [], []
    if not BASE.is_dir():
        return []
    for d in sorted(BASE.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue
        try:
            if (d / DB_DIR_NAME).is_dir() or (kb_doc_count(d / ".kb" / KB_SQLITE_NAME) or 0) > 0:
                strong.append(d)
            elif (d / PROJECT_CONFIG_NAME).is_file():
                weak.append(d)
        except OSError:
            continue
    return strong or weak


def project_root(explicit=None, strict: bool = True) -> Path | None:
    key = ("project", str(explicit), strict)
    if key in _CACHE:
        return _CACHE[key]
    tried = []
    cand = None
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("显式参数 %s（不是目录）" % p)
    if cand is None and os.environ.get("LITDB_PROJECT"):
        p = Path(os.environ["LITDB_PROJECT"])
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("环境变量 LITDB_PROJECT=%s（不是目录）" % p)
    if cand is None:
        found = _detect_projects()
        if len(found) == 1:
            cand = found[0]
        elif not found:
            tried.append("结构探测：%s 下没有含 .kb\\%s 或 %s 的一级目录"
                         % (BASE, KB_SQLITE_NAME, DB_DIR_NAME))
        else:
            tried.append("结构探测命中多个，拒绝猜测：%s" % "、".join(str(x) for x in found))
    if cand is None:
        if strict:
            _fail("项目根", tried)
        return None
    _CACHE[key] = cand
    return cand


# ── 知识库（kb_root 陷阱的正确用法：kb_root 必须是**直接含 kb.sqlite 的目录**）──
def kb_dir(explicit=None, strict: bool = True) -> Path | None:
    """返回**直接包含 kb.sqlite 的目录**（通常是 <项目>/.kb）。

    显式参数与环境变量两种写法都接受：给 `<项目>` 或给 `<项目>/.kb` 都能识别。
    """
    key = ("kbdir", str(explicit), strict)
    if key in _CACHE:
        return _CACHE[key]
    tried = []

    def _from(p: Path):
        if (p / KB_SQLITE_NAME).is_file():
            return p
        if (p / ".kb" / KB_SQLITE_NAME).is_file():
            return p / ".kb"
        return None

    cand = None
    if explicit:
        r = _from(Path(explicit))
        if r:
            cand = r.resolve()
        else:
            tried.append("显式参数 %s（里面没有 %s）" % (explicit, KB_SQLITE_NAME))
    if cand is None and os.environ.get("KB_ROOT"):
        v = os.environ["KB_ROOT"]
        r = _from(Path(v))
        if r:
            cand = r.resolve()
        else:
            tried.append("环境变量 KB_ROOT=%s（里面没有 %s）" % (v, KB_SQLITE_NAME))
    if cand is None:
        proj = project_root(strict=False)
        c, why = _from_config(proj, "kb")
        if c:
            cand = c
        elif why:
            tried.append(why)
        if cand is None and proj is not None:
            r = _from(proj)
            if r:
                cand = r.resolve()
            else:
                tried.append("项目根 %s 下没有 .kb\\%s" % (proj, KB_SQLITE_NAME))
        hits = [p.parent for p in sorted(BASE.glob("*/.kb/" + KB_SQLITE_NAME))] \
            if BASE.is_dir() else []
        alive = [h for h in hits if (kb_doc_count(h / KB_SQLITE_NAME) or 0) > 0]
        if len(alive) == 1:
            cand = alive[0].resolve()
        elif len(alive) > 1:
            tried.append("结构探测命中多个**非空**库，拒绝猜测：%s"
                         % "、".join(str(x) for x in alive))
        elif hits:
            tried.append("结构探测只找到空壳库（0 文档或不是 SQLite），"
                         "按规则不采用：%s" % "、".join(str(x) for x in hits))
        else:
            tried.append("结构探测：%s\\*\\.kb\\%s 无命中" % (BASE, KB_SQLITE_NAME))
    if cand is None:
        if strict:
            _fail("知识库目录（kb_root 必须是直接含 kb.sqlite 的目录）", tried)
        return None
    _CACHE[key] = cand
    return cand


def kb_sqlite(explicit=None, strict: bool = True) -> Path | None:
    d = kb_dir(explicit, strict)
    if d is None:
        return None
    p = Path(d) / KB_SQLITE_NAME
    key = ("kbsqlite", str(p))
    if key not in _CACHE:
        _CACHE[key] = p
    return p


# ── 项目内的约定子目录 ────────────────────────────────────────────────────────
def project_subdir(name: str, env_var: str | None = None,
                   explicit=None, strict: bool = True, config_key: str | None = None) -> Path | None:
    key = ("sub", name, str(explicit), strict)
    if key in _CACHE:
        return _CACHE[key]
    tried = []
    cand = None
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("显式参数 %s（不是目录）" % p)
    if cand is None and env_var and os.environ.get(env_var):
        p = Path(os.environ[env_var])
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("环境变量 %s=%s（不是目录）" % (env_var, p))
    proj = project_root(strict=False)
    if cand is None and config_key:
        c, why = _from_config(proj, config_key)
        if c:
            cand = c
        elif why:
            tried.append(why)
    if cand is None:
        if proj is not None and (proj / name).is_dir():
            cand = (proj / name).resolve()
        else:
            tried.append("%s\\%s 不存在" % (proj if proj else "<项目根未解析>", name))
    if cand is None:
        if strict:
            _fail(name, tried)
        return None
    _CACHE[key] = cand
    return cand


def db_dir(explicit=None, strict: bool = True) -> Path | None:
    """`05_文献数据库`（引文/页码成果目录）。"""
    return project_subdir(DB_DIR_NAME, "LITDB_DBDIR", explicit, strict, config_key="db")


def lit_dir(explicit=None, strict: bool = True) -> Path | None:
    """`*/01_文献`（原始素材目录）。结构探测：项目下唯一名为 `01_文献` 的目录。"""
    key = ("lit", str(explicit), strict)
    if key in _CACHE:
        return _CACHE[key]
    tried = []
    cand = None
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("显式参数 %s（不是目录）" % p)
    if cand is None and os.environ.get("LITDB_LIT"):
        p = Path(os.environ["LITDB_LIT"])
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("环境变量 LITDB_LIT=%s（不是目录）" % p)
    if cand is None:
        proj = project_root(strict=False)
        c, why = _from_config(proj, "lit")
        if c:
            cand = c
        elif why:
            tried.append(why)
        if cand is None and proj is not None:
            hits = sorted(p for p in proj.glob("*/" + LIT_DIR_NAME) if p.is_dir())
            if len(hits) == 1:
                cand = hits[0].resolve()
            elif not hits:
                tried.append("%s\\*\\%s 无命中" % (proj, LIT_DIR_NAME))
            else:
                tried.append("候选多个，拒绝猜测：%s（可在 %s 里用 \"lit\" 指定）"
                             % ("、".join(str(x) for x in hits), PROJECT_CONFIG_NAME))
        elif proj is None:
            tried.append("项目根未解析")
    if cand is None:
        if strict:
            _fail("素材目录（01_文献）", tried)
        return None
    _CACHE[key] = cand
    return cand


def papers_dir(explicit=None, strict: bool = True) -> Path | None:
    """论文目录（如 `<项目>/topic-b/03_论文`）。

    **没有结构约定**（每个项目的专题名都不同），所以只认三处：显式参数、环境变量
    `LITDB_PAPERS`、项目布局.json 的 `papers`。都没有就报错——不猜。
    """
    key = ("papers", str(explicit), strict)
    if key in _CACHE:
        return _CACHE[key]
    tried = []
    cand = None
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("显式参数 %s（不是目录）" % p)
    if cand is None and os.environ.get("LITDB_PAPERS"):
        p = Path(os.environ["LITDB_PAPERS"])
        if p.is_dir():
            cand = p.resolve()
        else:
            tried.append("环境变量 LITDB_PAPERS=%s（不是目录）" % p)
    if cand is None:
        c, why = _from_config(project_root(strict=False), "papers")
        if c:
            cand = c
        elif why:
            tried.append(why)
    if cand is None:
        tried.append("项目布局.json 里没有声明 papers（该目录无结构约定，只能显式指定）")
        if strict:
            _fail("论文目录（papers）", tried)
        return None
    _CACHE[key] = cand
    return cand


def workspace_path(*parts: str, strict: bool = True) -> Path | None:
    """`<BASE>/<工作区>/...`，例如 workspace_path("skill", "插件缺陷反馈")。

    `strict=False` 时，若该路径不是目录则返回 None（与其余解析函数一致；
    原实现写成 `p if (p.is_dir() or not strict) else None`，非严格模式下**永远**返回 p，
    等于"从不返回 None"，与语义不符）。"""
    p = BASE.joinpath(*parts)
    if p.is_dir():
        return p
    if strict:
        _fail("工作区路径", ["%s 不是目录" % p])
    return None


def workspace_file(*parts: str, strict: bool = True) -> Path | None:
    """`<BASE>/<工作区>/.../<文件>`——像 workspace_path，但末段是**文件**。

    单独需要一个函数是因为 workspace_path 要求 is_dir()：拿它去指一个 .md 文件，
    严格模式下会直接报错（当初 `_verify_md2docx.py` 里的示例论文路径就是这种情况）。"""
    p = BASE.joinpath(*parts)
    if p.is_file():
        return p
    if strict:
        _fail("工作区文件", ["%s 不是文件" % p])
    return None


# ── 诊断 ─────────────────────────────────────────────────────────────────────
def describe() -> dict:
    out = {"LITDB_BASE": str(BASE), "toolkit_dir": str(TOOLKIT_DIR)}
    for label, fn in (("project_root", project_root), ("kb_dir", kb_dir),
                      ("kb_sqlite", kb_sqlite), ("db_dir", db_dir), ("lit_dir", lit_dir)):
        try:
            v = fn(strict=True)
            out[label] = str(v)
        except PathError as e:
            out[label] = "**未解析**：" + str(e).replace("\n", " / ")[:200]
    return out


def selftest() -> int:
    import shutil
    import tempfile
    global BASE
    print("== _paths.py 自测 ==")
    checks: list[tuple[str, bool, str]] = []

    def add(name, cond, info=""):
        checks.append((name, bool(cond), info))

    real_base = BASE
    tmp = Path(tempfile.mkdtemp(prefix="paths_"))
    saved_env = {k: os.environ.get(k) for k in ("KB_ROOT", "LITDB_PROJECT", "LITDB_LIT",
                                               "LITDB_DBDIR", _ENV_BASE)}
    try:
        for k in saved_env:
            os.environ.pop(k, None)

        def mk_kb(p: Path, docs: int = 1):
            """造一个真 SQLite 库（docs=0 即空壳库）。"""
            p.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(p))
            con.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, path TEXT)")
            for i in range(docs):
                con.execute("INSERT INTO docs(path) VALUES(?)", ("x%d" % i,))
            con.commit()
            con.close()

        # 项目甲：完整（真库 3 篇 + 05_文献数据库 + 专题/01_文献）
        a = tmp / "项目甲"
        mk_kb(a / ".kb" / KB_SQLITE_NAME, docs=3)
        (a / DB_DIR_NAME).mkdir(parents=True)
        (a / "专题" / LIT_DIR_NAME).mkdir(parents=True)
        # 空壳项目：只有 0 文档的空壳库（不该被当成候选）
        empty_proj = tmp / "空壳项目"
        mk_kb(empty_proj / ".kb" / KB_SQLITE_NAME, docs=0)
        # 项目乙：先留空目录，稍后用于"多候选"测试
        b = tmp / "项目乙"
        b.mkdir()
        BASE = tmp.resolve()
        _CACHE.clear()

        add("① 唯一命中时自动解析项目根", project_root() == a.resolve(), str(project_root(strict=False)))
        add("①a **空壳库（0 文档）不被当作项目候选**",
            empty_proj not in _detect_projects() and a in _detect_projects(),
            str(_detect_projects()))
        # 项目布局.json 优先于结构探测（多个 01_文献 时靠它消歧）
        (a / "另一个专题" / LIT_DIR_NAME).mkdir(parents=True)
        _CACHE.clear()
        amb = ""
        try:
            lit_dir()
        except PathError as e:
            amb = str(e)
        add("①b 两个 01_文献 时拒绝猜测并提示可在项目布局.json 指定",
            "拒绝猜测" in amb and PROJECT_CONFIG_NAME in amb, amb[:150].replace("\n", " / "))
        import json as _json
        (a / PROJECT_CONFIG_NAME).write_text(
            _json.dumps({"lit": "专题/" + LIT_DIR_NAME}, ensure_ascii=False), encoding="utf-8")
        _CACHE.clear()
        add("①c 项目布局.json 可消歧（优先于结构探测）",
            lit_dir() == (a / "专题" / LIT_DIR_NAME).resolve(), str(lit_dir(strict=False)))
        add("①d 无约定的 papers 目录：未声明时报错",
            papers_dir(strict=False) is None, str(papers_dir(strict=False)))
        (a / "专题" / "03_论文").mkdir(parents=True)
        (a / PROJECT_CONFIG_NAME).write_text(_json.dumps(
            {"lit": "专题/" + LIT_DIR_NAME, "papers": "专题/03_论文"}, ensure_ascii=False),
            encoding="utf-8")
        _CACHE.clear()
        add("①e 项目布局.json 声明后可解析 papers",
            papers_dir() == (a / "专题" / "03_论文").resolve(), str(papers_dir(strict=False)))
        # 只有配置文件的目录（如可移植项目包/模板）不得把真实项目挤成多候选
        tpl = tmp / "模板目录"
        tpl.mkdir()
        (tpl / PROJECT_CONFIG_NAME).write_text("{}", encoding="utf-8")
        _CACHE.clear()
        add("①f 只带配置文件的目录（弱信号）不会造成多候选",
            project_root() == a.resolve(), str(project_root(strict=False)))
        add("② kb_dir 指向直接含 kb.sqlite 的目录", kb_dir() == (a / ".kb").resolve(),
            str(kb_dir(strict=False)))
        add("③ kb_sqlite 拼出完整文件路径",
            kb_sqlite() == (a / ".kb" / KB_SQLITE_NAME).resolve(), str(kb_sqlite(strict=False)))
        add("④ db_dir = 05_文献数据库", db_dir() == (a / DB_DIR_NAME).resolve(),
            str(db_dir(strict=False)))
        add("⑤ lit_dir 结构探测到 01_文献", lit_dir() == (a / "专题" / LIT_DIR_NAME).resolve(),
            str(lit_dir(strict=False)))

        # 只找到空壳库 → 明确报错（把静默陷阱变成响亮错误）
        only_stub = tmp / "只有空壳"
        only_stub.mkdir()
        mk_kb(only_stub / "空壳项目" / ".kb" / KB_SQLITE_NAME, docs=0)
        BASE = only_stub.resolve()
        _CACHE.clear()
        stub_msg = ""
        try:
            kb_sqlite()
        except PathError as e:
            stub_msg = str(e)
        add("⑤a 只有空壳库时报错且说明原因",
            "空壳" in stub_msg, stub_msg[:160].replace("\n", " / "))
        BASE = tmp.resolve()
        _CACHE.clear()

        # 多个**非空**候选 → 拒绝猜测
        mk_kb(b / ".kb" / KB_SQLITE_NAME, docs=2)
        (b / DB_DIR_NAME).mkdir()
        _CACHE.clear()
        raised = False
        try:
            project_root()
        except PathError as e:
            raised = "拒绝猜测" in str(e)
        add("⑥ 多个非空候选时拒绝猜测并报错", raised, "")
        raised_kb = False
        try:
            kb_dir()
        except PathError as e:
            raised_kb = "拒绝猜测" in str(e)
        add("⑥a kb_dir 同在多候选时同样拒绝", raised_kb, "")

        # 显式参数 / 环境变量 优先于探测（这正是多候选时的出路）
        os.environ["LITDB_PROJECT"] = str(b)
        _CACHE.clear()
        add("⑦ 环境变量可指定项目根（多候选时也能用）",
            project_root() == b.resolve(), str(project_root(strict=False)))
        _CACHE.clear()
        add("⑧ 显式参数优先于环境变量", project_root(explicit=str(a)) == a.resolve(),
            str(project_root(explicit=str(a), strict=False)))
        _CACHE.clear()

        # KB_ROOT 两种写法都认（给项目根 / 给 .kb 目录）
        os.environ["KB_ROOT"] = str(a)
        _CACHE.clear()
        r1 = kb_dir()
        os.environ["KB_ROOT"] = str(a / ".kb")
        _CACHE.clear()
        r2 = kb_dir()
        add("⑨ KB_ROOT 给项目根或 .kb 目录都识别",
            r1 == (a / ".kb").resolve() and r2 == (a / ".kb").resolve(),
            "%s / %s" % (r1, r2))
        os.environ.pop("KB_ROOT", None)
        _CACHE.clear()

        # 什么都没有 → 明确报错（strict），或 None（strict=False）
        os.environ.pop("LITDB_PROJECT", None)      # 前面⑦留下的，必须清掉
        _CACHE.clear()
        empty = tmp / "空目录"
        empty.mkdir()
        BASE = empty.resolve()
        _CACHE.clear()
        msg = ""
        try:
            kb_sqlite()
        except PathError as e:
            msg = str(e)
        add("⑩ 解析不出时抛 PathError 且列出试过的来源",
            "已尝试" in msg and "结构探测" in msg, msg[:160].replace("\n", " / "))
        add("⑪ strict=False 时不抛、返回 None", kb_sqlite(strict=False) is None, "")
        add("⑫ describe() 对未解析项不抛异常",
            isinstance(describe().get("kb_sqlite"), str), "")

        # 工作区目录 / 工作区文件（必须在 BASE 指向夹具目录时测）
        BASE = tmp.resolve()
        _CACHE.clear()
        (tmp / "某工作区" / "子目录").mkdir(parents=True, exist_ok=True)
        (tmp / "某工作区" / "子目录" / "文.md").write_text("x", encoding="utf-8")
        _CACHE.clear()
        add("⑮ workspace_path 解析工作区目录",
            workspace_path("某工作区", "子目录") == (tmp / "某工作区" / "子目录").resolve(),
            str(workspace_path("某工作区", "子目录", strict=False)))
        add("⑯ workspace_file 解析工作区文件",
            workspace_file("某工作区", "子目录", "文.md")
            == (tmp / "某工作区" / "子目录" / "文.md").resolve(), "")
        add("⑰ workspace_file 对目录要报错（与 workspace_path 分工明确）",
            workspace_file("某工作区", "子目录", strict=False) is None, "")
        add("⑱ workspace_path 对文件返回 None",
            workspace_path("某工作区", "子目录", "文.md", strict=False) is None, "")

        # 回到真实环境：验证本机解析结果
        BASE = real_base
        _CACHE.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        real = kb_sqlite(strict=False)
        expect = real_base / "example-project" / ".kb" / KB_SQLITE_NAME
        # 注意：这一项原先是"必须解析到本机那个研究库"，等于把某一台机器的目录名
        # 写进了自测——换到可移植项目骨架（BASE 下还没有库）就必然 FAIL。
        # 改成站点无关的不变量：本机确实有那个库时，必须命中它；
        # 本机没有时，只要求"给出答案且不抛异常"（strict=False 返回 None 是合法结果）。
        if expect.is_file():
            cond = real == expect.resolve()
            info = "解析=%s" % real
        else:
            cond = real is None or Path(real).is_file()
            info = "本机无 example-project，解析=%s（合规）" % real
        add("⑬ 真实环境解析自洽（本机有那个库则必须命中，没有则不抛）", cond, info)
        add("⑭ toolkit_dir 恒可解析", toolkit_dir() == TOOLKIT_DIR, "")
    finally:
        BASE = real_base
        _CACHE.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    print("本机路径解析结果：")
    for k, v in describe().items():
        print("  %-14s %s" % (k, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
