# litdb-pipeline · Page-anchored literature database pipeline

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![selftests](https://img.shields.io/badge/selftests-8%2F8%20passing-brightgreen.svg)](run_selftests.py)

[中文文档](README.md) · English

Turn **scans / PDFs / Word files** into **text that carries the printed page numbers** — so every
passage you retrieve can answer *"which page of the physical book is this on?"* — and push that
number all the way back into your footnotes.

---

## The problem it solves

You quote a passage from a scanned book in your paper, citing "p. 294". Three months later a
reviewer asks about it, and you go back to search — **you find the passage, but you cannot confirm
it is actually on page 294.** Because:

- OCR gives you the **PDF page order**, not the number printed on the page;
- printed p. 294 may be PDF page 301 (front matter, preface, table of contents come first);
- and the offset differs per volume.

This project welds that link shut: **the page number enters the text at the transcription stage**
and flows through to the `section` field of every search hit. Search "page 294" and you get the
294th page *of the book*.

### What input and output look like

Input is a `.docx` where each page is a section and the running header carries the printed page
number (or a scanned PDF with no text layer).

Output is Markdown with page markers:

```markdown
#### Page 294

【header】644 Collected Works
...body text...

> **Provenance and reliability — read first**
> - This is **not OCR output**: rebuilt page-by-page from a converted file, 812 pages total.
> - `#### Page N` — N is the number **printed on the page** (taken from the running header),
>   not the PDF page order. **Offset is 0 when you use this file.**
> - Page-less sections are merged into the preceding numbered page. **Merged, never
>   interpolated — so page numbers are never invented.**
> - **Always verify against the physical copy**: the text came from scan conversion and may
>   contain misreads.
```

Page markers are **headings** rather than horizontal rules on purpose: the chunker lifts them into
`section`, so retrieved passages show the hit page directly.

---

## Two halves — the first one stands alone

The repository has two halves, and **only the first is mandatory**.

### Half A · Transcription and page anchoring — standalone, Python only

```
PDF / scans / docx
      │
      ├─ Intake check: page count, text layer present?, layout type
      ├─ Layout probe: columns / full-width / header-footer / vertical writing
      ├─ Transcription: layout-aware OCR │ docx header method │ OCR-to-printed-page
      └─ Page calibration: voting offset + structural validation
      ↓
Markdown carrying printed page markers   ← feed it to Obsidian, grep, any RAG system
```

**This half depends on no external engine.** The output is plain text; use it however you like.

### Half B · Knowledge-base retrieval and citation work — optional

For cross-volume search, automatic citation location, verbatim collation and page back-fill, you
also need a retrieval engine (this project uses DSH + `dsh-kb-rag`; see *Optional: retrieval
engine*). **Half A works without it.**

---

## Two invariants

The whole pipeline obeys just these two; everything else is implementation detail.

> **Invariant 1: the page number is the spine.**
> From the scanned page onward, the page number enters the text markers and flows through to
> `section` / `page_start` and into the citation. **Break that thread and everything downstream is
> guesswork.**

> **Invariant 2: leave a blank rather than guess.**
> The dangerous failure is not "cannot read it" but "**misread it and it still looks plausible**".
> Unreadable → record `status` honestly. Page undeterminable → leave `NULL`. Unverified → mark
> *"to be checked against the physical copy"*.

If your project does not need page-traceable citations you can drop the layout and page-unification
stages — but do **not** fabricate page numbers with a "carry-forward" fallback.

---

## Five-layer architecture

```
L1 Source        PDF / DOCX / scans / existing Markdown
                    ↓  intake check: pages, text layer, layout
L2 Transcription three routes, pick per source type (mixable)     ┐
                    ① layout-aware OCR       ← scans, no text layer │ s
                    ② docx running header    ← one section per page │ t
                    ③ OCR text → printed page← OCR exists, but PDF order │ a
                    ↓  output: per-page text + page markers + QC report │ n
L3 Structure     chunking + page fields + vectors  ← needs an engine ┘ d
                    ↓                                                   a
L4 Application   search / citation location / collation / back-fill / secondary bibliography │ l
                    ↓                                                                          │ o
L5 Governance    structure audit / dedup / metadata repair / backup & rollback                ┘
```

---

## Quick start

### Step 0 — See what it does (no configuration required)

```bash
git clone https://github.com/guoxinlu17-cmyk/litdb-pipeline.git
cd litdb-pipeline
python examples/demo.py
```

`demo.py` generates a **synthetic sample** in a temp directory (a docx with one section per page,
printed page numbers in the running headers), then runs *build page index → write page-marked
Markdown* and prints the result.

The sample has only 6 pages, but its printed page numbers are **101–106** — so you see the
project's central claim at a glance:

> **Where does the page number come from? Not the PDF page order — the number printed on the page.**

The sample is generated on the fly; it contains no real book content.

### Step 1 — Verify the toolbox itself

```bash
python run_selftests.py
```

This also builds a minimal project skeleton in a temp directory, so it is fully decoupled from how
your own project is laid out. 8 tools, ~186 assertions. All green before you go further.

### Step 2 — Install dependencies

```bash
# Minimal: docx header route + audit tools
pip install python-docx

# Full: add the OCR route
pip install pymupdf rapidocr_onnxruntime numpy pillow
```

### Step 3 — Configure your project

```bash
# Recommended: drop a 项目布局.json in your project root
cp 项目布局.json.example <your-project>/项目布局.json   # then edit the paths

# Or use an environment variable
setx LITDB_PROJECT "<your-project>"

# Verify how paths resolve (unresolved ones explain why)
python tools/_paths.py
```

> ⚠️ Running tools unconfigured raises `PathError`; some tools fail even on `--help`. This is
> deliberate — **fail loudly rather than guess a path**. See *Known limitations*.

### Step 4 — Follow the SOP

Read [`docs/SOP.md`](docs/SOP.md) first (currently Chinese): the twelve stages, the acceptance gate
for each, failure modes, the eight operating rules, and a checklist.

---

## Non-obvious findings

All measured, all in the SOP — and they are the reason this pipeline exists.

1. **"Not ingested" ≠ "missing".** Two documents once looked like gaps; they were pure images
   (only 19 and 39 characters extractable). Check `status` before calling something missing.

2. **Match quotation marks across the whole text, never in a sliding window.** Straight quotes are
   symmetric; an odd count inside a window shifts the whole pairing by one — and you silently get
   the text *before* the quotation. Nothing errors; you just get the wrong page.

3. **Never strip ellipses as noise.** Papers use "…" to skip words; deleting it splices the two
   halves into one sentence that will never match. Split on the ellipsis, locate each part, then
   check whether the parts share a page.

4. **When the engine suggests "rebuild the database", do not comply.** Measured: page-anchor
   coverage silently drops from ~97% to ~23%; two back-fill passes are needed to recover (~97%).
   See [`docs/SOP.md`](docs/SOP.md) §12.

5. **Collation almost always finds errors.** A real run surfaced 1–2 substantive misreadings or
   misattributions — which is exactly why verbatim collation and cross-validation are separate
   stages, not evidence that the pipeline failed.

---

## Tooling

All 44 modules live flat in `tools/` — they import each other by bare module name, so splitting
them into subdirectories breaks imports. Full index: [`tools/README.md`](tools/README.md)
(Chinese).

| Stage | Representative tools | Engine needed |
| --- | --- | --- |
| Portability layer | `_paths.py` (the single path-resolution entry point) | no |
| Intake / layout probe | `_pdf_scan_check.py`, `_ocr_layout.py` | no |
| Transcription | `_ocr_pdf_to_md.py`, `_docx_pages_index.py`, `_docx_pages_to_md.py`, `_ocr_md_to_pages.py` | no |
| Page calibration | `_page_number.py` (voting offset + structural validation) | no |
| Write guard | `_kb_guard.py` (backup → transaction → post-write validation) | yes |
| Audit | `_kb_audit2/3.py`, `_kb_coverage_vs_files.py`, `_kb_meta_gaps.py` | yes |
| Citation | `_cite_page_candidates.py`, `_quote_collation.py`, `_crosscheck_pages.py`, `_insert_page_numbers.py` | partly |
| Interview branch | `_interview_check.py`, `_interview_norm.py`, `_interview_verify.py` | partly |

---

## Optional: retrieval engine

Needed only for cross-volume search, citation location and page back-fill. This project drives
**DSH (DeepSeek Harness)'s knowledge-base plugin `dsh-kb-rag`** over a small JSON-over-stdin
protocol:

- engine file: `<DSH profile>/node_modules/dsh-kb-rag/kb_engine.py`
- tools: `kb_ingest` / `kb_search` / `kb_stats` / `kb_rag` / `kb_dedup` / `kb_fetch`
- **fallback channel**: `tools/kb_cli.py` drives the engine straight from the command line
  (`stats` / `ingest` / `search` / `rag`)

> **The interface is replaceable.** L3 only requires a backend that can chunk on page markers,
> store a page field, and filter by `section`. Swap in another vector store or full-text index and
> L1/L2 and L4/L5 stay untouched — only the protocol adapter changes.

---

## Known limitations

1. **17 of the 44 tools cannot run without a configured project** (measured: `--help` works for
   27, blocked for 17). Path resolution is now **lazy** (`LazyPath` — resolved on first use), so
   calls like `--help` no longer trigger it; the remaining blocked ones are audit tools that read
   the database **at module level** — they genuinely cannot work without one. Configure first
   (Step 3) or just run `python run_selftests.py`.

2. **Most tools are bound to a project structure** and are not drop-in general-purpose components.
   Reuse means *reusing the flow and the reasoning, plus parameterising scripts as needed*.
   Grading rationale in [`docs/SOP.md`](docs/SOP.md) §5.

3. **Page offsets differ per volume** and must be proven per volume (criterion: the hit page really
   prints the deduced number, and the differences cluster rather than scatter). **Never copy an
   offset value from any example.**

4. **Rebuilding silently destroys page anchors** (see finding 4 above).

---

## Data and privacy

**This repository contains process and tooling only — no literature content and no research data.**

- `示例文集` / `示例人物` / `topic-a` etc. in the docs are **placeholders**.
- Every "quotation + page number" fixture has been moved to **external JSON** (`*_cases.json`);
  no real quotations ship here.
- Measured figures (document counts, chunk counts, volume counts, per-volume offsets) appear only
  as order-of-magnitude ranges.

**If you use this yourself:**

- `.gitignore` already excludes `.kb/`, source directories, bibliography CSVs and manuscripts —
  **do not commit them.** Scans and OCR text often carry copyright; unpublished papers and
  interview transcripts carry privacy.
- **Interview material must be de-identified *before* ingestion**: retrieved passages become part
  of a cloud model's prompt, so this toolchain is not closed local processing.
  `_interview_check.py` detects phone numbers and emails and warns.

---

## License

[MIT](LICENSE) — free for research, teaching and commercial use; just keep the copyright notice.
