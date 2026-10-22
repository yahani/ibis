# Progress

Updated after every step. Requirement IDs refer to the spec (E1–E12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-22. Session 3 of 5 is built and waiting for your review._

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
| E8 | Export | ⏳ session 4 | | |
| E9 | Move files | ⏳ session 4 | | |
| E10 | Review page | ⏳ session 5 | | |
| E11 | CLI | 🟡 partly | `cli.py` | Working: `check-config`, `--version`, `show <file>` (fields and checks, saves nothing), `run --dry-run` (reads and checks everything, prints each status and reason and a summary such as "Ibis read 17 documents. 7 checked out. 10 need a look.", writes and moves nothing; **costs API money**). `run` and `cost --estimate` are session 4. |
| E12 | Run report | ⏳ session 4 | | Token counts per model are already collected (`Outcome.tokens()`). |

**Also built:**

- `models.py`: `RawInvoice` (what Claude fills: all text, `""` for not printed), `ExtractedInvoice` (typed: `Decimal`, `date`, ISO code), `Invoice` (adds source file + hash, status, reasons, model), `Reason`.
- `money.py`: `parse_amount`, `infer_decimal_mark`, `is_ambiguous_amount`, `format_amount`, `normalise_currency`.
- `dates.py`: `parse_date` and `resolve_order` (see "Ambiguous dates" below).
- `pipeline.py`: discover → read (4 in parallel) → learn date orders → check → retry → duplicates.
- `memory.py`: what's kept between runs in `output/.ibis/` (invoice keys and supplier date orders; never amounts or contents). Written by `ibis run` in session 4; the dry run only reads it.
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
| 3 | E4, E5, E6, E7, ambiguous dates and decimal marks, full dry run | ✅ awaiting review | `9357af8` … this commit |

Repo: private at github.com/yahani/ibis (make public in session 5, after the secrets scan).

## 2. Test results

Run on 2026-10-22 with `uv run pytest --cov=ibis_docs`:

| Suite | Tests | Result |
|---|---|---|
| `test_money.py` | 78 | ✅ pass |
| `test_models.py` | 13 | ✅ pass |
| `test_config.py` | 30 | ✅ pass |
| `test_cli.py` | 21 | ✅ pass |
| `test_demo_set.py` | 105 | ✅ pass |
| `test_dates.py` | 30 | ✅ pass |
| `test_discover.py` | 12 | ✅ pass |
| `test_extract.py` | 40 | ✅ pass |
| `test_validate.py` | 22 | ✅ pass |
| `test_pipeline.py` | 20 | ✅ pass |
| `test_memory.py` | 2 | ✅ pass |
| `test_recorded.py` | 13 | ✅ pass |
| **Total** | **386** | **✅ all pass** |

Coverage 99% (every module 97% or more); target 90%. `ruff check` and `ruff format --check` clean.

No test calls the Claude API. `tests/fakes.py` runs the real SDK client over a mock HTTP transport
and routes each request by the PDF sent and the model asked, so the parallel pipeline is tested as
it runs. `test_recorded.py` replays 21 real responses through the whole pipeline and checks the
spec's acceptance criteria: expected status for every document, every broken one flagged with the
right reason, only reason-free documents `ok`, no invented values, retries only where they help.

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

Session 4: exports (E8), file moves (E9), run report and cost (E12), full `ibis run`, and the
accuracy run on the whole demo set (target 95% of header fields). `ibis run` also writes
`output/.ibis/` (seen files, invoice keys, learned date orders) so the next run skips and
recognises what it's seen. In the exports, `reasons` become one readable column and their codes
another.

Tuning to measure in session 4, not before: Haiku 5.5 runs with adaptive thinking at its default
effort (`medium`); about half the output tokens on the packing slip were thinking. Try `low` effort
on the full demo set and keep it only if accuracy holds.

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

- Review the session 3 checklist (below).

### Session 3 review checklist

1. **Reasons read clearly.** Run `uv run python scripts/record_fixtures.py 037-inv-classic.pdf
   038-inv-modern.pdf 039-inv-us_sales.pdf 047-inv-classic.pdf` (about $0.05) or read the live run
   above, and judge each reason as a bookkeeper would: is it clear what's wrong and where to look?
2. **Broken invoices are caught.** Open `037`–`047` in `demo/invoices/` next to their reasons.
3. **Try the dry run** on your own `inbox/`: `uv run ibis run --dry-run -c config.example.yaml`.
   It now calls Claude (about $0.001 a document, more for any that are retried).
4. **The retry rule.** `RETRY_CODES` in `src/ibis_docs/validate.py` lists what's worth a second
   read on Sonnet. Anything you'd add or remove?
