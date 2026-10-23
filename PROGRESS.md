# Progress

Updated after every step. Requirement IDs refer to the spec (E1–E12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-23. All five sessions are built. Three acceptance items are yours to tick by hand (section 2), and the repo is still private._

## 1. Done, by requirement

| ID | Requirement | Status | Where | Notes |
|---|---|---|---|---|
| E1 | Load config | ✅ done | `config.py` | Pydantic, one readable line per bad field, refuses secret-looking keys. `allowed_currencies` takes **any ISO 4217 code** (default USD, GBP, EUR); `currency_symbols` adds or remaps symbols per client (e.g. `"$": AUD`). Supplier hints: `currency`, `notes` (sent to Claude), and new in session 3 `decimal_mark` (`"."` or `","`) and `date_order` (`day_first` / `month_first`). Folders resolve relative to the config file. |
| E2 | Find documents | ✅ done | `discover.py` | Skips with a plain reason: empty, not a PDF (by content, not extension), password-protected, unreadable, no pages, too many pages, too large for the API. SHA-256 per file; files from earlier runs skipped even when renamed (`output/.ibis/seen.json`, names and hashes only). An identical file in the same run is kept and marked `duplicate_of`. |
| E3 | Extract | ✅ done | `extract.py`, `dates.py`, `money.py` | `messages.parse()` with a base64 PDF block and `RawInvoice`: Claude copies amounts, dates, currency, tax ID and country **as printed text**; Python reads them. The document's own decimal mark settles `12,125`; tax ID labels ("EIN", "VAT No.") are stripped in Python; discounts are stored as the amount taken off. Prompt forbids guessing, computing or converting. |
| E4 | Escalate hard cases | ✅ done | `pipeline.py`, `validate.RETRY_CODES` | One retry on Sonnet 5.5 when the first read failed or failed a check a misread could explain (maths, missing or unreadable fields, impossible dates). Not for duplicates, a currency not allowed, or an unsettled date order, which any model reads the same. The read that passes wins; if neither passes, Sonnet's is kept and any disagreement is a reason. The retry uses the server-side refusal fallback (`fallbacks: "default"`); the model that actually answered is recorded. |
| E5 | Validate | ✅ done | `validate.py` | Line maths, lines vs subtotal, subtotal + tax − discount + shipping = total (lines stand in for a missing subtotal), required fields by document type, due date before invoice date, invoice date in the future, currency not allowed, credit note with a positive total. Tolerance from config. |
| E6 | Detect duplicates | ✅ done | `pipeline.py`, `memory.py` | Same supplier + invoice number (case and spaces ignored), within a run and against earlier runs (`output/.ibis/invoices.json`); identical file, with no API call. The later document is flagged, naming the earlier one. |
| E7 | Confidence and review list | ✅ done | `validate.py`, `models.Reason` | `ok` only with no reasons; `review` with one or more; `failed` when no read came back. Each reason has a code (for tests and exports) and a plain message with the amounts: "the lines add up to £3,115.76, but the subtotal is £3,128.16 (£12.40 more)". |
| E8 | Export | ✅ done | `export.py`, `results.py` | `invoices.csv`, `line_items.csv`, `ibis.xlsx` (Invoices, Line items, Needs review) and optionally a Google Sheet, all built from the same rows. **Cumulative:** every result is kept in `output/results/` and each run rebuilds the exports from all of them. CSV keeps amounts exact (UTF-8 with BOM so Excel shows £ €); Excel has real numbers and dates, a frozen green header, amber review rows. Invoice text can never run as a formula. Writes are atomic. |
| E9 | Move files | ✅ done | `files.py`, `run.py` | After the exports are written: `ok` → `inbox/processed/`, `review`/`failed` → `inbox/needs-review/`; taken names get `-2`; skipped files stay. `move_files: false` turns it off. |
| E10 | Review page | ✅ done | `review.py`, `review_app.py`, `ibis review` | Streamlit, on `localhost` only, usage statistics off. The PDF (rendered locally) beside its fields and line items; reasons at the top; checks re-run as you edit. **Approve** saves `ok`, moves the PDF to `processed/` and rebuilds every export; with checks still failing it needs a note. **Discard** (after a tick) takes a document out of the sheets and moves the PDF to `needs-review/discarded/`; nothing is deleted. Every decision is kept on the result (what changed, from what, when, the note) and shown in the sheets' **Reviewed** column. |
| E11 | CLI | ✅ done | `cli.py` | `check-config`, `--version`, `show <file>`, `run` (read, check, save, export, move, remember, report), `run --dry-run` (read and check only; costs API money), `cost --estimate <folder>` (input tokens counted exactly with the free token counter; shown as a range: no retries / 10% retried). |
| E12 | Run report | ✅ done | `report.py` | Printed and saved to `output/reports/run-<time>.json`: documents, ok/review/failed, skipped with reasons, tokens and cost per model, files moved, outputs, time. File names and counts only, never invoice contents. |

**Also built:**

- `models.py`: `RawInvoice` (what Claude fills: all text, `""` for not printed), `ExtractedInvoice` (typed: `Decimal`, `date`, ISO code), `Invoice` (adds source file + hash, status, reasons, model), `Reason`.
- `money.py`: `parse_amount`, `infer_decimal_mark`, `is_ambiguous_amount`, `format_amount`, `normalise_currency`.
- `dates.py`: `parse_date` and `resolve_order` (see "Ambiguous dates" below).
- `pipeline.py`: discover → read (4 in parallel) → learn date orders → check → retry → duplicates.
- `memory.py`: what's kept between runs in `output/.ibis/` (invoice keys and supplier date orders; never amounts or contents). Written by `ibis run` in session 4; the dry run only reads it.
- `run.py`: keeps a run in a safe order: results, exports, then moves, then memory. A failed document isn't remembered, so dropping it in again retries it.
- `scripts/accuracy.py`: the whole demo set through the live pipeline, scored per field, with the actual cost next to the estimate.
- `accuracy.py`: scores extracted fields against `answers.json` (text compared ignoring spacing and case).
- `scripts/record_fixtures.py`: runs the pipeline live on the fixture set, saves every response to `tests/fixtures/`, prints statuses, reasons, accuracy and cost.
- CI (`.github/workflows/ci.yml`): ruff check + format, `check-config` on the example, pytest with 90% coverage gate, gitleaks over full history. Dependabot weekly for uv and actions.

### Ambiguous dates (decided in session 3)

A numeric date like `03/09/2026` could be 3 Sep or 9 Mar. Only noting it would let a date months
out reach the sheet as checked, so **a date whose order is still unknown sends the document to
review**, with both readings in the reason. To keep that rare, the order is settled first by:

1. A part over 12 on any of the document's dates (`13/09/2026`).
2. Ruling out impossible readings: an invoice date more than a month ahead, or a due date before
   the invoice date (`046`: `05/08/2026` due `04/09/2026` only works day first).
3. A hint: the supplier's `date_order` in config, else the order learned from their other
   documents (in this run, or remembered from earlier ones), else their printed country (Canada
   writes both ways, so it doesn't count).

The old fallback (USD means month first, otherwise day first) is gone.

### Ambiguous decimal marks (decided in session 3)

`12,125` is 12.125 or 12125 depending on the document. The document's own amounts settle it
(`1.299,00` says the comma is the decimal), or the supplier's `decimal_mark` in config. If neither
does: in currencies with 2 decimals the old reading stands (a misread is 1000× off and fails the
maths); in currencies with 3 decimals (KWD, BHD, OMR, JOD, TND, LYD, IQD) it's flagged for review.

### Demo set (`demo/invoices/`, default USD, GBP, EUR)

| Kind | Count | Detail |
|---|---|---|
| Clean invoices | 31 | 6 layouts (classic, modern, us_sales, compact with per-line tax, plain_text printout, euro with `1.299,00 €`); 2 are multi-page (46 lines); some lack PO, due date, shipping; some have discounts; `046` has an ambiguous date its due date settles |
| Packing slips | 3 | No prices; slip number, ship date, quantities |
| Credit notes | 3 | Negative quantities and amounts, reference to the credited invoice |
| Broken | 10 | line maths, lines ≠ subtotal (by 12.40), total off by 100, due before invoice date, no invoice number, CHF (not allowed), resend with COPY stamp, byte-identical copy, blurred tilted scan, `047` ambiguous date nothing settles |

Date formats vary on purpose (ISO, `18 August 2026`, `13/09/2026` UK, `09/20/2026` US, `26.08.2026`), as real suppliers do.

Other currencies: `uv run python demo/generate.py --currencies USD,AUD,NZD` (any ISO code; ones without a built-in style print as `NZD 1,234.00`).

### Session log

| Session | Scope | Status | Commits |
|---|---|---|---|
| 1 | Skeleton, E1, models, E11 shell, CI, demo generator + answer key | ✅ reviewed | `0d510ae` … `a1e88ec` |
| 2 | E2, E3, recorded fixtures, `show`, `run --dry-run` | ✅ done | `c5d7136` … `82439b8` |
| 3 | E4, E5, E6, E7, ambiguous dates and decimal marks, full dry run | ✅ done | `9357af8` … `08467f6` |
| 4 | E8, E9, E12, full `run`, `cost --estimate`, accuracy run | ✅ reviewed | `dbd0fbc` … `26d4eca` |
| 5 | E10 review page, README, demo, acceptance checklist, secrets scan | ✅ awaiting your checks | `3f2b299` … this commit |

Repo: public at github.com/yahani/ibis since 2026-10-23, all rights reserved (no licence).

## 2. Acceptance checklist (the spec's, with the evidence)

✅ = met and shown below. ☐ = yours to do by hand (the spec says "you tick boxes after checking
them yourself"); where I ran it too, the result is noted.

**Automated tests (pytest, no live API calls)**

- ✅ Validation catches line maths off, lines not summing to subtotal, total mismatch, due date
  before invoice date, unknown currency, missing invoice number: `test_validate.py`.
- ✅ Decimal parsing handles "1,299.00", "1.299,00", "£51.77" and negative credit-note amounts:
  `test_money.py`, `test_extract.py`.
- ✅ Duplicate detection by supplier + number and by file hash, in a run and across runs:
  `test_pipeline.py`, `test_run.py`, `test_review.py`.
- ✅ Escalation: a failing Haiku result triggers one Sonnet retry, the passing result wins, the
  model is recorded: `test_pipeline.py`, `test_recorded.py`.
- ✅ CSV, Excel (three sheets) and the Sheets payload match the same data, cell by cell:
  `test_export.py::test_all_outputs_carry_the_same_data`.
- ✅ Encrypted, empty and non-PDF files are skipped with a reason and don't stop the run:
  `test_discover.py`.
- ✅ Coverage 90% or more on validate (100%), models (99%), export (100%), config (99%); 99% overall.

**Accuracy on the demo set (live)**

- ✅ At least 95% of header fields across 30+ invoices: **609/611 (99.7%) on 47**.
- ✅ Every broken invoice on the review list with the right reason, none `ok`: **9/9**.
- ✅ No invented values: **0**.

**End to end (by hand)**

- ✅ Drop 10 PDFs in `inbox/`, run once, get the Excel file, the review list, files moved.
  *Run by hand with `scripts/demo.sh` (2026-10-23); also in a fresh clone (9 ok, 1 flagged).*
- ☐ Running again processes nothing new. *Not `scripts/demo.sh`, which starts fresh: after it, run `uv run ibis run -c demo-workspace/config.yaml`, then copy the same PDFs back into `demo-workspace/inbox/` and run again. Both runs should read nothing (I did it on a copy: "No new PDFs", then 10 skipped as "already processed", $0).*
- ✅ Review page: open a flagged invoice, fix a field, approve, re-export shows the fix. *Clicked
  through in a browser with the demo (2026-10-23); also headless in `test_review.py`.*
- ✅ `ibis cost --estimate` within 25% of actual: +13% on the whole demo set; on a clean batch the
  low end of the range was +13%.
- ☐ Fresh clone + README gets a stranger running in under 15 minutes. *Followed the README in a
  fresh clone: about 30 s with a warm cache; `uv sync` takes 51 s from cold. Creating the API key
  is the slowest step. A second person is the real test.*
- ✅ No API key or real document anywhere in the history: gitleaks over all 26 commits found
  nothing; no `.env`, `config.yaml`, key file, `inbox/` or `output/` was ever committed; every one
  of the 47 PDFs ever committed is one `demo/generate.py` makes. (Commit authors were rewritten
  from a local machine address to the GitHub account's email before going public.)

## 3. Test results

Run on 2026-10-23 with `uv run pytest --cov=ibis_docs`: **452 tests, all pass**, coverage 99%
(every module 96% or more), `ruff check` and `ruff format --check` clean. No test calls the
Claude API. `tests/fakes.py` runs the real SDK client over a mock HTTP transport; `test_recorded.py`
replays 21 real responses through the whole pipeline; `test_review.py` drives the review page
headless with Streamlit's test runner.

| Suite | Tests | | Suite | Tests |
|---|---|---|---|---|
| `test_money.py` | 78 | | `test_pipeline.py` | 20 |
| `test_models.py` | 14 | | `test_memory.py` | 2 |
| `test_config.py` | 30 | | `test_recorded.py` | 13 |
| `test_cli.py` | 30 | | `test_export.py` | 15 |
| `test_demo_set.py` | 105 | | `test_run.py` | 16 |
| `test_dates.py` | 30 | | `test_report.py` | 8 |
| `test_discover.py` | 12 | | `test_review.py` | 17 |
| `test_extract.py` | 40 | | `test_validate.py` | 22 |

### Session 5, found by running it for real

- `ibis review`: on first launch Streamlit asks for an email in the terminal and never serves the
  page. Now it runs headless and Ibis opens the browser once the page answers.
- Coverage of the page wasn't being measured (it ran as a script); the tests now run the root
  launcher, as `ibis review` does.

### Accuracy run (session 4), whole demo set, 2026-10-23

`uv run python scripts/accuracy.py`: all 47 documents through the live pipeline.

| | Default effort | `--effort low` |
|---|---|---|
| Header fields right | **609/611 (99.7%)**, target 95% | 609/611 (99.7%) |
| Per field | 100% on 11 of 13 fields; `supplier_tax_id` and `invoice_date` 46/47 | same |
| Line items exactly right | **289/289** | 286/289 |
| Expected status | **47/47** | 47/47 |
| Broken documents flagged with the right reason | **9/9** | 9/9 |
| Invented values | **0** | 0 |
| Retried on Sonnet 5.5 | 5 (all broken documents) | 5 |
| Cost | **$0.1117** (Haiku $0.045, Sonnet $0.067) | $0.1098 |
| `cost --estimate` said | $0.1257 (+13%; target within 25%) | +14% |
| Time | 64 s | 58 s |

The two misses are by design: `044`'s tax ID is too blurred to read and was left empty, and
`047`'s date can't be settled and goes to review. **Low effort is not worth it**: it saved $0.002
on 47 documents and got 3 line items wrong, so the model default stays. The Sonnet retries are
60% of the cost here because 10 of the 47 demo documents are broken on purpose; on ordinary
invoices a document costs about $0.001.

### Google Sheet (session 4), live

The end-to-end run's 11 results written to a test spreadsheet with the service account from the
price tracker (`GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`). Read back: the three tabs created beside
the existing one, amounts stored as numbers (credit note total `-1643.11`), dates as real dates,
reasons in place. Formatted like the Excel file (frozen green header, `1,812.86` money, `yyyy-mm-dd`
dates, amber review rows, columns sized to fit) in one batched request; each export resets the old
formatting first, so a row that's fixed loses its amber. Worksheets Ibis doesn't own are left alone.
For client setups, give each client their own service account and sheet.

### End to end (session 4), `ibis run` on 11 demo PDFs and a text file

9 `ok` moved to `processed/`, 2 flagged (CHF, identical copy) moved to `needs-review/`, the text
file skipped and left in place; `invoices.csv`, `line_items.csv`, `ibis.xlsx` and a run report
written; a second `ibis run` found nothing new. Cost $0.0091 (Haiku only, no retries needed);
the estimate said $0.0103 without retries (+13%), and is now shown as a range because a single
figure with 10% retries assumed ($0.026) overstates small clean batches.

### Live run (session 3), 2026-10-22

`uv run python scripts/record_fixtures.py`: 17 demo files (variety, a discount, every broken
document, the ambiguous dates), run date fixed at 2026-10-22.

| Result | |
|---|---|
| Expected status | **17/17** (044, the blurred scan, came out `ok` and correct apart from the tax ID it left empty) |
| Broken documents caught with the expected reason | **9/9**, none `ok` |
| Header fields | **217/221 (98.2%)** before the two fixes below; after them, the only misses are by design (044's unreadable tax ID, 047's unsettled date) |
| Retries | 5, all on broken documents (037–041); Sonnet read each the same way, confirming the document, not the read, is wrong |
| Calls and cost | 16 Haiku + 5 Sonnet; Haiku 72,555 in / 17,726 out, Sonnet 22,016 in / 2,273 out; **about $0.08** |

What it found, and what changed:

- `025`: Haiku copied the tax ID with its label ("EIN 36-6106824"). Asking for the number alone
  made it drop the EIN entirely, so the label is now stripped in Python instead (`strip_tax_label`),
  the same on every model.
- `041`: the plain-text layout prints the supplier in capitals and Claude copied it as printed,
  which is right. Scoring now ignores letter case; duplicate matching already did.
- The recording script filed 001's response under 045's name (identical bytes). Fixed.

Session 2 live run (extraction only, 5 invoices): 65/65 header fields, every line exact, $0.0067.

## 4. Next

**Decided before going public (2026-10-23):** commit authors rewritten to the GitHub account's
email (force-push while private); no licence, so all rights reserved; repo made public.

**Portfolio:** the one-page case study is drafted (a private page; share it from its Share menu),
and the Upwork / Fiverr listing text is drafted (kept out of this repo because it has prices).
Still to do: record the 60-second video from `docs/DEMO.md`, and link the repo from the case
study.

Tuning, if cost matters later: the Sonnet retry is most of the spend on broken documents and
always confirmed the first read on the demo set. Try `effort: low` for the retry model only, or
skip the retry for checks that a confirmed misread has never explained.

Later, when a client needs it:

- **Non-English invoices.** Claude reads them, and amounts and numeric dates already parse, but
  `dates.py` only knows English month names (`18 août 2026` goes to review as unreadable), not
  `2026年8月18日`, and `parse_amount` expects 0–9 digits. Needs month names for the main European
  languages, those date forms, non-ASCII digits, and 6–10 non-English demo invoices with an
  accuracy run before claiming support. About half a session. Version 1 stays English, per the spec.
- **Workload Identity Federation** for client setups that run on a schedule (their cloud or GitHub
  Actions): short-lived tokens instead of a stored API key. The SDK picks it up from environment
  variables and `extract.make_client()` uses the default client, so no code change. Not useful on
  a laptop.

## 5. Needs you

1. The two ☐ items left in section 2: run twice and see nothing new; a stranger follows the README.
2. The portfolio steps in section 4: the video, and sharing the case study.
