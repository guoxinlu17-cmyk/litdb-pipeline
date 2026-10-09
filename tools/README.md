# tools —— 按流水线阶段排列

所有工具都在**同一个目录**里：它们之间按裸模块名互相 import（例如 `from _paths import lit_dir`、
`import _cite_page_candidates as C`），**拆到子目录会直接 import 失败**。
所以本目录是扁平的，这是刻意设计，不是没整理。

文件名保留 `_` 前缀（`_paths.py` 等），因为改名的收益只是好看，代价是同步改 29 个文件的 import。

---

## 阶段 0 · 可移植层（不是工具，是共享模块）

| 模块 | 做什么 |
| --- | --- |
| `_paths.py` | **所有路径解析的唯一入口**。按「命令行 → 环境变量 → `项目布局.json` → 结构探测 → 报错」解析。结构探测按**结构**不按名字找（含非空 `.kb/kb.sqlite` 或 `05_文献数据库` 的目录＝项目根）。**0 文档的空壳库不算候选**——引擎在错路径上会自动建空库，那会造成"多个候选→解析失败"。 |

`_paths.py` 还导出 **`LazyPath`**：模块级常量写成 `LIT = LazyPath(lit_dir)` 时，
路径**推迟到第一次真正使用才解析**，因此未配置项目时 `--help` 仍能正常工作。
（44 个工具里 27 个因此可用；剩下 17 个是模块级就真的去读库的审计类工具。）

诊断当前解析结果：

```bash
python tools/_paths.py            # 打印各逻辑路径解析到什么，未解析的说明原因
python tools/_paths.py --selftest # 26 项自测
```

---

## 阶段 1–2 · 素材体检 / 版式预检

| 工具 | 调用 | 说明 |
| --- | --- | --- |
| `_pdf_scan_check.py` | `<pdf...>` | 页数、有无文字层、文字层质量 |
| `_ocr_layout.py` | `<pdf> [--pages N] [--dpi N] [--json FILE]` | 抽样判版式（栏数/通栏/页眉页脚/竖排），产出 `.layout.json` + 警告 |

## 阶段 3 · 文本化（三条路线择一）

| 工具 | 调用 | 说明 |
| --- | --- | --- |
| `_ocr_pdf_to_md.py` | `<pdf> [out] [--dpi] [--pages] [--resume] [--profile]` | **路线①**：版式感知 OCR，产出 `.md` + `.layout.json` + `.quality.md` |
| `_docx_pages_index.py` | `[--file FILE] [--tag TAG]` | **路线②**前置：把 docx 按节拆出「页眉页数字符串」，产出逐页 JSON |
| `_docx_pages_to_md.py` | `[卷号...] [--corpus NAME]` | **路线②**：按页重建带印刷页码的 `.PAGES.md`（免 OCR） |
| `_docx_full_text.py` | `<docx> [out] [--footnotes-last]` | docx 纯文本抽取（含脚注/尾注） |
| `_ocr_md_to_pages.py` | `[--vols ...]` | **路线③**：已有 OCR 但页码是 PDF 页序 → 换成印刷页码 |
| `_ocr_md_to_docx.py` | `<md> [out] [--page-breaks]` | md → docx（用 `md2docx_unified`） |
| `md2docx_unified.py` | `python md2docx_unified.py <md> --out <docx>` | 通用 Markdown→Word 排版器，带期刊版式档案（`md2docx_profiles/`） |

## 阶段 4–5 · 页码标定与口径统一

| 工具 | 调用 | 说明 |
| --- | --- | --- |
| `_page_number.py` | `<file...> [--json FILE] [--tol N]` | **投票求偏移 + 结构校验**：PDF 页序 − 印刷页码 |
| `_verify_page_offset.py` | `<cases.json>` | 逐条自证偏移量（必需外置用例） |
| `_verify_ocr_pages.py` | `<源.pdf> <OCR.md>` | 页标记与印刷页码的一致性核对 |
| `_docx_pageinfo_check.py` | `<docx>` | 检查 docx 里页眉部件是否带得出页码 |

> **偏移量必须逐卷自证，不能照抄。** 判据是"命中页文本里真的印着推算出的页码，且差值**成片**而非散点"。

## 阶段 6 · 入库

入库由 **DSH 知识库引擎**完成（见根 README「环境要求」）。两个入口：

| 入口 | 说明 |
| --- | --- |
| `kb_ingest`（DSH 工具） | 常规路径，**必须显式传 `kb_root`** |
| `kb_cli.py` | **保底通道**：不经 DSH 插件，直接用命令行驱动引擎（`stats`/`ingest`/`search`/`rag`） |

写库前必须过护栏：

| 工具 | 调用 | 说明 |
| --- | --- | --- |
| `_kb_guard.py` | `--selftest`；代码里 `KbGuard(DB, reason=...)` | 备份 → 事务执行 → 写后校验。**不绿就不要写库** |

## 阶段 7–8 · 结构核查与检索验证

| 工具 | 说明 |
| --- | --- |
| `_kb_audit2.py` / `_kb_audit3.py` | 库结构审计（悬空引用、块/向量一致性、重复 sha256） |
| `_kb_coverage_vs_files.py` | 磁盘素材 vs 库内文档的覆盖对照 |
| `_kb_meta_gaps.py` | 元数据缺失面量化 |
| `_kb_dup_inspect.py` | 重复文档细看 |
| `_kb_chunk_probe.py` | 单文档分块长度分布（识别"无文本层"扫描件） |
| `_kb_show_doc.py` | 看指定 id 的文档片段（判断空壳/噪声条目） |
| `_kb_find_text.py` | `"要查的句子"...` | 逐字引文检索不到时诊断落在哪个块 |
| `_index_overlap2.py` | 索引重叠检查 |
| `_litdb_consistency.py` | 题录库一致性 |
| `_db_family_audit.py` | 数据目录家族盘点 |

## 阶段 9–11 · 引文定位 / 交叉验证 / 校勘回填

| 工具 | 说明 |
| --- | --- |
| `_cite_page_candidates.py` | 从论文稿里抽引注 → 在页级文本里找候选页（产出 CSV 台账） |
| `_quote_spans.py` | 全文一次配对引号，避免"小窗口配对错位" |
| `_quote_collation.py` | 逐字校勘：difflib 比对论文引文与命中页原文，差异逐条列出 |
| `_crosscheck_pages.py` | **两条独立路径**算出的页码交叉验证 |
| `_resolve_full_pages.py` | 用邻页页码连续序列自证 |
| `_spot_check_pages.py` | `<cases.json>` | 抽查已定页引文落在哪页、上下文是什么 |
| `_verify_cite_context.py` | `<cases.json>` | 核对引文语境与页眉年份，看论文的年份标注站不站得住 |
| `_insert_page_numbers.py` | `--apply` | 把已定位页码写回论文的卷次标注（默认试运行，自动备份） |
| `_build_second_level_db.py` | 从库内文献的脚注/参考文献抽二级文献，汇总建库 |

## 阶段 12 · 治理

| 工具 | 说明 |
| --- | --- |
| `_kb_delete_doc.py` | `ids... [--apply]` | 按 id 删文档（含分块/向量） |
| `_tool_inventory.py` | `--since` | 工作目录产物清单 |

---

## 访谈材料分支（**另一套方法，不是本流水线的一步**）

访谈没有印刷页码，锚点只能是**时间码**。四个专用工具自成一体：

| 工具 | 说明 |
| --- | --- |
| `_interview_check.py` | **入库前干跑自检**：五类静默失败 + PII 检出（手机/邮箱）。**脱敏必须在入库之前** |
| `_interview_norm.py` | 转写稿规范化（文件名定元数据、时间码进小节标题） |
| `_interview_verify.py` | 只读体检（`file:...?mode=ro`，路径错就报错、绝不建空库） |
| `_interview_experiment.py` | 检索效果对照实验 |

方法见 [`../docs/访谈材料分支.md`](../docs/访谈材料分支.md)。

---

## 自测

```bash
python run_selftests.py      # 在仓库根跑，8 个工具 / 约 190 项断言
python run_selftests.py --keep   # 保留临时项目骨架便于排查
```

带自测的工具：`_paths.py`(26)、`_insert_page_numbers.py`(12)、`_interview_check.py`(21)、
`_interview_norm.py`(31)、`_interview_verify.py`(23)、`_interview_experiment.py`(16)、
`_kb_guard.py`、`md2docx_unified.py`(50)。

---

## 重要：这些工具**不是拿来即用**的

大部分工具绑定了具体项目结构（目录名、卷数、题录库字段）。复用方式是
**复用流程与思路 + 按需参数化脚本**，不是复制粘贴就能跑。
判定依据与分级清单见 [`../docs/SOP.md`](../docs/SOP.md) §5。

另有一条硬性限制见根 README「已知限制」：**44 个工具里有 17 个在未配置项目时无法运行**
（路径解析已惰性化，`--help` 可用 27 个；剩下 17 个是模块级就真的去读库的审计类工具）。
这是"宁可报错不要猜"的设计后果，不是 bug。
