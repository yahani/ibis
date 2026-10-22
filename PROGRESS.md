# Progress

Updated after every step. Requirement IDs refer to the spec (E1–E12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-22. Session 2 of 5 is built and waiting for your review._

## 1. Done, by requirement

| ID | Requirement | Status | Where | Notes |
|---|---|---|---|---|
| E1 | Load config | ✅ done | `config.py` | Pydantic, one readable line per bad field, refuses secret-looking keys. `allowed_currencies` takes **any ISO 4217 code** (default USD, GBP, EUR); `currency_symbols` adds or remaps symbols per client (e.g. `"$": AUD`). Supplier hints must use an allowed currency. Folders resolve relative to the config file. |
| E2 | Find documents | ✅ done | `discover.py` | Skips with a plain reason: empty, not a PDF (by content, not extension), password-protected, unreadable, no pages, too many pages, too large for the API. SHA-256 per file; files from earlier runs skipped even when renamed (`output/.ibis/seen.json`, names and hashes only). An identical file in the same run is kept and marked `duplicate_of` for E6. Hidden files and `processed/`, `needs-review/` ignored. |
| E3 | Extract | ✅ done | `extract.py`, `dates.py` | `messages.parse()` with a base64 PDF block and `RawInvoice`: Claude copies amounts, dates and currency **as printed text**; Python reads them (`parse_amount`, `parse_date`, `normalise_currency`). Unreadable values become empty plus an issue. Prompt forbids guessing, computing or converting. Errors with one file don't stop a run; no key / bad key / unknown model does, with a readable message. Model and tokens recorded per file. |
| E4 | Escalate hard cases | ⏳ session 3 | | Models configured: Haiku 5.5 → Sonnet 5.5. |
| E5 | Validate | ⏳ session 3 | | Amount parsing it relies on is done (`money.py`). |
| E6 | Detect duplicates | ⏳ session 3 | | Demo set has both kinds (same supplier + number; identical file). |
| E7 | Confidence and review list | ⏳ session 3 | | `Status` enum ready; an `Invoice` defaults to `review`, never `ok`. |
| E8 | Export | ⏳ session 4 | | |
| E9 | Move files | ⏳ session 4 | | |
| E10 | Review page | ⏳ session 5 | | |
| E11 | CLI | 🟡 partly | `cli.py` | Working: `check-config`, `--version`, `show <file>` (extracts and prints, saves nothing), `run --dry-run` (lists what would be processed or skipped; extraction joins it in session 3). `run` and `cost --estimate` say which session builds them (exit 2). |
| E12 | Run report | ⏳ session 4 | | |

**Also built:**

- `models.py`: `LineItem`, `ExtractedInvoice` (only printed fields, so it's the API schema), `Invoice` (adds source file + hash, status, reasons, model). Money is `Decimal`; missing values are `None`, never 0.
- `money.py`: `parse_amount` reads "1,299.00", "1.299,00", "£51.77", "(12.00)", "12.00-", "CHF 1'299.00", "1,29,999.00"; `normalise_currency` maps symbols/codes to ISO, with longer symbols first ("A$" before "$") and an ISO code in the text beating a symbol.
- `demo/generate.py`: 45 fake PDFs + `answers.json` (see below). Byte-identical output for the same seed and currencies.
- `accuracy.py`: scores extracted fields against `answers.json` (used by the recording script now, the 95% accuracy run in session 4).
- `scripts/record_fixtures.py`: live run on chosen demo invoices; saves response bodies to `tests/fixtures/` and prints per-field accuracy, tokens and cost.
- CI (`.github/workflows/ci.yml`): ruff check + format, `check-config` on the example, pytest with 90% coverage gate, gitleaks over full history. Dependabot weekly for uv and actions.

### Demo set (`demo/invoices/`, default USD, GBP, EUR)

| Kind | Count | Detail |
|---|---|---|
| Clean invoices | 30 | 6 layouts (classic, modern, us_sales, compact with per-line tax, plain_text printout, euro with `1.299,00 €`); 2 are multi-page (46 lines); some lack PO, due date, shipping; some have discounts |
| Packing slips | 3 | No prices; slip number, ship date, quantities |
| Credit notes | 3 | Negative quantities and amounts, reference to the credited invoice |
| Broken | 9 | line maths, lines ≠ subtotal (by 12.40), total off by 100, due before invoice date, no invoice number, CHF (not allowed), resend with COPY stamp, byte-identical copy, blurred tilted scan |

Date formats vary on purpose (ISO, `18 August 2026`, `13/09/2026` UK, `09/20/2026` US, `26.08.2026`), as real suppliers do.

Other currencies: `uv run python demo/generate.py --currencies USD,AUD,NZD` (any ISO code; ones without a built-in style print as `NZD 1,234.00`).

### Session log

| Session | Scope | Status | Commits |
|---|---|---|---|
| 1 | Skeleton, E1, models, E11 shell, CI, demo generator + answer key | ✅ reviewed | `0d510ae` … `a1e88ec` |
| 2 | E2, E3, recorded fixtures, `show`, `run --dry-run` | ✅ awaiting review | `c5d7136` … this commit |

Repo: private at github.com/yahani/ibis (make public in session 5, after the secrets scan). CI green on run 39787544493. The first push's gitleaks job failed on a known first-push quirk (it asks for the root commit's parent); a local `gitleaks git` over the full history found no leaks.

## 2. Test results

Run on 2026-10-22 with `uv run pytest --cov=ibis_docs`:

| Suite | Tests | Result |
|---|---|---|
| `test_money.py` | 62 | ✅ pass |
| `test_models.py` | 13 | ✅ pass |
| `test_config.py` | 30 | ✅ pass |
| `test_cli.py` | 17 | ✅ pass |
| `test_demo_set.py` | 101 | ✅ pass |
| `test_dates.py` | 25 | ✅ pass |
| `test_discover.py` | 12 | ✅ pass |
| `test_extract.py` | 25 | ✅ pass |
| `test_recorded.py` | 10 | ✅ pass |
| **Total** | **295** | **✅ all pass** |

Coverage 99% (`config` 99%, `models` 99%, `money` 98%, `cli` 100%, `dates` 100%, `discover` 100%,
`extract` 99%, `accuracy` 97%); target 90%. `ruff check` and `ruff format --check` clean.

No test calls the Claude API. `tests/fakes.py` runs the real SDK client over a mock HTTP transport,
so the request (PDF block, schema, prompt) and the SDK's parsing and error mapping are tested
without a network. `ANTHROPIC_API_KEY` is removed and `.env` loading is disabled for every test.

### Live run (session 2), Haiku 5.5, 2026-10-22

`uv run python scripts/record_fixtures.py` on 5 invoices chosen for variety:

| File | What it covers | Header fields | Line items |
|---|---|---|---|
| `001-inv-classic` | GBP, "18 August 2026", SKU inside description | 13/13 | 4/4 exact |
| `003-inv-us_sales` | USD, `09/25/2026`, no tax ID printed, "Net 30" | 13/13 | 5/5 exact |
| `004-credit-compact` | credit note, negative amounts, per-line tax | 13/13 | 4/4 exact |
| `006-inv-euro` | two pages, 46 lines, `1.299,00 €`, `26.08.2026` | 13/13 | 46/46 exact |
| `025-slip-classic` | packing slip, no prices | 13/13 | 8/8 exact |

**65/65 header fields (100%)**, every line field exact. 24,342 input + 8,576 output tokens, about
**$0.0067** in all, so about $0.0008 for a one-page invoice (spec estimate: $0.001). About 8–20 s
per document. Also `ibis show` on the blurred, tilted scan (`044`): everything right except the
tax ID, which it left empty rather than guess.

What the first live run found, and what changed:

- **The API rejected the first schema as "too complex"**: 18 optional `string | null` fields.
  Now every `RawInvoice` field is a required plain string, with `""` for "not printed"; a test
  keeps it that way.
- **Answer key bug:** the US layout doesn't print the supplier's tax ID, but `answers.json` had
  one for 5 documents. Fixed in `demo/generate.py`; no PDF changed. Claude had it right.
- The packing slip's "Ship date" wasn't taken as its date, and SKUs printed inside descriptions
  were kept in the description. Both fixed with clearer schema descriptions.

## 3. Next

Session 3: validation (E5), duplicates (E6), status and reasons (E7), escalation to Sonnet 5.5
(E4). Inputs already in place: `Extraction.issues` (values that couldn't be read, ambiguous
dates), `Found.duplicate_of` (identical files, no API call needed), and the 9 broken demo
invoices with their expected reasons in `answers.json`. Decide whether an assumed date order
(`issues`) sends a document to review or is only noted.

Session 3, ambiguous decimal marks (E5). `parse_amount` handles comma decimals (`1.299,00`,
`1 299,00 €`, `€ 51,77`), but one mark followed by exactly three digits is always read as a
thousands separator: `12,125` → 12125 (a 3-decimal unit price) and `1.250 KWD` → 1250 (should be
1.250). Validation would catch most of these through the line maths, but fix them at the source:

- Use the invoice currency: for 3-decimal currencies (KWD, BHD, OMR, JOD, TND, LYD, IQD) read
  `x.xxx` as a decimal.
- Optional per-supplier hint in config: `decimal_mark: ","`.
- If an amount is still ambiguous, flag the invoice for review with a plain reason
  ("amount '12,125' could be 12.125 or 12125") instead of picking one.

Later, for client setups: **Workload Identity Federation**. A scheduled job in a client's cloud or
GitHub Actions can call the API with short-lived tokens from its identity provider instead of a
stored API key. The SDK picks it up from environment variables, and `extract.make_client()` uses
the default client, so it needs no code change. Not useful on a laptop; use an API key in `.env`.

Tuning to measure in session 4, not before: Haiku 5.5 runs with adaptive thinking at its default
effort (`medium`); about half the output tokens on the packing slip were thinking. Try `low` effort
on the full demo set and keep it only if accuracy holds.

## 4. Blocked / needs you

- Review the session 2 checklist (below).

### Session 2 review checklist

1. **The prompt forbids guessing.** Read `SYSTEM_PROMPT` in `src/ibis_docs/extract.py` and the
   field descriptions in `RawInvoice` (`src/ibis_docs/models.py`); Claude sees both.
2. **One live run yourself:** `uv run ibis show demo/invoices/006-inv-euro.pdf -c config.example.yaml`
   (two pages, 46 lines) and compare with the PDF. Then try a PDF of your own choosing that isn't
   client data.
3. **Skips read clearly:** put a few demo PDFs, an empty file and a `.txt` in `inbox/`, then
   `uv run ibis run --dry-run`.
4. **Fixtures hold nothing sensitive:** open one file in `tests/fixtures/`. It's the API's response
   body only (fake invoice data, token counts), never the request or your key.
