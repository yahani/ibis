"""Record live Claude API responses for the tests, and score them against the answer key.

    uv run python scripts/record_fixtures.py               # the 5 fixture invoices
    uv run python scripts/record_fixtures.py 044-inv-euro.pdf

Needs ANTHROPIC_API_KEY in .env. Each response body is saved to tests/fixtures/<name>.json
(the request, which holds the PDF, is not). Tests replay these files and never call the API.
Five one-page-ish invoices on Haiku 5.5 cost well under one cent.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import anthropic
import httpx2
from dotenv import load_dotenv

from ibis_docs.accuracy import score
from ibis_docs.config import Config
from ibis_docs.extract import ApiSetupError, extract

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "demo" / "invoices"
ANSWERS = ROOT / "demo" / "answers.json"
FIXTURES = ROOT / "tests" / "fixtures"

# Classic GBP, US layout, credit note, European two-page with "1.299,00 €", packing slip.
DEFAULT_FILES = [
    "001-inv-classic.pdf",
    "003-inv-us_sales.pdf",
    "004-credit-compact.pdf",
    "006-inv-euro.pdf",
    "025-slip-classic.pdf",
]
# Haiku 5.5 list prices per million tokens (input, output), prompts up to 100k tokens.
PRICES = {"claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"))}


class Recorder:
    """An httpx response hook that keeps the last response body."""

    last_body: bytes = b""

    def __call__(self, response: httpx2.Response) -> None:
        self.last_body = response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", default=DEFAULT_FILES, help="names in demo/invoices")
    parser.add_argument("--model", default="claude-haiku-5-5")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    recorder = Recorder()
    try:
        client = anthropic.Anthropic(
            max_retries=1,
            timeout=180,
            http_client=anthropic.DefaultHttpxClient(event_hooks={"response": [recorder]}),
        )
    except anthropic.CredentialsError as exc:
        print(f"✗ no usable API credentials: set ANTHROPIC_API_KEY in .env ({exc})")
        return 1

    answers = {d["file"]: d for d in json.loads(ANSWERS.read_text())["documents"]}
    config = Config()
    FIXTURES.mkdir(parents=True, exist_ok=True)

    total_right = total_fields = tokens_in = tokens_out = 0
    for name in args.files:
        try:
            result = extract(client, INVOICES / name, args.model, config)
        except ApiSetupError as exc:
            print(f"✗ {exc}")
            return 1
        tokens_in += result.input_tokens
        tokens_out += result.output_tokens
        tokens = f"{result.input_tokens:,} in / {result.output_tokens:,} out"

        if result.error:
            print(f"✗ {name}: {result.error} ({tokens}); not saved")
            continue

        fixture = FIXTURES / f"{Path(name).stem}.json"
        record = {
            "file": name,
            "model": args.model,
            "recorded": date.today().isoformat(),
            "response": json.loads(recorder.last_body),
        }
        fixture.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")

        result_score = score(result.invoice, answers[name]["fields"])
        right, fields = result_score.header_right, result_score.header_total
        total_right += right
        total_fields += fields
        mark = "✓" if not result_score.differences else "•"
        print(f"{mark} {name}: {right}/{fields} header fields ({tokens})")
        for diff in result_score.differences:
            print(f"    {diff}")
        for issue in result.issues:
            print(f"    ! {issue}")

    if total_fields:
        print(
            f"\nHeader fields right: {total_right}/{total_fields} "
            f"({100 * total_right / total_fields:.1f}%; target 95%)"
        )
    price_in, price_out = PRICES.get(args.model, (Decimal(0), Decimal(0)))
    cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    print(f"Tokens: {tokens_in:,} in / {tokens_out:,} out, about ${cost:.4f}")
    print(f"Fixtures in {FIXTURES.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
