# The 60-second demo

What to show and say for the portfolio video. Everything in it is generated fake data.

**Before recording:** `.env` has an API key with credit; Excel (or Numbers) installed; screen at a
size where the terminal and the browser are readable. Do one practice run: `scripts/demo.sh`
rebuilds the workspace from scratch each time (about $0.02 a run).

**Run it:** `scripts/demo.sh`. It copies 10 fake PDFs into `demo-workspace/inbox/`, runs Ibis,
opens the Excel file, then opens the review page.

| Time | Show | Say |
|---|---|---|
| 0:00 | The inbox folder with 10 PDFs: different suppliers, layouts, currencies, a two-page invoice, a credit note, a packing slip | "Ten supplier documents, as they arrive. Typing these in takes an hour." |
| 0:06 | Terminal: `scripts/demo.sh` starts; the ✓ lines appear | "Ibis reads each one with Claude, then re-adds every number in Python." |
| 0:15 | The three `!` lines and their reasons | "Seven checked out. Three need a look, and it says why: this total is a hundred dollars more than its parts. This is the same file saved twice. And this date could be the 3rd of September or the 9th of March." |
| 0:27 | The summary line and the cost | "Ten documents, about two cents, twenty seconds." |
| 0:31 | Excel opens: the Invoices sheet, then Line items, then Needs review (amber) | "One row per invoice, one per line, real numbers and dates. And a list of what to check." |
| 0:40 | The review page: pick `047-inv-classic.pdf`. The PDF is on the left, the reason at the top | "It doesn't guess. It can't tell which way round this date is, so it asks." |
| 0:47 | Type `2026-09-03` in Invoice date; "Everything checks out" turns green; click **Approve** | "It's a UK supplier, so it's the 3rd of September. The checks run again, and it's approved, with a record of what I changed." |
| 0:55 | Back to the sheet: the row is now `ok`, Reviewed says "approved, edited invoice date" | "Every invoice, read and checked. Right, or flagged." |

**Points to land:** the reasons are in plain English with the actual amounts; nothing is filled
in by guesswork; it runs on your own computer; the cost per document is a tenth of a cent.

**After recording:** `rm -rf demo-workspace` (it's git-ignored either way).
