# Progress

Updated after every step. Requirement IDs refer to the spec (E1–E12). How the pieces fit
together is in [ARCHITECTURE.md](ARCHITECTURE.md).

_Last updated: 2026-10-22. Session 1 of 5 is built and waiting for your review._

## 1. Done, by requirement

| ID | Requirement | Status | Where | Notes |
|---|---|---|---|---|
| E1 | Load config | ✅ done | `config.py` | Pydantic, one readable line per bad field, refuses secret-looking keys. `allowed_currencies` takes **any ISO 4217 code** (default USD, GBP, EUR); `currency_symbols` adds or remaps symbols per client (e.g. `"$": AUD`). Supplier hints must use an allowed currency. Folders resolve relative to the config file. |
| E2 | Find documents | ⏳ session 2 | | |
| E3 | Extract | ⏳ session 2 | | `ExtractedInvoice` (what Claude fills) is ready in `models.py`. |
| E4 | Escalate hard cases | ⏳ session 3 | | Models configured: Haiku 5.5 → Sonnet 5.5. |
| E5 | Validate | ⏳ session 3 | | Amount parsing it relies on is done (`money.py`). |
| E6 | Detect duplicates | ⏳ session 3 | | Demo set has both kinds (same supplier + number; identical file). |
| E7 | Confidence and review list | ⏳ session 3 | | `Status` enum ready; an `Invoice` defaults to `review`, never `ok`. |
| E8 | Export | ⏳ session 4 | | |
| E9 | Move files | ⏳ session 4 | | |
| E10 | Review page | ⏳ session 5 | | |
| E11 | CLI | 🟡 shell | `cli.py` | `check-config` and `--version` work. `run [--dry-run]`, `show`, `cost --estimate` load the config, then say which session builds them (exit 2). |
| E12 | Run report | ⏳ session 4 | | |

**Also built:**

- `models.py`: `LineItem`, `ExtractedInvoice` (only printed fields, so it's the API schema), `Invoice` (adds source file + hash, status, reasons, model). Money is `Decimal`; missing values are `None`, never 0.
- `money.py`: `parse_amount` reads "1,299.00", "1.299,00", "£51.77", "(12.00)", "12.00-", "CHF 1'299.00", "1,29,999.00"; `normalise_currency` maps symbols/codes to ISO, with longer symbols first ("A$" before "$") and an ISO code in the text beating a symbol.
- `demo/generate.py`: 45 fake PDFs + `answers.json` (see below). Byte-identical output for the same seed and currencies.
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
| 1 | Skeleton, E1, models, E11 shell, CI, demo generator + answer key | ✅ awaiting review | `0d510ae` … this commit |

## 2. Test results

Run on 2026-10-22 with `uv run pytest --cov=ibis_docs`:

| Suite | Tests | Result |
|---|---|---|
| `test_money.py` | 66 | ✅ pass |
| `test_models.py` | 13 | ✅ pass |
| `test_config.py` | 30 | ✅ pass |
| `test_cli.py` | 10 | ✅ pass |
| `test_demo_set.py` | 97 | ✅ pass |
| **Total** | **216** | **✅ all pass** |

Coverage 99% (`config` 99%, `models` 99%, `money` 98%, `cli` 100%); target 90%. `ruff check` and
`ruff format --check` clean. No test calls the Claude API; `ANTHROPIC_API_KEY` is removed from
the environment for every test.

## 3. Next

Session 2: discovery (E2), extraction with `messages.parse()` (E3), recorded fixtures. Before
writing the Claude calls, re-read the current docs on PDF document blocks and structured
outputs (Decimal fields in the JSON schema, and whether to have Claude return amounts as
printed strings that `parse_amount` reads).

## 4. Blocked / needs you

- Review the session 1 checklist (below).
- Session 2's live run needs an `ANTHROPIC_API_KEY` in `.env` with a few dollars of credit.

### Session 1 review checklist

1. `uv run python demo/generate.py --contact-sheet`, then open `demo/contact-sheet.png`: do the layouts look like real supplier invoices?
2. Open a few PDFs in `demo/invoices/` and compare with `demo/answers.json`.
3. `cp config.example.yaml config.yaml && uv run ibis check-config`, then break it on purpose (e.g. `allowed_currencies: [EURO]`) and read the error.
4. Try `--currencies USD,AUD,NZD` in a temp folder: `uv run python demo/generate.py --currencies USD,AUD,NZD --out /tmp/ibis-demo`.
