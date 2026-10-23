# ibis

**Every invoice, read and checked.** Right, or flagged.

Ibis reads supplier invoices, credit notes and packing slips (PDF), pulls out the key fields and
every line item, re-adds every total in Python, and writes a clean spreadsheet. Anything it can't
verify goes on a review list with a plain reason, such as *"the lines add up to £3,115.76, but the
subtotal is £3,128.16 (£12.40 more)"*, instead of being guessed.

```text
$ uv run ibis run
Reading 11 document(s) from inbox ...
  ✓ 001-inv-classic.pdf      ok
  ! 042-inv-classic.pdf      review
      CHF isn't one of your allowed currencies (USD, GBP, EUR)
  ! 045-inv-copy-of-001.pdf  review
      identical file to 001-inv-classic.pdf
  ...
Ibis read 11 documents. 9 checked out. 2 need a look.
  claude-haiku-5-5: 44,065 in / 9,473 out tokens, $0.0091
Moved 9 PDF(s) to processed/.
Moved 2 PDF(s) to needs-review/.
```

**It doesn't guess.** Claude reads each document and copies what's printed, as text. Python reads
the amounts and dates, checks that lines, subtotal, tax, discount, shipping and total add up,
looks for duplicates, and only then calls a document `ok`. A document that fails a check a misread
could explain gets one more read on a stronger model; if it still fails, it goes to review.

## Contents

- [Quick start (about 10 minutes)](#quick-start-about-10-minutes)
- [Everyday use](#everyday-use)
- [What it checks](#what-it-checks)
- [What you get](#what-you-get)
- [The review page](#the-review-page)
- [Configuration](#configuration)
- [Google Sheets](#google-sheets)
- [Accuracy and cost](#accuracy-and-cost)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Licence](#licence)

## Quick start (about 10 minutes)

You need macOS, Linux or Windows, an internet connection, and a Claude API key.

**1. Install uv** (it fetches the right Python for you):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Get the code and install it:**

```bash
git clone https://github.com/yahani/ibis.git
cd ibis
uv sync
```

**3. Add your API key.** Create a key in the [Claude Console](https://platform.claude.com/)
(API keys), add a few dollars of credit, then:

```bash
cp .env.example .env
```

Open `.env` and paste the key after `ANTHROPIC_API_KEY=`. `.env` never leaves your computer and
is never committed.

**4. Make your config and check it:**

```bash
cp config.example.yaml config.yaml
uv run ibis check-config
```

**5. Try it on the demo invoices** (all fake; about $0.01):

```bash
mkdir -p inbox
cp demo/invoices/00[1-9]-*.pdf demo/invoices/042-*.pdf inbox/
uv run ibis run
```

Open `output/ibis.xlsx`. Then look at the flagged one:

```bash
uv run ibis review
```

## Everyday use

1. Drop PDFs into `inbox/`.
2. `uv run ibis run`. Checked documents move to `inbox/processed/`, flagged ones to
   `inbox/needs-review/`, and the sheets in `output/` are updated.
3. `uv run ibis review` to fix and approve the flagged ones.

Running again only processes new files: Ibis remembers every file it has read (by content, so a
renamed copy is still recognised).

| Command | What it does |
|---|---|
| `ibis run` | Read, check, export, move, report. |
| `ibis run --dry-run` | Read and check, but write and move nothing. Still calls the API. |
| `ibis show <file.pdf>` | Read one PDF and print its fields and checks. Saves nothing. |
| `ibis review` | Open the review page in your browser. |
| `ibis cost --estimate <folder>` | Estimate the API cost of a folder. Counts tokens for free; reads nothing. |
| `ibis check-config` | Validate `config.yaml` and show what Ibis will do. |

Every command takes `-c path/to/config.yaml`; the default is `config.yaml` in the current folder.

## What it checks

| Check | Example reason |
|---|---|
| Each line: quantity × unit price = line total | line 2: 5 × £12.00 = £60.00, but the line total is £600.00 |
| Lines add up to the subtotal | the lines add up to £3,115.76, but the subtotal is £3,128.16 (£12.40 more) |
| Subtotal + tax − discount + shipping = total | the total doesn't add up: … = $798.45, but the printed total is $898.45 ($100.00 more) |
| Required fields are there | no invoice number on the document |
| Due date isn't before the invoice date | the due date 25 Sep 2026 is before the invoice date 10 Oct 2026 |
| Invoice date isn't in the future | the invoice date 1 Dec 2026 is in the future |
| Currency is one you accept | CHF isn't one of your allowed currencies (USD, GBP, EUR) |
| Credit notes are negative | credit note with a positive total (£120.00); check the sign |
| Not seen before (same supplier and number) | same supplier and invoice number (INV-2026-0697) as 001-inv-classic.pdf |
| Not the same file twice | identical file to 001-inv-classic.pdf |
| Every value could be read | invoice date '03/09/2026' could be 3 Sep 2026 or 9 Mar 2026; nothing on the document settles which |

Amounts are read in the document's own style (`1,299.00`, `1.299,00 €`, `(12.00)`, `12.00-`), and
numeric dates in the document's own order: a date like `13/09/2026` settles the rest of the
document, and a supplier's order is remembered from their other invoices. Only if nothing settles
it does the date go to review. Nothing a document doesn't print is ever filled in.

## What you get

In `output/` (one ledger across all runs, not just the latest):

| File | Contents |
|---|---|
| `ibis.xlsx` | **Invoices** (one row per document), **Line items** (one row per line), **Needs review** (flagged documents, their reasons, and where the PDF is). Real numbers and dates; review rows in amber. |
| `invoices.csv`, `line_items.csv` | The same data with amounts exactly as read. Opens in Excel with £ and € intact. |
| `reports/run-<time>.json` | Each run's counts, models, tokens, cost and time. No invoice contents. |
| `results/` | One file per document: the source the sheets and the review page are built from. |

Every row says which model produced it, and the reasons column says why anything needs a look.

## The review page

`uv run ibis review` opens a page in your browser (on this computer only). Pick a flagged
document: the PDF is on the left, its fields and line items on the right, with the reasons at
the top. Correct a field and the checks run again as you type.

- **Approve** saves it as `ok`, rebuilds the sheets, and moves the PDF to `processed/`. If a check
  is still failing (say you do pay that supplier in CHF), add a note saying why; it's kept.
- **Discard** (tick the box first) takes a duplicate or a non-invoice out of the sheets and moves
  the PDF to `needs-review/discarded/`. Nothing is deleted.

Every decision is recorded on the document: what changed, from what, when, and your note. The
sheets show it in the **Reviewed** column.

## Configuration

`config.yaml` (copy it from `config.example.yaml`). Folders are relative to the config file.

```yaml
input_dir: inbox
output_dir: output
move_files: true                    # move PDFs to processed/ or needs-review/ after a run

model:
  default: claude-haiku-5-5         # reads every document
  escalate_to: claude-sonnet-5-5    # one retry for documents that fail a check; null to skip
  max_pages: 20
  effort: null                      # low | medium | high; null = each model's default

validation:
  total_tolerance: "0.01"
  allowed_currencies: [USD, GBP, EUR]   # any ISO 4217 code
  currency_symbols: {}                  # e.g. {"$": AUD} for an Australian business

outputs:
  csv: true
  excel: true
  google_sheet:
    enabled: false
    spreadsheet_id: ""              # the sheet's URL or ID

suppliers:                          # optional hints, matched by supplier name
  - name: Acme Packaging Ltd
    currency: GBP
    decimal_mark: ","               # optional: "1.299,00" style
    date_order: day_first           # optional: or month_first
    notes: "Total is bottom left"   # optional: passed to the model
```

`uv run ibis check-config` explains any mistake in one line per problem. Secrets never go in this
file; Ibis refuses keys that look like passwords or tokens.

## Google Sheets

Optional: Ibis can keep a Google Sheet in step with the Excel file (same tabs, same formatting).

1. In [Google Cloud](https://console.cloud.google.com/), create a project, enable the **Google
   Sheets API**, create a **service account**, and download a JSON key for it.
2. Keep the key outside this folder, and add its path to `.env`:
   `GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/key.json`
3. Create a spreadsheet and **Share** it, as **Editor**, with the service account's email (the
   `client_email` in the key file).
4. In `config.yaml`: `google_sheet: {enabled: true, spreadsheet_id: "<the sheet's URL>"}`.

Ibis writes the **Invoices**, **Line items** and **Needs review** tabs and leaves any other tab
alone. If the sheet can't be updated, the run still writes the Excel and CSV files and tells you
why. Use a separate service account and sheet for each client.

## Accuracy and cost

Measured on the 47 fake documents in `demo/` (`uv run python scripts/accuracy.py`, October 2026):

| | |
|---|---|
| Header fields read correctly | **609 of 611 (99.7%)** |
| Line items exactly right | **289 of 289** |
| Deliberately broken documents caught, with the right reason | **9 of 9**; none marked `ok` |
| Values invented (not printed, filled in anyway) | **0** |
| Cost for all 47 | **$0.11** |

The two misses are deliberate: a tax ID too blurred to read on a scanned page (left empty, not
guessed) and a date that nothing on the document can settle (sent to review). Fake documents
aren't your suppliers' documents; measure on a sample of your own before relying on the figure.

A typical one-page invoice costs about **$0.001** on Claude Haiku 5.5. A document that needs a
second read on Sonnet 5.5 costs about **$0.013** more. `ibis cost --estimate <folder>` gives a range
before you run.

## Privacy and security

- **Runs on your computer.** There's no Ibis server. Documents go from your machine to the Claude
  API and nowhere else (plus your Google Sheet, if you turn it on).
- **The API.** Anthropic states that API data is not used for model training without your
  express permission ([data retention](https://platform.claude.com/docs/en/build-with-claude/zero-data-retention)).
  Check the current retention terms before quoting them to a client.
- **Secrets** come only from `.env` or environment variables, never from `config.yaml`. `.env`,
  `config.yaml`, `inbox/` and `output/` are git-ignored, and CI scans the whole history for secrets.
- **Logs and reports** hold file names, counts and costs, never invoice contents or amounts.
- **The review page** listens on `localhost` only and has Streamlit's usage statistics turned off.
- **Spreadsheet safety.** Text from an invoice can never run as a formula in Excel or Sheets.
- **This repository** contains only generated fake invoices. No client document has ever been
  committed.

If nothing may leave your machine at all, Ibis isn't a fit: it needs the Claude API to read.

## Troubleshooting

| Message | What to do |
|---|---|
| `no usable API credentials: set ANTHROPIC_API_KEY in .env` | Create `.env` from `.env.example` and paste your key. |
| `the API key was rejected` | The key is wrong, revoked, or has no credit. Check the Console. |
| `input folder not found` | Create `inbox/`, or change `input_dir` in `config.yaml`. |
| `password-protected` / `not a PDF` / `too many pages` | The file is skipped and left in the inbox; the reason says why. |
| `Google Sheet not updated: ...` | See [Google Sheets](#google-sheets): the key path in `.env`, the Sheets API enabled, the sheet shared with the service account as Editor. |
| A document `couldn't be read` | It wasn't marked as seen, so drop it in `inbox/` again to retry. |
| The review page doesn't open | Go to http://localhost:8501 yourself. Port 8501 must be free. |

## Development

```bash
uv sync                                   # includes the dev tools
uv run pytest                             # never calls the API; 90% coverage gate
uv run ruff check . && uv run ruff format --check .
uv run python demo/generate.py            # regenerate the fake invoices (deterministic)
uv run python scripts/record_fixtures.py  # live: re-record API responses for the tests
uv run python scripts/accuracy.py         # live: the whole demo set, scored (about $0.10)
```

How it fits together is in [ARCHITECTURE.md](ARCHITECTURE.md); what's built, measured and next
is in [PROGRESS.md](PROGRESS.md).

## Licence

© 2026 Yaha Hewagama. All rights reserved. The code is public to read; it isn't licensed for
reuse. Get in touch if you'd like Ibis set up for your business.
