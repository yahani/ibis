"""Run the pipeline live on demo invoices, record every API response for the tests, and score
the results against the answer key.

    uv run python scripts/record_fixtures.py                 # the fixture set (FIXTURE_SET)
    uv run python scripts/record_fixtures.py 044-inv-euro.pdf

Needs ANTHROPIC_API_KEY in .env. Each response body is saved to tests/fixtures/, named
<file stem>.json for the default model and <file stem>.<model>.json for a retry (the request,
which holds the PDF, is not saved). Tests replay these files and never call the API.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import anthropic
import httpx2
from dotenv import load_dotenv

from ibis_docs.accuracy import score
from ibis_docs.config import Config
from ibis_docs.discover import discover
from ibis_docs.extract import ApiSetupError
from ibis_docs.models import Status
from ibis_docs.pipeline import process

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "demo" / "invoices"
ANSWERS = ROOT / "demo" / "answers.json"
FIXTURES = ROOT / "tests" / "fixtures"

# Variety for extraction (session 2), plus a discount, every broken invoice, and the ambiguous
# dates (session 3). 001 is in it so 043 (a resend of 001) and 045 (a copy of it) are caught.
FIXTURE_SET = [
    "001-inv-classic.pdf",
    "002-inv-modern.pdf",
    "003-inv-us_sales.pdf",
    "004-credit-compact.pdf",
    "006-inv-euro.pdf",
    "025-slip-classic.pdf",
    "037-inv-classic.pdf",
    "038-inv-modern.pdf",
    "039-inv-us_sales.pdf",
    "040-inv-compact.pdf",
    "041-inv-plain_text.pdf",
    "042-inv-classic.pdf",
    "043-inv-classic.pdf",
    "044-inv-euro.pdf",
    "045-inv-copy-of-001.pdf",
    "046-inv-classic.pdf",
    "047-inv-classic.pdf",
]
# List prices per million tokens (input, output); Haiku's are for prompts up to 100k tokens.
PRICES = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
}
# The run date the replay tests use, so date checks give the same answer every day.
RUN_DATE = date(2026, 10, 22)


class Recorder:
    """An httpx response hook that keeps each response body by (file, model asked)."""

    def __init__(self, files: list[Path]):
        self.names: dict[bytes, str] = {}
        for p in files:  # a byte-identical copy is never sent, so the first name is the one
            self.names.setdefault(p.read_bytes(), p.name)
        self.bodies: dict[tuple[str, str], bytes] = {}
        self.lock = threading.Lock()

    def __call__(self, response: httpx2.Response) -> None:
        body = response.read()
        request = json.loads(response.request.content)
        pdf = request["messages"][0]["content"][0]["source"]["data"]
        name = self.names[base64.standard_b64decode(pdf)]
        with self.lock:
            self.bodies[(name, request["model"])] = body


def fixture_name(file: str, model: str, default_model: str) -> str:
    stem = Path(file).stem
    return f"{stem}.json" if model == default_model else f"{stem}.{model}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", default=FIXTURE_SET, help="names in demo/invoices")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = Config()
    paths = [INVOICES / name for name in args.files]
    recorder = Recorder(paths)
    try:
        client = anthropic.Anthropic(
            max_retries=1,
            timeout=180,
            http_client=anthropic.DefaultHttpxClient(event_hooks={"response": [recorder]}),
        )
    except anthropic.CredentialsError as exc:
        print(f"✗ no usable API credentials: set ANTHROPIC_API_KEY in .env ({exc})")
        return 1

    # Discover from a folder holding just these files, so duplicates are linked as in a run.
    staging = ROOT / "output" / ".record-staging"
    staging.mkdir(parents=True, exist_ok=True)
    for old in staging.glob("*.pdf"):
        old.unlink()
    for p in paths:
        (staging / p.name).write_bytes(p.read_bytes())
    found = discover(staging, config.model.max_pages).found

    try:
        outcome = process(found, config, client, today=RUN_DATE, on_progress=print)
    except ApiSetupError as exc:
        print(f"✗ {exc}")
        return 1
    finally:
        for old in staging.glob("*.pdf"):
            old.unlink()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for (name, model), body in sorted(recorder.bodies.items()):
        record = {
            "file": name,
            "model": model,
            "recorded": date.today().isoformat(),
            "response": json.loads(body),
        }
        target = FIXTURES / fixture_name(name, model, config.model.default)
        target.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")

    answers = {d["file"]: d for d in json.loads(ANSWERS.read_text())["documents"]}
    right = fields = caught = missed = 0
    print()
    for doc in outcome.documents:
        inv, answer = doc.invoice, answers[doc.name]
        expected = answer["expected_status"]
        models = " → ".join(ex.model for ex in doc.extractions) or "no call"
        result = score(inv, answer["fields"])
        right += result.header_right
        fields += result.header_total
        codes = {r.code for r in inv.reasons}
        if answer["expected_reasons"] and expected != "ok_or_review":
            hit = inv.status != Status.OK and set(answer["expected_reasons"]) <= codes
            caught += hit
            missed += not hit
        as_expected = expected == inv.status.value or (
            expected == "ok_or_review" and inv.status != Status.FAILED
        )
        print(f"{'✓' if as_expected else '✗'} {doc.name}: {inv.status.value} "
              f"(expected {expected}; {models})")  # fmt: skip
        for reason in inv.reasons:
            print(f"    - [{reason.code}] {reason.message}")
        for diff in result.differences:
            print(f"    ≠ {diff}")

    cost = Decimal(0)
    for model, (tokens_in, tokens_out) in outcome.tokens().items():
        price_in, price_out = PRICES.get(model, (Decimal(0), Decimal(0)))
        cost += (tokens_in * price_in + tokens_out * price_out) / 1_000_000
        print(f"{model}: {tokens_in:,} in / {tokens_out:,} out")
    print(f"\nHeader fields right: {right}/{fields} ({100 * right / max(fields, 1):.1f}%)")
    print(f"Broken documents caught with the expected reason: {caught}/{caught + missed}")
    print(f"About ${cost:.4f}. Fixtures in {FIXTURES.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
