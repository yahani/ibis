# ibis

**Every invoice, read and checked.** Right, or flagged.

Ibis reads supplier invoices and packing slips (PDF), pulls out the key fields and line items,
re-adds every total in Python, and exports a clean sheet. Anything it can't verify goes on a
review list with a plain reason instead of being guessed.

> Status: in development (session 3 of 5). See [PROGRESS.md](PROGRESS.md).

## Quick start

```bash
uv sync
cp config.example.yaml config.yaml
uv run ibis check-config
```

## Demo invoices

`demo/invoices/` holds generated **fake** invoices and `demo/answers.json` their correct values.
No real client documents ever go in this repository.

```bash
uv run python demo/generate.py                      # USD, GBP, EUR (default)
uv run python demo/generate.py --currencies USD,AUD,NZD
```
