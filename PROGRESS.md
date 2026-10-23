# Progress

Updated after every step. Requirement IDs refer to the spec (E1–E12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-23. Session 4 of 5 is built; the Google Sheet needs one live test with your service account._

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
| E8 | Export | ✅ done (Google Sheet: live test pending) | `export.py`, `results.py` | `invoices.csv`, `line_items.csv`, `ibis.xlsx` (Invoices, Line items, Needs review) and optionally a Google Sheet, all built from the same rows. **Cumulative:** every result is kept in `output/results/` and each run rebuilds the exports from all of them. CSV keeps amounts exact (UTF-8 with BOM so Excel shows £ €); Excel has real numbers and dates, a frozen green header, amber review rows. Invoice text can never run as a formula. Writes are atomic. |
| E9 | Move files | ✅ done | `files.py`, `run.py` | After the exports are written: `ok` → `inbox/processed/`, `review`/`failed` → `inbox/needs-review/`; taken names get `-2`; skipped files stay. `move_files: false` turns it off. |
| E10 | Review page | ⏳ session 5 | | |
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
| 4 | E8, E9, E12, full `run`, `cost --estimate`, accuracy run | ✅ awaiting review | `dbd0fbc` … this commit |

Repo: private at github.com/yahani/ibis (make public in session 5, after the secrets scan).

## 2. Test results

Run on 2026-10-23 with `uv run pytest --cov=ibis_docs`:

| Suite | Tests | Result |
|---|---|---|
| `test_money.py` | 78 | ✅ pass |
| `test_models.py` | 14 | ✅ pass |
| `test_config.py` | 30 | ✅ pass |
| `test_cli.py` | 25 | ✅ pass |
| `test_demo_set.py` | 105 | ✅ pass |
| `test_dates.py` | 30 | ✅ pass |
| `test_discover.py` | 12 | ✅ pass |
| `test_extract.py` | 40 | ✅ pass |
| `test_validate.py` | 22 | ✅ pass |
| `test_pipeline.py` | 20 | ✅ pass |
| `test_memory.py` | 2 | ✅ pass |
| `test_recorded.py` | 13 | ✅ pass |
| `test_export.py` | 12 | ✅ pass |
| `test_run.py` | 16 | ✅ pass |
| `test_report.py` | 8 | ✅ pass |
| **Total** | **427** | **✅ all pass** |

Coverage 99% (every module 97% or more); target 90%. `ruff check` and `ruff format --check` clean.

No test calls the Claude API. `tests/fakes.py` runs the real SDK client over a mock HTTP transport
and routes each request by the PDF sent and the model asked. `test_recorded.py` replays 21 real
responses through the whole pipeline and checks the acceptance criteria. `test_export.py` checks
that CSV, Excel and the Sheets payload carry the same data, cell by cell.

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

## 3. Next

Session 5: the Streamlit review page (E10: the PDF beside the fields, edit, approve, re-export
from `output/results/`), the README for a stranger to get running in 15 minutes, the demo script,
the end-to-end checklist, and the secrets scan before making the repo public.

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

## 4. Blocked / needs you

- **Google Sheet live test.** Needs the path to your service account's JSON key and a test
  spreadsheet shared (as Editor) with the service account's email. Then set
  `GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`, turn on `outputs.google_sheet` in `config.yaml`, and
  run. For client setups, give each client their own service account.
- Review the session 4 checklist (below).

### Session 4 review checklist

1. **Open the Excel file and check numbers by hand** (the spec's session 4 review). Run
   `uv run ibis run` on a few demo PDFs in `inbox/` (or open the end-to-end run's
   `output/ibis.xlsx`), pick three invoices, and compare totals, dates and line items with the PDFs.
2. **The review list:** the "Needs review" sheet lists each flagged document with its reasons and
   where its PDF now is.
3. **Files moved correctly:** `inbox/processed/` and `inbox/needs-review/`; run again and nothing
   new is processed.
4. **The estimate:** `uv run ibis cost --estimate demo/invoices -c config.example.yaml` (free) and
   compare with the accuracy run's $0.11.
