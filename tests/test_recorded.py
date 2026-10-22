"""Replay real API responses (recorded by scripts/record_fixtures.py) through the real SDK and
the whole pipeline, and hold the results to the spec's acceptance criteria."""

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibis_docs.accuracy import same, score
from ibis_docs.config import Config
from ibis_docs.discover import discover
from ibis_docs.models import DocType, Status
from ibis_docs.pipeline import process
from scripts.record_fixtures import FIXTURE_SET, RUN_DATE
from tests.fakes import FIXTURES, FakeClaude, by_document, models_asked

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "demo" / "invoices"
ANSWERS = {
    d["file"]: d for d in json.loads((ROOT / "demo" / "answers.json").read_text())["documents"]
}
RETRIED = {"037", "038", "039", "040", "041"}  # broken in ways a misread could explain


def recorded_answers() -> dict:
    """{pdf path: {model: response}} from every fixture file."""
    answers: dict[Path, dict] = {}
    for path in FIXTURES.glob("*.json"):
        record = json.loads(path.read_text())
        answers.setdefault(INVOICES / record["file"], {})[record["model"]] = record["response"]
    return answers


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    inbox = tmp_path_factory.mktemp("inbox")
    for name in FIXTURE_SET:
        shutil.copy(INVOICES / name, inbox / name)
    fake = FakeClaude(route=by_document(recorded_answers()))
    files = discover(inbox, 20).found
    result = process(files, Config(), fake.client, today=RUN_DATE)
    return result, fake


def docs(outcome):
    return {d.name: d for d in outcome[0].documents}


def test_every_fixture_file_is_recorded():
    recorded = {json.loads(p.read_text())["file"] for p in FIXTURES.glob("*.json")}
    # 045 is a byte-identical copy of 001, so it's never sent.
    assert recorded == set(FIXTURE_SET) - {"045-inv-copy-of-001.pdf"}


def test_every_document_gets_its_expected_status(outcome):
    for name, doc in docs(outcome).items():
        expected = ANSWERS[name]["expected_status"]
        allowed = {"ok", "review"} if expected == "ok_or_review" else {expected}
        assert doc.invoice.status.value in allowed, (name, doc.invoice.reasons)


def test_every_broken_document_is_flagged_with_the_right_reason(outcome):
    for name, doc in docs(outcome).items():
        expected = ANSWERS[name]["expected_reasons"]
        if not expected or ANSWERS[name]["expected_status"] == "ok_or_review":
            continue
        assert doc.invoice.status == Status.REVIEW, name
        assert set(expected) <= {r.code for r in doc.invoice.reasons}, name


def test_only_documents_without_reasons_are_ok(outcome):
    for doc in docs(outcome).values():
        assert (doc.invoice.status == Status.OK) == (doc.invoice.reasons == [])


def test_fields_match_the_answer_key(outcome):
    known = {
        # The blurred scan's footer can't be read; the tax ID is left empty, not guessed.
        "044-inv-euro.pdf": ["supplier_tax_id: expected 'DE473268628', got None"],
        # 03/09/2026 with nothing to settle it: left empty and flagged (see ambiguous_date).
        "047-inv-classic.pdf": ["invoice_date: expected '2026-09-03', got None"],
    }
    for name, doc in docs(outcome).items():
        assert score(doc.invoice, ANSWERS[name]["fields"]).differences == known.get(name, []), name


def test_no_invented_values(outcome):
    """A field the document doesn't print is empty in the output, never filled in."""
    for name, doc in docs(outcome).items():
        for field, expected in ANSWERS[name]["fields"].items():
            if expected is None and field != "line_items":
                assert getattr(doc.invoice, field) is None, (name, field)


def test_retries_happen_only_where_a_misread_could_explain_the_problem(outcome):
    _, fake = outcome
    for name in FIXTURE_SET:
        asked = models_asked(fake, INVOICES / name)
        if name[:3] in RETRIED:
            assert asked == ["claude-haiku-5-5", "claude-sonnet-5-5"], name
        elif name.startswith("045"):
            continue  # same bytes as 001, so 001's request matches it too; see the count below
        else:
            assert asked == ["claude-haiku-5-5"], name
    # 16 distinct files once each (045 is never sent), plus 5 retries.
    assert len(fake.requests) == 16 + 5
    sonnet_requests = [r for r in fake.requests if r["model"] == "claude-sonnet-5-5"]
    assert sonnet_requests and all(r["fallbacks"] == "default" for r in sonnet_requests)


def test_the_final_model_is_recorded(outcome):
    d = docs(outcome)
    assert d["001-inv-classic.pdf"].invoice.model == "claude-haiku-5-5"
    assert d["037-inv-classic.pdf"].invoice.model == "claude-sonnet-5-5"


def test_reasons_read_plainly(outcome):
    d = docs(outcome)

    def messages(name):
        return [r.message for r in d[name].invoice.reasons]

    assert messages("038-inv-modern.pdf") == [
        "the lines add up to £3,115.76, but the subtotal is £3,128.16 (£12.40 more)"
    ]
    assert messages("043-inv-classic.pdf") == [
        "same supplier and invoice number (INV-2026-0697) as 001-inv-classic.pdf"
    ]
    assert messages("045-inv-copy-of-001.pdf") == ["identical file to 001-inv-classic.pdf"]


def test_dates_and_discounts(outcome):
    d = docs(outcome)
    # 046: 05/08/2026 due 04/09/2026 only works day first.
    assert d["046-inv-classic.pdf"].invoice.invoice_date == date(2026, 8, 5)
    # 002 prints its discount as "-£38.59"; it's stored as the amount taken off.
    assert d["002-inv-modern.pdf"].invoice.discount == Decimal("38.59")
    # 004 is a credit note: negative throughout.
    credit = d["004-credit-compact.pdf"].invoice
    assert credit.doc_type == DocType.CREDIT_NOTE and credit.total < 0
    # 025 is a packing slip: no money, and its EIN without the label.
    slip = d["025-slip-classic.pdf"].invoice
    assert slip.total is None and slip.supplier_tax_id == "36-6106824"


def test_tokens_are_counted(outcome):
    tokens = outcome[0].tokens()
    assert set(tokens) == {"claude-haiku-5-5", "claude-sonnet-5-5"}
    assert all(i > 0 and o > 0 for i, o in tokens.values())


def test_same():
    assert same("1510.72", Decimal("1510.720"))
    assert same(None, None) and not same(None, "x") and not same("x", None)
    assert same("2026-08-18", date(2026, 8, 18))
    assert same("invoice", DocType.INVOICE)
    assert same("Kittiwake Coffee  Roasters", "KITTIWAKE COFFEE ROASTERS")


def test_score_lists_differences(outcome):
    inv = docs(outcome)["001-inv-classic.pdf"].invoice
    answer = {**ANSWERS["001-inv-classic.pdf"]["fields"], "total": "1.00"}
    answer["line_items"] = answer["line_items"][:2]
    result = score(inv, answer)
    assert result.header_right == 12
    assert result.differences == [
        "total: expected '1.00', got Decimal('1812.86')",
        "line items: expected 2, got 4",
    ]
