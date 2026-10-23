"""Accuracy run: the whole demo set through the live pipeline, scored against the answer key.

    uv run python scripts/accuracy.py                # model defaults from config.example.yaml
    uv run python scripts/accuracy.py --effort low   # compare a cheaper setting

Reports per-field accuracy (the spec's target: 95% of header fields), whether every broken
document was flagged with its reason, any invented values, retries, time, and the actual cost
next to `ibis cost --estimate`'s figure. Writes nothing to the demo folder. About $0.10 a run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from ibis_docs.accuracy import HEADER_FIELDS, LINE_FIELDS, same
from ibis_docs.config import load_config
from ibis_docs.discover import discover
from ibis_docs.extract import ApiSetupError, make_client
from ibis_docs.models import Status
from ibis_docs.pipeline import process
from ibis_docs.report import cost_of, dollars, estimate

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "demo" / "invoices"
ANSWERS = ROOT / "demo" / "answers.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--effort", choices=["low", "medium", "high"], default=None)
    parser.add_argument("--save", type=Path, help="write the results as JSON here")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / "config.example.yaml")
    if args.effort:
        config = config.model_copy(
            update={"model": config.model.model_copy(update={"effort": args.effort})}
        )
    answers = {d["file"]: d for d in json.loads(ANSWERS.read_text())["documents"]}

    with tempfile.TemporaryDirectory() as tmp:
        inbox = Path(tmp)
        for pdf in INVOICES.glob("*.pdf"):
            shutil.copy(pdf, inbox / pdf.name)
        found = discover(inbox, config.model.max_pages).found
        try:
            client = make_client()
            predicted = estimate(found, config, client)
            started = time.monotonic()
            outcome = process(found, config, client, today=date.today())
        except ApiSetupError as exc:
            print(f"✗ {exc}")
            return 1
        seconds = time.monotonic() - started

    per_field: Counter[str] = Counter()
    header_right = header_total = 0
    lines_exact = lines_total = 0
    caught, missed, invented, wrong_status = [], [], [], []
    for doc in outcome.documents:
        inv, answer = doc.invoice, answers[doc.name]
        fields = answer["fields"]
        for name in HEADER_FIELDS:
            header_total += 1
            if same(fields[name], getattr(inv, name)):
                header_right += 1
                per_field[name] += 1
            elif fields[name] is None:
                invented.append(f"{doc.name}: {name} = {getattr(inv, name)!r}")
        expected_lines = fields["line_items"]
        lines_total += len(expected_lines)
        if len(expected_lines) == len(inv.line_items):
            lines_exact += sum(
                all(same(e[f], getattr(g, f)) for f in LINE_FIELDS)
                for e, g in zip(expected_lines, inv.line_items, strict=True)
            )
        expected = answer["expected_status"]
        if expected == "ok_or_review":
            if inv.status == Status.FAILED:
                wrong_status.append(doc.name)
        elif inv.status.value != expected:
            wrong_status.append(f"{doc.name}: {inv.status.value}, expected {expected}")
        if answer["expected_reasons"] and expected != "ok_or_review":
            codes = {r.code for r in inv.reasons}
            hit = inv.status != Status.OK and set(answer["expected_reasons"]) <= codes
            (caught if hit else missed).append(doc.name)

    documents = len(outcome.documents)
    tokens = outcome.tokens()
    actual = sum((cost_of(m, i, o) or 0 for m, (i, o) in tokens.items()), start=0)
    retried = sum(1 for d in outcome.documents if len(d.extractions) > 1)

    effort = config.model.effort or "model default"
    print(f"Effort: {effort}; {documents} documents, {seconds:.0f} s")
    print(f"Header fields right: {header_right}/{header_total} "
          f"({100 * header_right / header_total:.1f}%; target 95%)")  # fmt: skip
    for name in HEADER_FIELDS:
        print(f"  {name:<16} {per_field[name]:>3}/{documents}")
    print(f"Line items exactly right: {lines_exact}/{lines_total}")
    broken = len(caught) + len(missed)
    print(f"Broken documents flagged with the right reason: {len(caught)}/{broken}")
    for name in missed:
        print(f"  ✗ missed: {name}")
    print(f"Documents with the expected status: {documents - len(wrong_status)}/{documents}")
    for item in wrong_status:
        print(f"  ✗ {item}")
    print(f"Invented values (printed nowhere, filled in anyway): {len(invented)}")
    for item in invented:
        print(f"  ✗ {item}")
    print(f"Retried on {config.model.escalate_to}: {retried}")
    for model, (i, o) in tokens.items():
        print(f"  {model}: {i:,} in / {o:,} out, {dollars(cost_of(model, i, o))}")
    print(f"Actual cost {dollars(actual)}; the estimate said {dollars(predicted.cost)} "
          f"({100 * (predicted.cost - actual) / actual:+.0f}%)")  # fmt: skip

    if args.save:
        args.save.write_text(
            json.dumps(
                {
                    "effort": config.model.effort,
                    "documents": documents,
                    "header_right": header_right,
                    "header_total": header_total,
                    "per_field": dict(per_field),
                    "lines_exact": lines_exact,
                    "lines_total": lines_total,
                    "broken_caught": len(caught),
                    "broken_total": broken,
                    "wrong_status": wrong_status,
                    "invented": invented,
                    "retried": retried,
                    "tokens": tokens,
                    "cost": str(actual),
                    "estimate": str(predicted.cost),
                    "seconds": round(seconds),
                },
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
