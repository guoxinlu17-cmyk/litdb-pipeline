#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""md2docx（统一版 v2）—— Markdown → Word (.docx)，仅依赖 python-docx，无需 pandoc。

v2 在 v1 基础上按《md转Word工具_设计定稿与实施单》加入：
  · 版式档（profile）：default + 《社会学研究》《政治学研究》《开放时代》
  · 计划卡 + --confirm 落盘闸门（转前确认）
  · 注释：脚注 / 尾注（手写 OOXML，python-docx 无此 API）
  · 字体四槽 + w:hint（python-docx 无 hint 支持）
  · 首行缩进真「2 字符」（w:firstLineChars，替代原先硬编码的 Pt(24)）
  · 东亚版式开关（禁则/标点溢出/中西文间距，python-docx 无访问器）
  · 行距三模式（倍数/固定值/最小值）
  · 图片（相对路径解析、等比缩放、可选灰度）
  · 标点三层之 L1（默认只报告不改写）+ 字体可用性预检

用法（示例）：
    # 1) 单文件（v2 起必须 --confirm 才落盘；不加则只出计划卡）
    python md2docx_unified.py 正文.md -o 正文.docx --confirm
    # 2) 指定期刊版式 + 多文件按序合并 + 封面
    python md2docx_unified.py 引言.md 正文.md -o 论文.docx \\
        --profile open_times --cover --title "论文题目" --author "作者" --confirm
    # 3) 只看计划（不落盘，退出码 0）
    python md2docx_unified.py --batch . --profile Political_Studies --dry-run
    # 4) 列出全部版式档
    python md2docx_unified.py --list-profiles
    # 5) 自测
    python md2docx_unified.py --selftest

⚠ v2 的一处**故意行为变更**：不带 --confirm 时**不写文件**（v1 会直接写）。
   这是设计决策 D4「转前确认」的落地；自动化调用方请改为先 --dry-run --json 再 --confirm。

版本与变更记录
    1.0  (2026-09) 收敛同一目录下多个各自为战的实现；五项验证通过。
    2.0  (2026-09-25) 见上；修改前备份 md2docx_unified.py.bak-20260925-pre2.0
                      变更前 hash 35496db114b88386（与工具库注册表一致）
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

try:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import CONTENT_TYPE as CT
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.shared import Cm, Pt, RGBColor
except ImportError:  # pragma: no cover
    print("缺少依赖：请先执行  pip install python-docx", file=sys.stderr)
    raise SystemExit(3)

try:  # Windows 控制台默认编码可能是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

VERSION = "2.0.0"
DEFAULT_STRIP_MARKER = "## 写作说明"
SUPPORTED_SUFFIXES = (".md", ".markdown")
PROFILE_DIR = Path(__file__).resolve().parent / "md2docx_profiles"

# 号数制 → pt。期刊给的是「五号宋体」这类词，不是 pt，故 profile 用号数书写、编译成 pt。
NUMSIZE = {
    "初号": 42.0, "小初": 36.0, "一号": 26.0, "小一": 24.0, "二号": 22.0,
    "小二": 18.0, "三号": 16.0, "小三": 15.0, "四号": 14.0, "小四": 12.0,
    "五号": 10.5, "小五": 9.0, "六号": 7.5, "小六": 6.5,
}

# ---------------------------------------------------------------- 版式档

_FONT_SLOT_ATTRS = ("ascii", "hAnsi", "eastAsia", "cs")
_THEME_ATTRS = ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme")


def _mp(pt_or_name) -> float:
    """号数名或数字 → pt。"""
    if isinstance(pt_or_name, str):
        if pt_or_name in NUMSIZE:
            return NUMSIZE[pt_or_name]
        return float(re.sub(r"[^\d.]", "", pt_or_name) or 12)
    return float(pt_or_name)


BUILTIN_DEFAULT_PROFILE = {
    "profile_id": "default",
    "display_name": "中文论文默认版式",
    "typography": {
        "status": "known",
        "page": {"width_cm": 21.0, "height_cm": 29.7, "margin_top_cm": 2.54,
                 "margin_bottom_cm": 2.54, "margin_left_cm": 3.17, "margin_right_cm": 3.17},
        "fonts": {
            "body": {"ascii": "Times New Roman", "hAnsi": "Times New Roman", "eastAsia": "宋体", "cs": "Times New Roman", "hint": "eastAsia"},
            "heading": {"ascii": "Times New Roman", "hAnsi": "Times New Roman", "eastAsia": "黑体", "cs": "Times New Roman", "hint": "eastAsia"},
            "mono": {"ascii": "Consolas", "hAnsi": "Consolas", "eastAsia": "宋体", "cs": "Consolas", "hint": "eastAsia"},
            "note": {"ascii": "Times New Roman", "hAnsi": "Times New Roman", "eastAsia": "宋体", "cs": "Times New Roman", "hint": "eastAsia"},
        },
        "sizes_pt": {"body": 12.0, "h1": 16.0, "h2": 14.0, "h3": 12.0, "code": 9.0, "note": 9.0},
        "heading_bold": True,
        "line_spacing": {"mode": "multiple", "value": 1.5},
        "first_line_indent_chars": 2.0,
        "east_asian": {"kinsoku": True, "word_wrap": True, "overflow_punct": True,
                       "top_line_punct": False, "auto_space_de": True, "auto_space_dn": True},
        "doc_grid": {"mode": "keep"},
    },
    "notes": {"form": "footnote", "number_format": "decimal", "restart": "continuous", "pending_review": False},
    "figures": {"caption_position": "below", "numbering": "continuous", "color": "original", "max_width_cm": None},
    "elements": {"abstract_max_chars": None, "keywords_min": None, "keywords_max": None, "anonymize_body": None},
    "punctuation": {"mode": "report", "exempt_quotes": True},
    "fonts_preflight": {
        "on_missing": "warn",
        "aliases": {
            "宋体": "SimSun",
            "黑体": "SimHei",
            "楷体": "KaiTi",
            "仿宋": "FangSong",
            "微软雅黑": "Microsoft YaHei",
            "等线": "DengXian",
            "方正书宋": "FZShuSong",
            "方正小标宋简体": "FZXiaoBiaoSong-B05S",
            "仿宋_GB2312": "FangSong_GB2312",
            "楷体_GB2312": "KaiTi_GB2312"
        }
    },
    "evidence": [],
}


def list_profiles() -> list[str]:
    if not PROFILE_DIR.is_dir():
        return ["default"]
    return sorted(p.stem for p in PROFILE_DIR.glob("*.json"))


def load_profile(name_or_path: str | None) -> dict:
    """载入版式档。找不到文件时回落内置 default（脚本可独立运行）。"""
    import copy

    base = copy.deepcopy(BUILTIN_DEFAULT_PROFILE)
    if not name_or_path or name_or_path == "default":
        disk = PROFILE_DIR / "default.json"
        if disk.is_file():
            base = _merge(base, json.loads(disk.read_text(encoding="utf-8")))
        return base

    path = Path(name_or_path)
    if not path.is_file():
        cand = PROFILE_DIR / (name_or_path + ".json")
        if not cand.is_file():
            cand = PROFILE_DIR / (name_or_path.lower() + ".json")
        if not cand.is_file():
            raise FileNotFoundError(
                f"找不到版式档「{name_or_path}」。可用：" + "、".join(list_profiles()))
        path = cand
    prof = _merge(base, json.loads(path.read_text(encoding="utf-8")))

    # 期刊「排版层未知」时沿用 default 的有效排版，但保留 status=unknown 供计划卡提示
    typ = prof.get("typography", {})
    if typ.get("status") == "unknown" or typ.get("inherit_from"):
        prof["_typography_status"] = "unknown"
        prof["_typography_reason"] = typ.get("reason", "")
    else:
        prof["_typography_status"] = "known"
        prof["_typography_reason"] = ""
    return prof


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def effective_typography(prof: dict) -> dict:
    """排版层：期刊未规定时用 default 的排版参数渲染。"""
    if prof.get("_typography_status") == "unknown":
        import copy
        return copy.deepcopy(BUILTIN_DEFAULT_PROFILE["typography"])
    return prof.get("typography", BUILTIN_DEFAULT_PROFILE["typography"])


# ---------------------------------------------------------------- 字体


def _ensure_rfonts(rpr):
    """取得/新建 w:rFonts（须紧随 w:rStyle，其余元素之前）。"""
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is not None:
        return rfonts
    rfonts = OxmlElement("w:rFonts")
    rstyle = rpr.find(qn("w:rStyle"))
    if rstyle is not None:
        rstyle.addnext(rfonts)
    else:
        rpr.insert(0, rfonts)
    return rfonts


def set_font_slots(rpr, spec: dict) -> None:
    """写 w:rFonts 四槽 + w:hint。

    为什么必须手写：python-docx 的 font.name 只覆盖 ascii/hAnsi，管不到 eastAsia；
    w:hint 更是完全无支持（全库检索 0 命中）。hint 决定全角标点等「归属模糊」字符
    落到哪个槽——不设时可能被交给 ascii 槽的 Times New Roman，症状是标点变窄、偏高。
    """
    rfonts = _ensure_rfonts(rpr)
    for attr in _THEME_ATTRS:                      # 显式字体与主题字体互斥，先清主题属性
        if rfonts.get(qn("w:" + attr)) is not None:
            del rfonts.attrib[qn("w:" + attr)]
    for slot in _FONT_SLOT_ATTRS:
        if spec.get(slot):
            rfonts.set(qn("w:" + slot), spec[slot])
    if spec.get("hint"):
        rfonts.set(qn("w:hint"), spec["hint"])


def style_fonts(style, spec: dict) -> None:
    set_font_slots(style._element.get_or_add_rPr(), spec)


def run_fonts(run, spec: dict) -> None:
    set_font_slots(run._element.get_or_add_rPr(), spec)


def installed_fonts() -> set[str]:
    """枚举本机字体族名（Windows 注册表）。失败返回空集，预检自动降级为「无法判定」。"""
    names: set[str] = set()
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        i = 0
        while True:
            try:
                name, _val, _t = winreg.EnumValue(key, i)
                i += 1
            except OSError:
                break
            base = re.sub(r"\s*\((TrueType|OpenType|All res|VGA res)\)\s*$", "", name)
            for part in base.split("&"):
                part = part.strip()
                if part:
                    names.add(part)
    except Exception:
        pass
    if not names:
        for d in (Path(r"C:\Windows\Fonts"),):
            if d.is_dir():
                names |= {p.stem for p in d.glob("*.tt*")} | {p.stem for p in d.glob("*.otf")}
    return names


def check_fonts(typ: dict, aliases: dict) -> tuple[list[str], list[str]]:
    """返回 (缺失, 已确认)。字体名可能是中文名而字体族是英文名，故走 aliases 映射。"""
    inst = installed_fonts()
    if not inst:
        return [], []
    want: set[str] = set()
    for slot in ("body", "heading", "mono", "note"):
        spec = typ.get("fonts", {}).get(slot, {})
        for k in ("ascii", "eastAsia"):
            if spec.get(k):
                want.add(spec[k])
    missing, ok = [], []
    for name in sorted(want):
        if name in inst or (name in aliases and aliases[name] in inst):
            ok.append(name)
        else:
            missing.append(name)
    return missing, ok


# ---------------------------------------------------------------- 东亚版式 / 缩进 / 行距

_PPR_AFTER_EA = (
    "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap",
    "w:jc", "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
    "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
)
_EA_SWITCHES = (
    ("kinsoku", "w:kinsoku"), ("word_wrap", "w:wordWrap"), ("overflow_punct", "w:overflowPunct"),
    ("top_line_punct", "w:topLinePunct"), ("auto_space_de", "w:autoSpaceDE"),
    ("auto_space_dn", "w:autoSpaceDN"),
)


def apply_east_asian_typography(pPr, ea: dict) -> None:
    """写东亚版式开关。python-docx 对这些元素只有 tag 顺序占位、无访问器（parfmt.py:77-85），
    故须手工插入，且位置必须在 w:spacing 之前（CT_PPr 元素顺序）。"""
    for key, tag in _EA_SWITCHES:
        if key not in ea:
            continue
        el = pPr.find(qn(tag))
        if el is None:
            el = OxmlElement(tag)
            pPr.insert_element_before(el, *_PPR_AFTER_EA)
        el.set(qn("w:val"), "1" if ea[key] else "0")


def set_first_line_chars(p, chars: float) -> None:
    """真「首行缩进 N 字符」= w:ind/@w:firstLineChars（1/100 字符，随字号自适应）。

    为什么不用 Pt：v1 把 2 字符硬编码为 Pt(24)，仅当正文恰为 12pt 时才等于 2 字符；
    一旦换成五号（10.5pt）就变成 2.29 字符。python-docx 无 firstLineChars 支持（0 命中）。
    """
    ind = p._p.get_or_add_pPr().get_or_add_ind()
    if ind.get(qn("w:firstLine")) is not None:
        del ind.attrib[qn("w:firstLine")]
    ind.set(qn("w:firstLineChars"), str(int(round(chars * 100))))


def apply_line_spacing(pPr, spec: dict) -> None:
    """倍数 / 固定值 / 最小值 三模式（v1 只支持倍数）。"""
    spacing = pPr.get_or_add_spacing()
    mode = (spec or {}).get("mode", "multiple")
    value = (spec or {}).get("value", 1.5)
    if mode == "multiple":
        spacing.set(qn("w:line"), str(int(round(float(value) * 240))))
        spacing.set(qn("w:lineRule"), "auto")
    else:  # exact | atLeast，值为 pt
        spacing.set(qn("w:line"), str(int(round(_mp(value) * 20))))
        spacing.set(qn("w:lineRule"), "exact" if mode == "exact" else "atLeast")


def apply_doc_grid(section, spec: dict) -> None:
    """文档网格。python-docx 对 w:docGrid 只有 tag 占位（section.py:130）。"""
    mode = (spec or {}).get("mode", "keep")
    if mode == "keep":
        return
    sectPr = section._sectPr
    el = sectPr.find(qn("w:docGrid"))
    if el is None:
        el = OxmlElement("w:docGrid")
        sectPr.insert_element_before(el, "w:printerSettings", "w:sectPrChange")
    if mode == "none":
        el.set(qn("w:type"), "default")
    else:
        el.set(qn("w:type"), mode)          # lines | linesAndChars | snapToChars
    el.set(qn("w:linePitch"), str(int((spec or {}).get("line_pitch", 312))))
    if (spec or {}).get("char_space") is not None:
        el.set(qn("w:charSpace"), str(int(spec["char_space"])))


# ---------------------------------------------------------------- 注释（脚注/尾注）

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class NoteManager:
    """脚注 / 尾注。python-docx 无此 API：须自建 part、写关系、并在正文插入引用 run。

    易踩的两点（已在本机 default.docx 上核实）：
      · default.docx 既无 footnotes.xml / endnotes.xml，也无 FootnoteText / FootnoteReference 样式，
        故不能依赖 w:pStyle / w:rStyle，只能直接格式化（否则 Word 里注释文字无格式或报错）。
      · 两个特殊注释（separator / continuationSeparator，id=-1 / 0）不能省。
    """

    def __init__(self, doc, form: str, profile: dict):
        self.doc = doc
        self.form = form                      # footnote | endnote
        self.profile = profile
        self.items: list[str] = []

    # -- 名称映射
    @property
    def _is_foot(self) -> bool:
        return self.form == "footnote"

    @property
    def _item_tag(self) -> str:
        return "w:footnote" if self._is_foot else "w:endnote"

    @property
    def _ref_tag(self) -> str:
        return "w:footnoteReference" if self._is_foot else "w:endnoteReference"

    @property
    def _inner_ref(self) -> str:
        return "w:footnoteRef" if self._is_foot else "w:endnoteRef"

    # -- 正文引用
    def add_reference(self, paragraph, text: str, note_font: dict, size_pt: float):
        self.items.append(text)
        nid = len(self.items)
        run = paragraph.add_run()
        run_fonts(run, note_font)
        run.font.size = Pt(size_pt)
        run.font.superscript = True
        ref = OxmlElement(self._ref_tag)
        ref.set(qn("w:id"), str(nid))
        run._element.append(ref)
        return run

    # -- part 构造
    def _rpr_xml(self, font: dict, size_pt: float) -> str:
        parts = ['<w:rFonts']
        for slot in _FONT_SLOT_ATTRS:
            if font.get(slot):
                parts.append(f' w:{slot}="{xml_escape(font[slot], {chr(34): "&quot;"})}"')
        if font.get("hint"):
            parts.append(f' w:hint="{font["hint"]}"')
        parts.append("/>")
        half = int(round(size_pt * 2))          # w:sz 单位为半磅
        parts.append(f'<w:sz w:val="{half}"/><w:szCs w:val="{half}"/>')
        return "".join(parts)

    def _note_xml(self, nid: int, text: str, font: dict, size_pt: float) -> str:
        rpr = self._rpr_xml(font, size_pt)
        body = xml_escape(text)
        # 注意：_item_tag 本身已含 w: 前缀，不能再拼一次（否则产出 <w:w:footnote>，XML 非法）
        return (
            f'<{self._item_tag} w:id="{nid}">'
            '<w:p><w:pPr>'
            '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
            '<w:ind w:firstLineChars="0" w:firstLine="0"/><w:jc w:val="both"/>'
            '</w:pPr>'
            f'<w:r><w:rPr><w:vertAlign w:val="superscript"/>{rpr}</w:rPr><{self._inner_ref}/></w:r>'
            f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve"> {body}</w:t></w:r>'
            '</w:p>'
            f'</{self._item_tag}>'
        )

    def finalize(self, typ: dict) -> bool:
        """生成 part 与 sectPr 配置；无注释时不做任何事。返回是否已写入。"""
        if not self.items:
            return False
        font = typ["fonts"]["note"]
        size_pt = typ["sizes_pt"].get("note", 9.0)
        specials = (
            f'<{self._item_tag} w:type="separator" w:id="-1"><w:p><w:pPr>'
            '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            f'<w:r><w:separator/></w:r></w:p></{self._item_tag}>'
            f'<{self._item_tag} w:type="continuationSeparator" w:id="0"><w:p><w:pPr>'
            '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            f'<w:r><w:continuationSeparator/></w:r></w:p></{self._item_tag}>'
        )
        body = "".join(self._note_xml(i + 1, t, font, size_pt) for i, t in enumerate(self.items))
        root = "w:footnotes" if self._is_foot else "w:endnotes"
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<{root} xmlns:w="{_W_NS}" xmlns:r="{_R_NS}">'
            + specials + body +
            f'</{root}>'
        ).encode("utf-8")

        name = "footnotes.xml" if self._is_foot else "endnotes.xml"
        partname = PackURI("/word/" + name)
        part = Part(partname, CT.WML_FOOTNOTES if self._is_foot else CT.WML_ENDNOTES,
                    xml, self.doc.part.package)
        self.doc.part.relate_to(part, RT.FOOTNOTES if self._is_foot else RT.ENDNOTES)
        self._apply_sect_pr()
        return True

    def _apply_sect_pr(self) -> None:
        notes = self.profile.get("notes", {})
        sectPr = self.doc.sections[0]._sectPr
        tag = "w:footnotePr" if self._is_foot else "w:endnotePr"
        el = sectPr.find(qn(tag))
        if el is None:
            el = OxmlElement(tag)
            successors = (("w:endnotePr",) if self._is_foot else ()) + (
                "w:type", "w:pgSz", "w:pgMar", "w:paperSrc", "w:pgBorders", "w:lnNumType",
                "w:pgNumType", "w:cols", "w:formProt", "w:vAlign", "w:noEndnote", "w:titlePg",
                "w:textDirection", "w:bidi", "w:rtlGutter", "w:docGrid", "w:printerSettings",
                "w:sectPrChange",
            )
            sectPr.insert_element_before(el, *successors)
        # CT_FtnProps / CT_EdnProps 顺序：pos, numFmt, numStart, numRestart
        for child in ("w:pos", "w:numFmt", "w:numStart", "w:numRestart"):
            old = el.find(qn(child))
            if old is not None:
                el.remove(old)
        pos = OxmlElement("w:pos")
        pos.set(qn("w:val"), "pageBottom" if self._is_foot else "docEnd")
        el.append(pos)
        fmt = OxmlElement("w:numFmt")
        fmt.set(qn("w:val"), notes.get("number_format") or "decimal")
        el.append(fmt)
        start = OxmlElement("w:numStart")
        start.set(qn("w:val"), "1")
        el.append(start)
        restart = OxmlElement("w:numRestart")
        restart.set(qn("w:val"), notes.get("restart") or "continuous")
        el.append(restart)


# ---------------------------------------------------------------- 标点（L1）

_CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s，。；）)】>\]]+")
_CODE_RE = re.compile(r"`[^`]*`")
_DQ_RE = re.compile(r"“[^”]*”")
_SQ_RE = re.compile(r"‘[^’]*’")
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

_HALF2FULL = {",": "，", ".": "。", ";": "；", ":": "：", "?": "？", "!": "！"}
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_RANGE_RE = re.compile(r"(?P<a>\d+)\s*[-~～]\s*(?P<b>\d+)")
_ELLIPSIS_RE = re.compile(r"\.\.\.|。{3,}")
# 破折号：必须排除「数字—数字」——那是年份/数值范围用的一字线（如 1912—1930、3—5 个），
# 不是破折号误用。实测在真实稿件上，不加这个排除会把 5 处年份范围全部改坏。
_DASH_RE = re.compile(r"(?<![\d—])—(?![\d—])|(?<![\d-])--(?![\d-])")
# 日期形如 YYYY-MM-DD：范围规则不得把 2026-08-31 改成 2026～08
_DATE_RE = re.compile(r"\d{4}\s*-\s*\d{1,2}\s*-\s*\d{1,2}")
_RANGE_CTX_STOP = ("页", "版", "卷", "期", "章", "节")


def review_ranges(text: str) -> tuple[list[re.Match], int]:
    """判定每个数值范围：可转换 vs 应跳过。返回 (可转换匹配, 跳过数)。

    跳过三类（**启发式**，不是硬规则，故必须回报跳过数供人工核对）：
      · 年份起止（两侧均为 19xx/20xx 四位年）；
      · 日期（YYYY-MM-DD 形状）；
      · 引用/版式语境（邻近 12 字内出现 页/版/卷/期/章/节）。
    """
    ok: list[re.Match] = []
    skipped = 0
    for m in _RANGE_RE.finditer(text):
        a, b = m.group("a"), m.group("b")
        ctx = text[max(0, m.start() - 12): m.end() + 12]
        near = text[max(0, m.start() - 2): m.end() + 8]
        if _YEAR_RE.fullmatch(a) and _YEAR_RE.fullmatch(b):
            skipped += 1
            continue
        if _DATE_RE.search(near):
            skipped += 1
            continue
        if any(ch in ctx for ch in _RANGE_CTX_STOP):
            skipped += 1
            continue
        ok.append(m)
    return ok, skipped


def _convert_ranges(text: str, apply: bool = True) -> tuple[str, int, int]:
    """数值范围 → 浪纹式连接号「～」（GB/T 15835 / 开放时代体例五（一）3）。

    `apply=False` 时只统计候选、不改写。默认不启用改写：页码/年份/日期与真正的数值范围
    在文本上无法可靠区分（实测 74-96、2026-08-31、1912—1930 都在真实稿件里出现），
    故把改写权交给 --fix-ranges 显式开关。返回 (文本, 改写数, 跳过数)。
    """
    ok, skipped = review_ranges(text)
    if not apply or not ok:
        return text, 0, skipped
    out = text
    for m in reversed(ok):                       # 从后往前替换，避免位移
        out = out[:m.start()] + f"{m.group('a')}～{m.group('b')}" + out[m.end():]
    return out, len(ok), skipped


def _protect_spans(text: str, exempt_quotes: bool) -> list[tuple[bool, str]]:
    """切成 (是否受保护, 片段)。保护区域优先级：代码 > URL > 引号内容 > 正文。"""
    spans: list[tuple[bool, str]] = []
    idx = 0
    pats = [(m.start(), m.end(), True) for m in _CODE_RE.finditer(text)]
    pats += [(m.start(), m.end(), True) for m in _URL_RE.finditer(text)]
    if exempt_quotes:
        for rx in (_DQ_RE, _SQ_RE):
            for m in rx.finditer(text):
                # 只保护引号内部（定界符本身仍可按规则转换）
                pats.append((m.start() + 1, m.end() - 1, True))
    pats.sort()
    for start, end, prot in pats:
        if start < idx:
            continue
        if start > idx:
            spans.append((False, text[idx:start]))
        spans.append((prot, text[start:end]))
        idx = end
    if idx < len(text):
        spans.append((False, text[idx:]))
    return spans


def analyze_punctuation(text: str, exempt_quotes: bool = True) -> dict:
    """统计拟改写处（不改写）。返回 {rule: count} + 汇总。"""
    counts: dict[str, int] = {}
    exempted = 0
    for protected, seg in _protect_spans(text, exempt_quotes):
        if protected:
            exempted += len(seg)
            continue
        for rule, n in _rule_counts(seg).items():
            if n:
                counts[rule] = counts.get(rule, 0) + n
    return {"rules": counts, "total": sum(counts.values()), "exempted_chars": exempted}


def _rule_counts(seg: str) -> dict:
    """统计（不改写）。先把省略号/破折号掩码掉，否则同一批点号会被「半角句读」重复计数。"""
    out = {}
    masked = _ELLIPSIS_RE.sub(lambda m: "\x00" * len(m.group()), seg)
    masked = _DASH_RE.sub(lambda m: "\x00" * len(m.group()), masked)
    half = 0
    for i, ch in enumerate(masked):
        if ch in _HALF2FULL:
            prev = masked[i - 1] if i else ""
            nxt = masked[i + 1] if i + 1 < len(masked) else ""
            if re.match(f"[{_CJK}]", prev) and not nxt.isdigit():
                half += 1
    out["半角句读→全角"] = half
    out["全角数字→半角"] = len(re.findall(r"[０-９]", seg))
    out["省略号→……"] = len(_ELLIPSIS_RE.findall(seg))
    out["破折号→——"] = len(_DASH_RE.findall(seg))
    ok_ranges, skip_ranges = review_ranges(masked)
    out["数值范围→～（候选）"] = len(ok_ranges)
    out["数值范围·跳过（年份/日期/引用）"] = skip_ranges
    out["不可见字符"] = len(re.findall(r"[\u00a0\u200b\ufeff]", seg))
    out["全角空格"] = seg.count("\u3000")
    return {k: v for k, v in out.items() if v}


def normalize_punctuation(text: str, exempt_quotes: bool = True,
                         fix_ranges: bool = False) -> tuple[str, dict]:
    """按 L1 规则改写（仅在不含受保护片段的部分）。返回 (新文本, 改动统计)。

    **规则顺序有意义**：省略号与破折号必须先于「半角句读→全角」，否则 `...` 的首个点会被
    当成句号吃掉，结果是 `。..`（实测踩过）。数值范围也在句读之前，避免 `-` 附近误判。

    `fix_ranges=False`（默认）时**不改写数值范围**，只做其余安全规则——页码/年份/日期与真正
    的数值范围在文本上不可靠区分，实测真实稿件里的 74-96、2026-08-31 都会被误改。

    纪律：调用方默认不启用此函数；须显式 --normalize-punct fix。理由见设计 §4.4.4
    ——自动改写标点可能改动被引原文，属学术诚信问题而非排版问题。
    """
    changes: dict[str, int] = {}
    pieces: list[str] = []
    for protected, seg in _protect_spans(text, exempt_quotes):
        if protected:
            pieces.append(seg)
            continue
        new = seg
        new, n3 = _ELLIPSIS_RE.subn("……", new)
        new, n4 = _DASH_RE.subn("——", new)
        new, n2 = re.subn(r"[０-９]", lambda m: chr(ord(m.group()) - 0xFEE0), new)
        new, n5, _skip5 = _convert_ranges(new, apply=fix_ranges)
        new, n6 = re.subn(r"[\u00a0]", " ", new)
        new, n7 = re.subn(r"[\u200b\ufeff]", "", new)

        def _half(m):
            punct, sp = m.group(1), m.group(2)
            i = m.start()
            prev = new[i - 1] if i else ""
            after = new[m.end()] if m.end() < len(new) else ""
            if re.match(f"[{_CJK}]", prev) and not (sp == "" and after.isdigit()):
                return _HALF2FULL[punct]              # 顺带吃掉随尾空格（中文标点后不留空格）
            return m.group(0)

        new, n1 = re.subn(r"([,.:;?!])( ?)", _half, new)
        for k, v in (("半角句读→全角", n1), ("全角数字→半角", n2), ("省略号→……", n3),
                     ("破折号→——", n4), ("数值范围→～", n5), ("不可见字符", n6 + n7)):
            if v:
                changes[k] = changes.get(k, 0) + v
        pieces.append(new)
    return "".join(pieces), changes


# ---------------------------------------------------------------- 解析


CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
NOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
NOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")
INLINE_NOTE_RE = re.compile(r"\^\[([^\]]+)\]")
STANDALONE_IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")


def split_table_row(line: str) -> list[str]:
    """按**未转义**的竖线切分表格行，并把 `\\|` 还原成 `|`。

    为什么需要它：原来直接 `split("|")`，于是单元格里写成 `\\|` 的竖线（Markdown 的标准
    转义写法）会被当成分隔符 —— 结果是**列数错位**，而且跨过那个竖线的行内代码被拆成两半
    （实测一份 2 万字文档里出现 10 处：单元格残留了未闭合的反引号）。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.replace("\\|", "|").strip() for c in CELL_SPLIT_RE.split(s)]


def is_table_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def split_writing_note(text: str, marker: str | None) -> tuple[str, int, int, int]:
    """从 marker 所在行起截断。返回 (新文本, 截断行号, 丢弃行数, 丢弃字符数)。"""
    if not marker:
        return text, 0, 0, 0
    lines = text.splitlines()
    cut = next((i for i, ln in enumerate(lines) if ln.strip().startswith(marker)), None)
    if cut is None:
        return text, 0, 0, 0
    dropped_lines = lines[cut:]
    return "\n".join(lines[:cut]), cut + 1, len(dropped_lines), sum(len(x) + 1 for x in dropped_lines)


def parse_markdown(text: str, strip_marker: str | None = None) -> list[dict]:
    """把 Markdown 解析成块序列。返回块 dict 列表（img 块的 path 未解析，留给 prepare）。"""
    lines = text.splitlines()
    if strip_marker:
        cut = next((i for i, ln in enumerate(lines) if ln.strip().startswith(strip_marker)), None)
        if cut is not None:
            lines = lines[:cut]

    blocks: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s or s == "---" or s == "***":
            i += 1
            continue
        # 独立成行的图片
        m = STANDALONE_IMG_RE.match(s)
        if m:
            blocks.append({"kind": "image", "alt": m.group(1).strip(), "path": m.group(2).strip()})
            i += 1
            continue
        # 代码块（``` 围栏）：整块原样保留，内部不做 Markdown 解释
        quote_prefix = ""
        probe = s
        if s.startswith(">"):
            quote_prefix = ">"
            probe = s.lstrip(">").strip()
        if probe.startswith("```"):
            lang = probe[3:].strip()
            i += 1
            code_lines: list[str] = []
            while i < n:
                raw = lines[i]
                cur = raw.strip()
                if quote_prefix:
                    if not cur.startswith(">"):
                        break          # 引用块结束而围栏未闭合
                    cur = cur.lstrip(">").strip()
                    if cur.startswith("```"):
                        break
                    code_lines.append(cur)
                else:
                    if cur.startswith("```"):
                        break
                    code_lines.append(raw.rstrip())
                i += 1
            i += 1  # 跳过收尾围栏
            blocks.append({"kind": "code", "lang": lang, "lines": code_lines})
            continue
        # 表格
        if s.startswith("|") and i + 1 < n and lines[i + 1].strip().startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = split_table_row(lines[i])
                if not is_table_separator(cells):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append({"kind": "table", "rows": rows})
            continue
        if s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            blocks.append({"kind": "heading", "level": min(level, 3), "text": s.lstrip("#").strip()})
        elif s.startswith("- ") or s.startswith("* ") or s.startswith("+ "):
            blocks.append({"kind": "bullet", "text": s[2:].strip()})
        elif re.match(r"^\d+[.)]\s", s):
            blocks.append({"kind": "number", "text": re.sub(r"^\d+[.)]\s", "", s)})
        elif s.startswith(">"):
            blocks.append({"kind": "quote", "text": s.lstrip(">").strip()})
        else:
            blocks.append({"kind": "body", "text": s})
        i += 1
    return blocks


def extract_note_defs(blocks: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """抽出脚注定义行（[^x]: 文本），从块流中移除。"""
    defs: dict[str, str] = {}
    kept: list[dict] = []
    for b in blocks:
        if b["kind"] in ("body", "quote") and NOTE_DEF_RE.match(b["text"]):
            m = NOTE_DEF_RE.match(b["text"])
            defs[m.group(1)] = m.group(2).strip()
        else:
            kept.append(b)
    return kept, defs


# ---------------------------------------------------------------- 渲染


class Ctx:
    def __init__(self, profile: dict, typ: dict, punct_mode: str, source_dir: Path,
                 fix_ranges: bool = False):
        self.profile = profile
        self.typ = typ
        self.punct_mode = punct_mode
        self.fix_ranges = fix_ranges
        self.source_dir = source_dir
        self.notes = None                     # 由 build_document 实例化（需要 doc）
        self.note_defs: dict[str, str] = {}   # 每份输入文件重置
        self.missing_notes: list[str] = []
        self.fig_no = 0
        self.punct_changes: dict[str, int] = {}
        self.punct_scanned: dict[str, int] = {}
        self.images: list[dict] = []
        self.missing_images: list[str] = []
        self.stats = {"headings": 0, "paragraphs": 0, "tables": 0, "quotes": 0,
                      "lists": 0, "code": 0, "images": 0}

    def punct(self, text: str) -> str:
        """按模式处理标点：report 只统计，fix 改写，off 不动。"""
        if self.punct_mode == "off":
            return text
        exempt = self.profile.get("punctuation", {}).get("exempt_quotes", True)
        if self.punct_mode == "fix":
            new, ch = normalize_punctuation(text, exempt, fix_ranges=self.fix_ranges)
            for k, v in ch.items():
                self.punct_changes[k] = self.punct_changes.get(k, 0) + v
            return new
        info = analyze_punctuation(text, exempt)
        for k, v in info["rules"].items():
            self.punct_scanned[k] = self.punct_scanned.get(k, 0) + v
        return text


INLINE_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _emit_code(paragraph, text: str, bold: bool, ctx: Ctx) -> None:
    pos = 0
    for m in INLINE_CODE_RE.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()]).bold = bold
        run = paragraph.add_run(m.group(1))
        run.bold = bold
        run.font.name = "Consolas"
        run_fonts(run, ctx.typ["fonts"]["mono"])
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:]).bold = bold


def add_runs(paragraph, text: str, ctx: Ctx) -> None:
    """行内 `**粗体**`、`` `等宽` ``、`![alt](路径)` 与注释引用。

    原先的嵌套修复（粗体里的等宽代码）保留：先切粗体、再在每段里切等宽，两种格式可叠加。"""
    text = INLINE_NOTE_RE.sub(lambda m: "\x01" + m.group(1) + "\x02", text)
    pos = 0
    for m in INLINE_BOLD_RE.finditer(text):
        if m.start() > pos:
            _emit_code(paragraph, text[pos:m.start()], False, ctx)
        _emit_code(paragraph, m.group(1), True, ctx)
        pos = m.end()
    if pos < len(text):
        _emit_code(paragraph, text[pos:], False, ctx)


def _emit_with_notes(paragraph, text: str, ctx: Ctx) -> None:
    """先处理注释引用与行内图片，再交给 add_runs 处理行内格式。"""
    note_font = ctx.typ["fonts"]["note"]
    note_size = ctx.typ["sizes_pt"].get("note", 9.0)
    pos = 0
    for m in re.finditer(r"\[\^([^\]]+)\]|\^\[([^\]]+)\]|!\[([^\]]*)\]\(([^)]+)\)", text):
        if m.start() > pos:
            add_runs(paragraph, text[pos:m.start()], ctx)
        if m.group(1) is not None:
            label = m.group(1)
            body = ctx.note_defs.get(label, "")
            if body:
                ctx.notes.add_reference(paragraph, _note_plain(body), note_font, note_size)
            else:
                ctx.missing_notes.append(label)
        elif m.group(2) is not None:
            ctx.notes.add_reference(paragraph, _note_plain(m.group(2)), note_font, note_size)
        else:
            _add_inline_image(paragraph, m.group(3).strip(), m.group(4).strip(), ctx)
        pos = m.end()
    if pos < len(text):
        add_runs(paragraph, text[pos:], ctx)


def _note_plain(text: str) -> str:
    """注释文本去行内标记（注释原文按纯文本渲染，避免 ** 与反引号进入 Word）。"""
    return INLINE_CODE_RE.sub(r"\1", INLINE_BOLD_RE.sub(r"\1", text)).strip()


def _resolve_image(raw: str, base: Path) -> Path | None:
    p = Path(raw)
    cand = p if p.is_absolute() else (base / p)
    return cand.resolve() if cand.is_file() else None


def _add_inline_image(paragraph, alt: str, raw: str, ctx: Ctx) -> None:
    path = _resolve_image(raw, ctx.source_dir)
    if path is None:
        ctx.missing_images.append(raw)
        run = paragraph.add_run(f"［缺图：{alt or raw}］")
        run.italic = True
        return
    _insert_picture(paragraph, path, ctx)


def _insert_picture(paragraph, path: Path, ctx: Ctx) -> None:
    max_cm = ctx.profile.get("figures", {}).get("max_width_cm")
    if max_cm is None:
        max_cm = ctx.typ["page"]["width_cm"] - 2 * ctx.typ["page"]["margin_left_cm"]
    stream = None
    width = Cm(max_cm)
    try:
        from PIL import Image
        if ctx.profile.get("figures", {}).get("color") == "grayscale":
            img = Image.open(path).convert("L")
            stream = io.BytesIO()
            img.save(stream, format="PNG")
            stream.seek(0)
            px_w, px_h = img.size
        else:
            with Image.open(path) as im:
                px_w, px_h = im.size
        # px → cm，按 96dpi；超版心宽则等比缩到版心
        natural_cm = px_w / 96.0 * 2.54
        if natural_cm > max_cm:
            width = Cm(max_cm)
    except Exception:
        width = Cm(max_cm)
    target = stream if stream is not None else str(path)
    try:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run().add_picture(target, width=width)
        ctx.stats["images"] += 1
        ctx.images.append({"path": str(path)})
    except Exception as e:                                  # pragma: no cover
        ctx.missing_images.append(f"{path}（读取失败：{e}）")


def fmt_paragraph(p, kind: str, ctx: Ctx) -> None:
    pf = p.paragraph_format
    apply_line_spacing(p._p.get_or_add_pPr(), ctx.typ.get("line_spacing"))
    if kind == "heading":
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(6)
    elif kind == "body":
        pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        set_first_line_chars(p, ctx.typ.get("first_line_indent_chars", 2.0))
    elif kind in ("bullet", "number"):
        pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    elif kind == "quote":
        pf.left_indent = Cm(0.74)


def setup_styles(doc, ctx: Ctx) -> None:
    """正文/标题字体（四槽 + hint）、字号、字重、东亚版式开关。"""
    typ = ctx.typ
    normal = doc.styles["Normal"]
    style_fonts(normal, typ["fonts"]["body"])
    normal.font.size = Pt(typ["sizes_pt"]["body"])
    apply_east_asian_typography(normal._element.get_or_add_pPr(), typ.get("east_asian", {}))

    sizes = typ["sizes_pt"]
    for hname, key in (("Heading 1", "h1"), ("Heading 2", "h2"),
                       ("Heading 3", "h3"), ("Heading 4", "h3")):
        style = doc.styles[hname]
        style_fonts(style, typ["fonts"]["heading"])
        style.font.size = Pt(sizes[key])
        style.font.bold = bool(typ.get("heading_bold", True))
        style.font.color.rgb = RGBColor(0, 0, 0)
        apply_east_asian_typography(style._element.get_or_add_pPr(), typ.get("east_asian", {}))


def setup_page(doc, ctx: Ctx, page_number: bool = True) -> None:
    typ = ctx.typ
    page = typ["page"]
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(page["width_cm"]), Cm(page["height_cm"])
    section.top_margin = Cm(page["margin_top_cm"])
    section.bottom_margin = Cm(page["margin_bottom_cm"])
    section.left_margin = Cm(page["margin_left_cm"])
    section.right_margin = Cm(page["margin_right_cm"])
    apply_doc_grid(section, typ.get("doc_grid", {}))
    if not page_number:
        return
    footer_p = section.footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer_p.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)


def add_cover(doc, ctx: Ctx, title: str, subtitle: str = "", author: str = "", date: str = "") -> None:
    """封面页：标题（大号黑体居中）+ 副标题 + 作者/日期，末尾分页。"""
    for _ in range(6):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(title or "（未命名文档）")
    run_fonts(run, ctx.typ["fonts"]["heading"])
    run.font.size = Pt(22)
    run.font.bold = True
    if subtitle:
        sp = doc.add_paragraph()
        sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        srun = sp.add_run(subtitle)
        run_fonts(srun, ctx.typ["fonts"]["heading"])
        srun.font.size = Pt(14)
    for _ in range(6):
        doc.add_paragraph()
    for line in (author, date):
        if not line:
            continue
        lp = doc.add_paragraph()
        lp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        lrun = lp.add_run(line)
        run_fonts(lrun, ctx.typ["fonts"]["body"])
        lrun.font.size = Pt(14)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _caption(doc, ctx: Ctx, text: str) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run_fonts(run, ctx.typ["fonts"]["body"])
    run.font.size = Pt(ctx.typ["sizes_pt"]["body"] - 1.5)


def render_blocks(doc, blocks: list[dict], ctx: Ctx) -> dict:
    cap_above = ctx.profile.get("figures", {}).get("caption_position") == "above"
    for block in blocks:
        kind = block["kind"]
        if kind == "heading":
            h = doc.add_heading("", level=block["level"])
            _emit_with_notes(h, ctx.punct(block["text"]), ctx)
            fmt_paragraph(h, "heading", ctx)
            ctx.stats["headings"] += 1
        elif kind == "table":
            rows = block["rows"]
            ncols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=ncols)
            # 表格框线：默认样式（Normal Table）在 Word 里**没有框线**，多列表格会串成一团。
            # Table Grid 是 python-docx 内置样式，同时也就是《开放时代》要求的「田」字型表格。
            try:
                table.style = "Table Grid"
            except Exception:
                pass
            for ri, row in enumerate(rows):
                for ci in range(ncols):
                    if ci < len(row):
                        cell = table.rows[ri].cells[ci]
                        _emit_with_notes(cell.paragraphs[0], ctx.punct(row[ci]), ctx)
                        if ri == 0:
                            for r in cell.paragraphs[0].runs:
                                r.bold = True
            ctx.stats["tables"] += 1
        elif kind == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            _emit_with_notes(p, ctx.punct(block["text"]), ctx)
            fmt_paragraph(p, "bullet", ctx)
            ctx.stats["lists"] += 1
        elif kind == "number":
            p = doc.add_paragraph(style="List Number")
            _emit_with_notes(p, ctx.punct(block["text"]), ctx)
            fmt_paragraph(p, "number", ctx)
            ctx.stats["lists"] += 1
        elif kind == "quote":
            p = doc.add_paragraph()
            _emit_with_notes(p, ctx.punct(block["text"]), ctx)
            p.italic = True
            fmt_paragraph(p, "quote", ctx)
            ctx.stats["quotes"] += 1
        elif kind == "code":
            # 代码块：等宽字体 + 浅底色 + 单倍行距，保留原始缩进与换行
            p = doc.add_paragraph()
            pf = p.paragraph_format
            pf.line_spacing = 1.0
            pf.space_before = Pt(4)
            pf.space_after = Pt(8)
            pf.alignment = WD_ALIGN_PARAGRAPH.LEFT
            pf.left_indent = Cm(0.4)
            code_lines = block["lines"] or [""]
            for li, line in enumerate(code_lines):
                run = p.add_run(line)
                run_fonts(run, ctx.typ["fonts"]["mono"])
                run.font.size = Pt(ctx.typ["sizes_pt"]["code"])
                if li != len(code_lines) - 1:
                    run.add_break()
            shade = OxmlElement("w:shd")
            shade.set(qn("w:val"), "clear")
            shade.set(qn("w:fill"), "F5F5F5")
            p._element.get_or_add_pPr().append(shade)
            ctx.stats["code"] += 1
        elif kind == "image":
            ctx.fig_no += 1
            alt = block["alt"]
            cap = f"图{ctx.fig_no}  {alt}" if alt else ""
            if cap and cap_above:
                _caption(doc, ctx, ctx.punct(cap))
            p = doc.add_paragraph()
            _add_inline_image(p, alt, block["path"], ctx)
            if cap and not cap_above:
                _caption(doc, ctx, ctx.punct(cap))
            if cap == "":
                ctx.fig_no -= 1                              # 无题图片不占用编号
        else:
            p = doc.add_paragraph()
            _emit_with_notes(p, ctx.punct(block["text"]), ctx)
            fmt_paragraph(p, "body", ctx)
            ctx.stats["paragraphs"] += 1
    return ctx.stats


def build_document(sources: list[tuple[Path, str, list[dict], dict]], args, profile: dict,
                   typ: dict, punct_mode: str, fix_ranges: bool = False):
    doc = Document()
    ctx = Ctx(profile, typ, punct_mode, sources[0][0].parent if sources else Path.cwd(),
              fix_ranges=fix_ranges)
    ctx.notes = NoteManager(doc, profile.get("notes", {}).get("form", "footnote"), profile)
    ctx.note_defs = {}
    ctx.missing_notes = []
    setup_styles(doc, ctx)
    setup_page(doc, ctx, page_number=not args.no_page_number)
    if args.cover:
        add_cover(doc, ctx, args.title or (sources[0][0].stem if sources else ""),
                  args.subtitle, args.author, args.date)

    totals = {k: 0 for k in ("headings", "paragraphs", "tables", "quotes", "lists", "code", "images")}
    totals.update({"files": 0, "blocks": 0, "notes": 0})
    for index, (path, text, _blocks, _meta) in enumerate(sources):
        if index > 0 and not args.no_page_break_between:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        ctx.source_dir = path.parent
        blocks, defs = extract_note_defs(parse_markdown(text, None))
        ctx.note_defs = defs
        ctx.missing_notes = []
        before = len(ctx.notes.items)
        render_blocks(doc, blocks, ctx)
        totals["blocks"] += len(blocks)
        totals["notes"] += len(ctx.notes.items) - before
        for key in ("headings", "paragraphs", "tables", "quotes", "lists", "code", "images"):
            totals[key] += ctx.stats[key]
            ctx.stats[key] = 0
        totals["files"] += 1

    wrote_note_part = ctx.notes.finalize(typ)
    totals["note_part"] = bool(wrote_note_part)
    totals["punct_changes"] = dict(ctx.punct_changes)
    totals["punct_scanned"] = dict(ctx.punct_scanned)
    totals["missing_images"] = list(ctx.missing_images)
    totals["missing_notes"] = sorted(set(ctx.missing_notes))
    totals["figures"] = ctx.fig_no
    return doc, totals


def read_markdown(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def collect_batch(root: Path) -> list[Path]:
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES]
    return [p for p in files if not any(seg.startswith(".") or seg == "Word版" for seg in p.relative_to(root).parts[:-1])]


# ---------------------------------------------------------------- 计划卡


def prepare_source(path: Path, profile: dict, args, punct_mode: str) -> dict:
    """预解析一份输入：截断、块、注释定义、图片、标点统计。"""
    raw = read_markdown(path)
    text, cut_line, dropped_lines, dropped_chars = split_writing_note(
        raw, None if args.keep_writing_note else args.strip_from)
    blocks = parse_markdown(text, None)
    blocks, defs = extract_note_defs(blocks)
    images, missing = [], []
    for b in blocks:
        raw_paths = []
        if b["kind"] == "image":
            raw_paths.append(b["path"])
        else:
            raw_paths += [m.group(4).strip() for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", b.get("text", ""))]
        for rp in raw_paths:
            if _resolve_image(rp, path.parent):
                images.append(rp)
            else:
                missing.append(rp)
    exempt = profile.get("punctuation", {}).get("exempt_quotes", True)
    scanned: dict[str, int] = {}
    if punct_mode != "off":
        for b in blocks:
            texts = [b.get("text", "")]
            if b["kind"] == "table":
                texts = [c for row in b["rows"] for c in row]
            for t in texts:
                for k, v in analyze_punctuation(t, exempt)["rules"].items():
                    scanned[k] = scanned.get(k, 0) + v
    refs = sum(len(NOTE_REF_RE.findall(b.get("text", ""))) + len(INLINE_NOTE_RE.findall(b.get("text", "")))
               for b in blocks)
    unresolved = sorted({m.group(1) for b in blocks
                         for m in NOTE_REF_RE.finditer(b.get("text", "")) if m.group(1) not in defs})
    return {"path": path, "raw": raw, "text": text, "blocks": blocks, "note_defs": defs,
            "cut_line": cut_line, "dropped_lines": dropped_lines, "dropped_chars": dropped_chars,
            "images": images, "missing_images": missing, "punct": scanned,
            "note_refs": refs, "unresolved_notes": unresolved}


def plan_card(plan: list[dict], prepared: list[dict], profile: dict, typ: dict,
              punct_mode: str, missing_fonts: list[str], ok_fonts: list[str], args) -> str:
    L: list[str] = []
    notes = profile.get("notes", {})
    form_cn = "尾注" if notes.get("form") == "endnote" else "脚注"
    fmt_cn = {"decimal": "阿拉伯数字", "decimalEnclosedCircle": "①②③"}.get(
        notes.get("number_format") or "decimal", notes.get("number_format"))
    restart_cn = {"continuous": "整篇连续编号", "eachPage": "每页重新编号"}.get(
        notes.get("restart") or "continuous", notes.get("restart"))
    status = profile.get("_typography_status", "known")

    L.append("═══ md → Word 转换计划（未落盘）═══")
    L.append(f"版本        : md2docx {VERSION}")
    L.append(f"版式档      : {profile.get('profile_id')}（{profile.get('display_name')}）"
             f"{'  ⚠ 排版层未知' if status == 'unknown' else ''}")
    if status == "unknown":
        L.append(f"              ↳ {profile.get('_typography_reason', '')}")
        L.append("              ↳ 字体/字号/行距/页边距沿用「中文论文默认版式」")
    levels = sorted({(e.get("evidence_level") or "?").split("（")[0].strip()
                     for e in profile.get("evidence", [])})
    if levels:
        L.append(f"证据等级    : {'、'.join(levels)}  （B/C 级为二手证据，投稿前需复核）")
    L.append(f"注释        : {form_cn} · {fmt_cn} · {restart_cn}")
    if notes.get("pending_review"):
        L.append(f"              ⚠ 待核对：{notes.get('pending_reason', '')}")
    L.append("")
    for i, (item, prep) in enumerate(zip(plan, prepared)):
        names = "、".join(p.name for p in item["inputs"])
        L.append(f"源文件 {i + 1}   : {names}（{len(item['inputs'])} 个，按序合并）")
        if prep["cut_line"]:
            L.append(f"  素材截断  : 从第 {prep['cut_line']} 行「{args.strip_from}」起截断，"
                     f"丢弃 {prep['dropped_lines']} 行 / 约 {prep['dropped_chars']} 字  ← 会丢内容")
        L.append(f"  块/注释   : {len(prep['blocks'])} 块 · 注释引用 {prep['note_refs']} 处"
                 f" · 注释定义 {len(prep['note_defs'])} 条")
        if prep["unresolved_notes"]:
            L.append(f"  ⚠ 未定义  : {prep['unresolved_notes']}")
        img = len(prep["images"])
        L.append(f"  图片      : {img} 个" + (f" · 缺失 {len(prep['missing_images'])}：{prep['missing_images']}"
                                              if prep["missing_images"] else "（全部找到）"))
        if prep["punct"]:
            detail = "、".join(f"{k}{v}" for k, v in prep["punct"].items())
            mode_cn = {"fix": "fix 模式", "report": "report 模式", "off": "off"}.get(punct_mode, punct_mode)
            tail = "（将改写）" if punct_mode == "fix" else "（不改写）"
            L.append(f"  标点      : 检出 {detail}  [{mode_cn}{tail}]")
        L.append(f"  输出      : {item['output']}")
    L.append("")
    L.append(f"字体预检    : 已确认 {len(ok_fonts)} 个"
             + (f"；⚠ 本机缺失 {len(missing_fonts)} 个 → {'、'.join(missing_fonts)}" if missing_fonts
                else "；无缺失"))
    if missing_fonts:
        L.append("              ↳ Word 会静默回退，产物字体与预期不符；请先安装或改 profile")
    if typ.get("heading_bold"):
        L.append("标题字重    : 黑体 + 加粗（heading_bold=true；决策 D-2 待定，可在 profile 改）")
    else:
        L.append("标题字重    : 黑体不加粗（heading_bold=false；按中文排版惯例，非期刊明文）")
    if punct_mode == "fix" and not getattr(args, "fix_ranges", False):
        L.append("数值范围    : 不改写（页码/年份/日期与数值范围不可靠区分；要改加 --fix-ranges）")
    L.append("")
    L.append("未落盘。加 --confirm 即执行；--dry-run 用于只看计划（退出码 0）。")
    return "\n".join(L)


# ---------------------------------------------------------------- 自测


def selftest() -> int:
    """自测：内置样例 → 转换 → 校验产物。

    覆盖历史缺口（``` 代码块、引用块内代码块、表格转义竖线、嵌套粗体+等宽）之外，
    v2 新增：脚注 part 与引用、尾注切换、w:hint、w:firstLineChars、东亚开关、
    行距三模式、图片、标点统计与改写。
    """
    import tempfile
    import zipfile

    sample = "\n".join([
        "# 一级标题",
        "",
        "正文一段，含 **加粗** 与 `行内代码`，还有半角标点, 例如这里。",
        "",
        "## 二级标题",
        "",
        "- 列表项一",
        "- 列表项二",
        "",
        "> 引用块一段。",
        "",
        "```powershell",
        "# 这是代码块内的注释，不应成为标题",
        "- 这不是列表项",
        "python --version",
        "```",
        "",
        "> ⚠️ 引用块里的代码块：",
        "> ```powershell",
        "> echo 引用块内代码",
        "> ```",
        "",
        "| 列A | 列B |",
        "| --- | --- |",
        "| a1 | **粗体单元格** |",
        "| `code` | b2 |",
        "| `x \\| y` | 转义竖线 |",
        "",
        "普通句子含 **粗体里的 `等宽`** 结束。",
        "",
        "带脚注的一句[^n1]。",
        "",
        "[^n1]: 这是脚注正文。",
        "",
    ])

    checks: list[tuple[str, bool, str]] = []
    tmp = Path(tempfile.mkdtemp(prefix="md2docx-selftest-"))
    try:
        profile = load_profile("default")
        typ = effective_typography(profile)

        class A:
            pass
        a = A()
        a.no_page_number = False
        a.cover = False
        a.title = a.subtitle = a.author = a.date = ""
        a.no_page_break_between = False
        a.strip_from = DEFAULT_STRIP_MARKER
        a.keep_writing_note = True

        out = tmp / "sample.docx"
        src = [(tmp / "sample.md", sample, None, None)]
        doc, totals = build_document(src, a, profile, typ, "report")
        doc.save(str(out))

        # ---- 包结构 / 脚注 part
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            has_fn = "word/footnotes.xml" in names
            ct = z.read("[Content_Types].xml").decode("utf-8")
            rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
            doc_xml = z.read("word/document.xml").decode("utf-8")
            fn_xml = z.read("word/footnotes.xml").decode("utf-8") if has_fn else ""
            styles_xml = z.read("word/styles.xml").decode("utf-8")

        from xml.etree import ElementTree as ET
        W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

        # 为什么必须「解析」而不能只做子串断言：XML 非法时（例如标签被拼成 <w:w:footnote>）
        # 子串照样匹配，而 Word 打开会报修复。实测踩过一次，故这里对包内每个 part 真解析。
        bad_parts = []
        with zipfile.ZipFile(out) as z:
            for n in z.namelist():
                if n.endswith((".xml", ".rels")):
                    try:
                        ET.fromstring(z.read(n))
                    except ET.ParseError as e:
                        bad_parts.append(f"{n}: {e}")
        checks.append(("包内全部 XML part 均可解析", not bad_parts, "；".join(bad_parts)))

        def _try_parse(text_or_bytes):
            try:
                blob = text_or_bytes if isinstance(text_or_bytes, (bytes, bytearray)) \
                    else text_or_bytes.encode("utf-8")
                return ET.fromstring(blob), None
            except ET.ParseError as e:
                return None, str(e)

        root_fn, err_fn = _try_parse(fn_xml)
        root_doc, err_doc = _try_parse(doc_xml)
        if root_fn is None or root_doc is None:
            checks.append(("footnotes.xml / document.xml 可解析", False,
                           f"footnotes:{err_fn} document:{err_doc}"))
            root_fn = root_fn if root_fn is not None else ET.fromstring(
                f'<w:footnotes xmlns:w="{W[1:-1]}"/>')
            root_doc = root_doc if root_doc is not None else ET.fromstring(
                f'<w:document xmlns:w="{W[1:-1]}"/>')
        fn_kids = [k for k in list(root_fn) if k.tag == W + "footnote"]
        fn_user = [k for k in fn_kids if k.attrib.get(W + "id") not in ("-1", "0")]
        fn_seps = [k for k in fn_kids if k.attrib.get(W + "type")]
        fn_texts = "".join(t.text or "" for k in fn_user for t in k.iter(W + "t"))
        refs = list(root_doc.iter(W + "footnoteReference"))

        checks.append(("生成了 footnotes.xml part", has_fn, ""))
        checks.append(("footnotes 内容类型已注册", "footnotes+xml" in ct, ""))
        checks.append(("footnotes 关系已建立", "relationships/footnotes" in rels, ""))
        checks.append(("footnotes.xml 合法且根为 w:footnotes", root_fn.tag == W + "footnotes", root_fn.tag))
        checks.append(("含 2 个特殊注释 + 1 条用户注释",
                       len(fn_seps) == 2 and len(fn_user) == 1,
                       f"特殊={len(fn_seps)} 用户={len(fn_user)}"))
        checks.append(("正文恰含 1 个 w:footnoteReference", len(refs) == 1, f"{len(refs)}"))
        checks.append(("脚注正文在 w:t 内（独立解析器可读出）", "这是脚注正文" in fn_texts, fn_texts[:40]))
        checks.append(("sectPr 含 w:footnotePr + numFmt + numRestart",
                       root_doc.find(".//" + W + "footnotePr") is not None, ""))

        # ---- 字体槽 / hint / 缩进 / 东亚开关 / 行距
        checks.append(("样式含 rFonts 四槽 + w:hint",
                       'w:eastAsia="宋体"' in styles_xml and 'w:hint="eastAsia"' in styles_xml
                       and 'w:ascii="Times New Roman"' in styles_xml, ""))
        checks.append(("正文含 firstLineChars=200（真 2 字符）",
                       'w:firstLineChars="200"' in doc_xml, ""))
        checks.append(("无遗留 w:firstLine 磅值缩进",
                       'w:firstLine="' not in doc_xml, ""))
        checks.append(("东亚版式开关已写入",
                       all(f"w:{t}" in styles_xml for t in
                           ("kinsoku", "wordWrap", "overflowPunct", "autoSpaceDE", "autoSpaceDN")), ""))
        checks.append(("行距以 w:line 半磅表示（1.5 倍 = 360）",
                       'w:line="360"' in styles_xml or 'w:line="360"' in doc_xml, ""))

        # ---- 原有缺口回归
        doc2 = Document(str(out))
        all_text = "\n".join(p.text for p in doc2.paragraphs)
        headings = [p.text for p in doc2.paragraphs if p.style.name.startswith("Heading")]
        checks.append(("识别出 2 个代码块", totals.get("code") == 2, f"code={totals.get('code')}"))
        checks.append(("无残留 ``` 标记", "```" not in all_text, "发现残留" if "```" in all_text else ""))
        checks.append(("代码块内容保留",
                       "这是代码块内的注释" in all_text and "python --version" in all_text, ""))
        checks.append(("引用块内代码块内容保留", "echo 引用块内代码" in all_text, ""))
        checks.append(("代码块内的 # 未变成标题",
                       not any("这是代码块内的注释" in h for h in headings), f"标题={headings}"))
        checks.append(("代码块内的 - 未变成列表项",
                       not any(p.style.name == "List Bullet" and "这不是列表项" in p.text for p in doc2.paragraphs), ""))
        checks.append(("表格 1 个、4 行", len(doc2.tables) == 1 and len(doc2.tables[0].rows) == 4,
                       f"tables={len(doc2.tables)}"))
        checks.append(("表格列数为 2（\\| 未被当成分隔符）",
                       bool(doc2.tables) and len(doc2.tables[0].columns) == 2, ""))
        cell_text = "\n".join(c.text for t in doc2.tables for r in t.rows for c in r.cells)
        checks.append(("转义竖线还原为 |", "x | y" in cell_text, f"{cell_text[-40:]!r}"))
        checks.append(("单元格内 ** 已渲染（无残留标记）", "**" not in cell_text and "`" not in cell_text, ""))
        checks.append(("单元格 `等宽` 用 Consolas",
                       any(r.font.name == "Consolas" for t in doc2.tables for row in t.rows
                           for c in row.cells for p in c.paragraphs for r in p.runs), ""))
        checks.append(("表格有框线样式（非 Normal Table）",
                       bool(doc2.tables) and doc2.tables[0].style.name != "Normal Table", ""))
        checks.append(("表头加粗",
                       bool(doc2.tables) and all(r.bold for c in doc2.tables[0].rows[0].cells
                                                 for p in c.paragraphs for r in p.runs if r.text.strip()), ""))
        checks.append(("标题层级正确（2 个标题）", len(headings) == 2, f"{headings}"))
        checks.append(("引用块被识别", totals.get("quotes", 0) >= 1, ""))
        nested = [r for p in doc2.paragraphs if "粗体里的" in p.text
                  for r in p.runs if r.text == "等宽"]
        checks.append(("嵌套：粗体里的等宽同时粗体+Consolas",
                       bool(nested) and all(r.bold and r.font.name == "Consolas" for r in nested), ""))
        checks.append(("嵌套：不留反引号",
                       all("`" not in p.text for p in doc2.paragraphs if "粗体里的" in p.text), ""))

        # ---- 尾注切换
        prof_end = dict(profile)
        prof_end["notes"] = {"form": "endnote", "number_format": "decimalEnclosedCircle",
                             "restart": "eachPage"}
        out2 = tmp / "end.docx"
        doc3, _ = build_document(src, a, prof_end, typ, "off")
        doc3.save(str(out2))
        with zipfile.ZipFile(out2) as z:
            n2 = z.namelist()
            d2 = z.read("word/document.xml").decode("utf-8")
            e2 = z.read("word/endnotes.xml").decode("utf-8") if "word/endnotes.xml" in n2 else ""
        root_en, err_en = _try_parse(e2)
        if root_en is None:
            root_en = ET.fromstring(f'<w:endnotes xmlns:w="{W[1:-1]}"/>')
        en_user = [k for k in list(root_en)
                   if k.tag == W + "endnote" and k.attrib.get(W + "id") not in ("-1", "0")]
        en_texts = "".join(t.text or "" for k in en_user for t in k.iter(W + "t"))
        root_d2, err_d2 = _try_parse(d2)
        if root_d2 is None:
            root_d2 = ET.fromstring(f'<w:document xmlns:w="{W[1:-1]}"/>')
        checks.append(("尾注模式生成 endnotes.xml", "word/endnotes.xml" in n2, ""))
        checks.append(("endnotes.xml 合法且含 1 条用户尾注",
                       root_en.tag == W + "endnotes" and len(en_user) == 1,
                       f"tag={root_en.tag} n={len(en_user)} err={err_en}"))
        checks.append(("尾注模式正文用 endnoteReference",
                       len(list(root_d2.iter(W + "endnoteReference"))) == 1, ""))
        checks.append(("尾注 numFmt=decimalEnclosedCircle 且 eachPage",
                       'w:val="decimalEnclosedCircle"' in d2 and 'w:val="eachPage"' in d2, ""))
        checks.append(("尾注无 footnote 引用残留",
                       len(list(root_d2.iter(W + "footnoteReference"))) == 0
                       and "word/footnotes.xml" not in n2, ""))
        checks.append(("尾注 part 内容正确", "这是脚注正文" in en_texts, en_texts[:40]))

        # ---- 标点
        seg = "这里用半角逗号, 和一个省略号..."
        info = analyze_punctuation(seg, True)
        checks.append(("标点统计能检出半角句读", info["rules"].get("半角句读→全角", 0) >= 1, f"{info}"))
        fixed, ch = normalize_punctuation(seg, True)
        checks.append(("fix 模式改写半角句读与省略号（顺序正确，不出现 。..）",
                       "，" in fixed and "……" in fixed and "。." not in fixed, f"{fixed!r}"))
        keep, _ = normalize_punctuation("见 `a, b` 与 http://x.com/a,b 保持不动", True)
        checks.append(("保护区内标点不被改写", "`a, b`" in keep and "http://x.com/a,b" in keep, f"{keep!r}"))
        q, _ = normalize_punctuation("“引文内的半角逗号, 不改”", True)
        checks.append(("引文内容默认豁免", ", " in q, f"{q!r}"))
        yr, _ = normalize_punctuation("1980-1990 与 63-68%", True, fix_ranges=True)
        checks.append(("年份起止保留一字线、数值范围转浪纹（需 fix_ranges）",
                       "1980-1990" in yr and "63～68%" in yr, f"{yr!r}"))
        yr2, _ = normalize_punctuation("1980-1990 与 63-68%", True)
        checks.append(("默认不改写数值范围（页码/年份/日期不可靠区分）",
                       "63-68%" in yr2 and "63～68%" not in yr2, f"{yr2!r}"))
        dt, _ = normalize_punctuation("编写日期：2026-08-31", True, fix_ranges=True)
        checks.append(("日期不被范围规则破坏", "2026-08-31" in dt, f"{dt!r}"))
        dash, _ = normalize_punctuation("1912—1930 年间与 3—5 个", True)
        checks.append(("数字间的一字线不被当成破折号",
                       "1912—1930" in dash and "3—5" in dash and "——" not in dash, f"{dash!r}"))

        # ---- 图片
        try:
            from PIL import Image
            img_path = tmp / "fig.png"
            Image.new("RGB", (800, 400), (200, 40, 40)).save(img_path)
            md_img = f"![示意图]({img_path.name})\n\n"
            src_img = [(tmp / "img.md", md_img, None, None)]
            out3 = tmp / "img.docx"
            doc4, t4 = build_document(src_img, a, dict(profile, figures={"caption_position": "above",
                                                                        "color": "grayscale"}),
                                      typ, "off")
            doc4.save(str(out3))
            with zipfile.ZipFile(out3) as z:
                media = [n for n in z.namelist() if n.startswith("word/media/")]
            checks.append(("图片被嵌入 docx", len(media) >= 1 and t4.get("images", 0) == 1, f"media={media}"))
            doc5 = Document(str(out3))
            caps = [p.text for p in doc5.paragraphs if p.text.startswith("图1")]
            checks.append(("图题按 profile 置于上方且含编号", bool(caps), f"{caps}"))
        except ImportError:                                   # pragma: no cover
            checks.append(("图片测试", False, "无 Pillow"))

        # ---- 缺图与未定义注释的报告
        miss_md = tmp / "miss.md"
        miss_md.write_text("正文一句。\n\n![缺图](nope.png)\n\n引用未定义[^ghost]。\n", encoding="utf-8")
        prep = prepare_source(miss_md, profile, a, "report")
        checks.append(("prepare_source 能统计注释引用", prep["note_refs"] >= 1, f"{prep['note_refs']}"))
        checks.append(("prepare_source 能报出缺失图片",
                       prep["missing_images"] == ["nope.png"], f"{prep['missing_images']}"))
        checks.append(("prepare_source 能报出未定义注释引用",
                       prep["unresolved_notes"] == ["ghost"], f"{prep['unresolved_notes']}"))
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)

    print(f"md2docx 自测（v{VERSION}）：")
    ok = True
    for name, passed, detail in checks:
        print(f"  {'✅' if passed else '❌'} {name}" + (f"（{detail}）" if detail and not passed else ""))
        ok &= passed
    print(f"自测{'通过 ✅' if ok else '失败 ❌'}（{sum(1 for _, p, _ in checks if p)}/{len(checks)}）")
    return 0 if ok else 1


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="md2docx", description="Markdown → Word（统一版 v2）")
    parser.add_argument("inputs", nargs="*", help="输入 .md 文件（给多个则按顺序合并为一个文档）")
    parser.add_argument("-o", "--output", help="输出 .docx（单文件模式默认同名；合并模式默认 <首个文件名>_合并.docx）")
    parser.add_argument("--batch", help="批量转换该目录下所有 .md（递归）")
    parser.add_argument("--outdir", help="批量输出目录（默认与源文件同目录）")
    parser.add_argument("--mirror", action="store_true", help="批量时保持相对目录结构")
    parser.add_argument("--profile", default="default",
                        help="版式档：default / sociology_studies / political_studies / open_times"
                             "（或 .json 路径）；--list-profiles 查看全部")
    parser.add_argument("--list-profiles", action="store_true", help="列出可用版式档后退出")
    parser.add_argument("--confirm", action="store_true",
                        help="确认落盘。不加则只打印计划卡、不写文件（v2 行为变更）")
    parser.add_argument("--normalize-punct", choices=["off", "report", "fix"], default=None,
                        help="标点模式：off 不处理 / report 只统计（默认）/ fix 按规则改写")
    parser.add_argument("--fix-ranges", action="store_true",
                        help="fix 模式下同时改写数值范围为「～」。默认不改：页码/年份/日期与数值"
                             "范围在文本上不可靠区分（实测 74-96、2026-08-31 会被误改）")
    parser.add_argument("--strip-from", default=DEFAULT_STRIP_MARKER,
                        help=f"从该标记所在行起截断（默认 {DEFAULT_STRIP_MARKER!r}）")
    parser.add_argument("--keep-writing-note", action="store_true", help="不截断写作说明段")
    parser.add_argument("--cover", action="store_true", help="生成封面页")
    parser.add_argument("--title", default="", help="封面标题")
    parser.add_argument("--subtitle", default="", help="封面副标题")
    parser.add_argument("--author", default="", help="封面作者")
    parser.add_argument("--date", default="", help="封面日期")
    parser.add_argument("--no-page-number", action="store_true", help="页脚不插入页码")
    parser.add_argument("--no-page-break-between", action="store_true", help="合并模式不在文件之间分页")
    parser.add_argument("--dry-run", "--plan", dest="dry_run", action="store_true",
                        help="只打印计划卡，不写文件（退出码 0）。走 .tools\\run.py 时请用 --plan："
                             "工具库包装器会把自己的同名 --dry-run 截走（toolkit.py 的 hoist_engine_flags）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--version", action="version", version=f"md2docx {VERSION}")
    parser.add_argument("--selftest", action="store_true", help="自测：内置样例跑一遍并校验产物")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.list_profiles:
        print("可用版式档：")
        for name in list_profiles():
            try:
                p = load_profile(name)
                print(f"  {name:20s} {p.get('display_name', '')}")
            except Exception as e:                            # pragma: no cover
                print(f"  {name:20s} [读取失败：{e}]")
        return 0

    try:
        profile = load_profile(args.profile)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    typ = effective_typography(profile)
    punct_mode = args.normalize_punct or profile.get("punctuation", {}).get("mode", "report")

    # ---- 计划
    plan: list[dict] = []
    if args.batch:
        root = Path(args.batch).expanduser().resolve()
        if not root.is_dir():
            print(f"批量目录不存在：{root}", file=sys.stderr)
            return 1
        files = collect_batch(root)
        if not files:
            print(f"目录下没有 .md 文件：{root}", file=sys.stderr)
            return 1
        outdir = Path(args.outdir).expanduser().resolve() if args.outdir else None
        for src in files:
            if outdir is None:
                dst = src.with_suffix(".docx")
            elif args.mirror:
                dst = outdir / src.relative_to(root).with_suffix(".docx")
            else:
                dst = outdir / (src.stem + ".docx")
            plan.append({"inputs": [src], "output": dst})
    else:
        if not args.inputs:
            parser.print_help()
            return 1
        sources = [Path(p).expanduser().resolve() for p in args.inputs]
        missing = [str(p) for p in sources if not p.is_file()]
        if missing:
            print("输入文件不存在：" + "、".join(missing), file=sys.stderr)
            return 1
        if args.output:
            dst = Path(args.output).expanduser().resolve()
        elif len(sources) == 1:
            dst = sources[0].with_suffix(".docx")
        else:
            dst = sources[0].with_name(sources[0].stem + "_合并.docx")
        plan.append({"inputs": sources, "output": dst})

    prepared = [prepare_source(item["inputs"][0], profile, args, punct_mode) for item in plan]
    missing_fonts, ok_fonts = check_fonts(typ, profile.get("fonts_preflight", {}).get("aliases", {}))
    card = plan_card(plan, prepared, profile, typ, punct_mode, missing_fonts, ok_fonts, args)

    if args.dry_run:
        print(card)
        if args.json:
            print(json.dumps({"dry_run": True, "profile": profile.get("profile_id"),
                              "typography_status": profile.get("_typography_status"),
                              "missing_fonts": missing_fonts, "punct_mode": punct_mode,
                              "plan": [{"inputs": [str(p) for p in i["inputs"]],
                                        "output": str(i["output"]),
                                        "blocks": len(p["blocks"]), "note_refs": p["note_refs"],
                                        "images": len(p["images"]), "missing_images": p["missing_images"],
                                        "dropped_chars": p["dropped_chars"],
                                        "punct": p["punct"]} for i, p in zip(plan, prepared)]},
                             ensure_ascii=False))
        return 0

    if not args.confirm:
        print(card)
        print("\n未加 --confirm，未写任何文件（退出码 3）。", file=sys.stderr)
        return 3

    # ---- 落盘
    results = []
    for item, prep in zip(plan, prepared):
        srcs = [(p, read_markdown(p), None, None) for p in item["inputs"]]
        doc, totals = build_document(srcs, args, profile, typ, punct_mode, args.fix_ranges)
        item["output"].parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(item["output"]))
        record = {"output": str(item["output"]),
                  "inputs": [str(p) for p in item["inputs"]],
                  "profile": profile.get("profile_id"),
                  "bytes": item["output"].stat().st_size, **totals}
        results.append(record)
        print("OK: {} ← {} 个文件（标题 {}，段落 {}，表格 {}，图片 {}，注释 {}）".format(
            item["output"], totals["files"], totals["headings"], totals["paragraphs"],
            totals["tables"], totals["images"], totals["notes"]))
        if totals.get("missing_images"):
            print("  ⚠ 缺图：" + "、".join(totals["missing_images"]), file=sys.stderr)
        if totals.get("missing_notes"):
            print("  ⚠ 未定义的注释引用：" + "、".join(totals["missing_notes"]), file=sys.stderr)
        if totals.get("punct_changes"):
            print("  标点已改写：" + "、".join(f"{k}{v}" for k, v in totals["punct_changes"].items()))
    if args.json:
        print(json.dumps({"converted": len(results), "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
