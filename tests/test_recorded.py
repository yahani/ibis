"""Replay real API responses (recorded by scripts/record_fixtures.py) through the real SDK."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibis_docs.accuracy import same, score
from ibis_docs.config import Config
from ibis_docs.extract import extract
from ibis_docs.models import DocType
from tests.fakes import FIXTURES, FakeClaude, recorded

ROOT = Path(__file__).resolve().parent.parent
ANSWERS = {
    d["file"]: d["fields"]
    for d in json.loads((ROOT / "demo" / "answers.json").read_text())["documents"]
}
RECORDED = sorted(p.stem for p in FIXTURES.glob("*.json"))


def replay(name: str):
    """Extract a demo invoice, with Claude's reply played back from its recorded fixture."""
    pdf = ROOT / "demo" / "invoices" / f"{name}.pdf"
    return extract(FakeClaude(recorded(name)).client, pdf, "claude-haiku-5-5", Config())


def test_the_five_fixture_invoices_are_recorded():
    assert RECORDED == [
        "001-inv-classic",
        "003-inv-us_sales",
        "004-credit-compact",
        "006-inv-euro",
        "025-slip-classic",
    ]


@pytest.mark.parametrize("name", RECORDED)
def test_recorded_reply_matches_the_answer_key(name):
    result = replay(name)
    assert result.ok, result.error
    assert result.issues == []
    assert result.input_tokens > 0 and result.output_tokens > 0
    assert score(result.invoice, ANSWERS[f"{name}.pdf"]).differences == []


def test_fields_not_printed_stay_empty():
    """No invented values: what the document doesn't show comes back empty, not zero or guessed."""
    slip = replay("025-slip-classic").invoice
    assert slip.doc_type == DocType.PACKING_SLIP
    assert slip.total is None and slip.currency is None and slip.due_date is None
    assert all(line.unit_price is None for line in slip.line_items)

    us = replay("003-inv-us_sales").invoice
    assert us.supplier_tax_id is None  # the US layout doesn't print one
    assert us.due_date is None  # "Net 30" is printed, but no date


def test_credit_note_amounts_are_negative():
    credit = replay("004-credit-compact").invoice
    assert credit.doc_type == DocType.CREDIT_NOTE
    assert credit.total < 0
    assert all(line.line_total < 0 for line in credit.line_items)


def test_same():
    assert same("1510.72", Decimal("1510.720"))
    assert same(None, None) and not same(None, "x") and not same("x", None)
    assert same("2026-08-18", date(2026, 8, 18))
    assert same("invoice", DocType.INVOICE)
    assert same("Kraft  mailer", "Kraft mailer")


def test_score_lists_differences():
    inv = replay("001-inv-classic").invoice
    answer = {**ANSWERS["001-inv-classic.pdf"], "total": "1.00"}
    answer["line_items"] = answer["line_items"][:2]
    result = score(inv, answer)
    assert result.header_right == 12
    assert result.differences == [
        "total: expected '1.00', got Decimal('1812.86')",
        "line items: expected 2, got 4",
    ]
