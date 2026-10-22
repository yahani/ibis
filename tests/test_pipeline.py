from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibis_docs.config import Config
from ibis_docs.dates import Order
from ibis_docs.discover import Found, sha256_of
from ibis_docs.extract import ApiSetupError
from ibis_docs.memory import Memory
from ibis_docs.models import Status
from ibis_docs.pipeline import process
from tests.fakes import FakeClaude, by_document, error_response, message, models_asked
from tests.test_extract import REPLY

DEMO = Path(__file__).resolve().parent.parent / "demo" / "invoices"
# Any distinct PDFs will do: the fake routes on the bytes sent, not on what they show.
A, B, C = (DEMO / n for n in ("001-inv-classic.pdf", "002-inv-modern.pdf", "003-inv-us_sales.pdf"))
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
TODAY = date(2026, 10, 22)

# REPLY's numbers add up except for the lines it leaves out; give it lines that add up.
GOOD = {
    **REPLY,
    "subtotal": "£1,481.00",
    "tax": "£296.20",
    "total": "£1,777.20",
    "due_date": "17 September 2026",
    "line_items": [
        {**REPLY["line_items"][0]},
        {**REPLY["line_items"][1], "tax_rate": ""},
    ],
}


def reply(**changes) -> dict:
    return message({**GOOD, **changes}, model=HAIKU)


def sonnet(**changes) -> dict:
    return message({**GOOD, **changes}, model=SONNET, input_tokens=4100, output_tokens=900)


def found(path: Path, name: str | None = None, duplicate_of: str | None = None) -> Found:
    return Found(path, sha256_of(path), 1, duplicate_of=duplicate_of)


def run(answers: dict, files: list[Found] | None = None, config: Config | None = None, **kw):
    fake = FakeClaude(route=by_document(answers))
    files = files or [found(p) for p in answers]
    return process(files, config or Config(), fake.client, today=TODAY, **kw), fake


def test_a_clean_document_is_ok_after_one_read():
    outcome, fake = run({A: {HAIKU: reply()}})
    (doc,) = outcome.documents
    assert doc.invoice.status == Status.OK
    assert doc.invoice.reasons == []
    assert doc.invoice.model == HAIKU
    assert doc.invoice.source_file.name == A.name
    assert doc.invoice.total == Decimal("1777.20")
    assert models_asked(fake, A) == [HAIKU]


def test_a_misread_is_retried_and_the_passing_read_wins():
    outcome, fake = run({A: {HAIKU: reply(total="£1,877.20"), SONNET: sonnet()}})
    (doc,) = outcome.documents
    assert models_asked(fake, A) == [HAIKU, SONNET]
    assert doc.invoice.status == Status.OK
    assert doc.invoice.model == SONNET
    assert doc.invoice.total == Decimal("1777.20")
    assert [ex.model for ex in doc.extractions] == [HAIKU, SONNET]
    # The retry asks for the refusal fallback.
    assert fake.requests[-1]["fallbacks"] == "default"


def test_when_both_reads_fail_the_stronger_one_is_kept_with_where_they_disagree():
    outcome, _ = run({A: {HAIKU: reply(total="£1,877.20"), SONNET: sonnet(total="£1,977.20")}})
    (doc,) = outcome.documents
    assert doc.invoice.status == Status.REVIEW
    assert doc.invoice.model == SONNET
    codes = [r.code for r in doc.invoice.reasons]
    assert codes == ["total_mismatch", "models_disagree"]
    assert doc.invoice.reasons[1].message == (
        "claude-haiku-5-5 and claude-sonnet-5-5 read this differently: total £1,877.20 vs £1,977.20"
    )


def test_when_both_reads_agree_on_a_broken_document_there_is_nothing_to_add():
    outcome, _ = run({A: {HAIKU: reply(total="£1,877.20"), SONNET: sonnet(total="£1,877.20")}})
    assert [r.code for r in outcome.documents[0].invoice.reasons] == ["total_mismatch"]


def test_a_failed_first_read_is_retried():
    outcome, _ = run({A: {HAIKU: error_response(500, "overloaded"), SONNET: sonnet()}})
    (doc,) = outcome.documents
    assert doc.invoice.status == Status.OK and doc.invoice.model == SONNET


def test_when_both_calls_fail_the_document_failed():
    outcome, _ = run(
        {
            A: {
                HAIKU: error_response(500, "overloaded"),
                SONNET: message("no", stop_reason="refusal"),
            }
        }
    )
    inv = outcome.documents[0].invoice
    assert inv.status == Status.FAILED
    assert [r.message for r in inv.reasons] == [
        "claude-haiku-5-5: the API returned an error (500): overloaded",
        "claude-sonnet-5-5: Claude's reply didn't match the invoice schema (refused or cut off)",
    ]
    assert inv.total is None


def test_a_failed_retry_keeps_the_first_read():
    outcome, _ = run({A: {HAIKU: reply(total="£1,877.20"), SONNET: error_response(500, "busy")}})
    inv = outcome.documents[0].invoice
    assert inv.status == Status.REVIEW and inv.model == HAIKU
    assert [r.code for r in inv.reasons] == ["total_mismatch", "retry_failed"]
    assert inv.reasons[1].message == (
        "the retry on claude-sonnet-5-5 failed: the API returned an error (500): busy"
    )


def test_no_retry_when_escalation_is_off():
    cfg = Config.model_validate({"model": {"escalate_to": None}})
    outcome, fake = run({A: {HAIKU: reply(total="£1,877.20")}}, config=cfg)
    assert models_asked(fake, A) == [HAIKU]
    assert outcome.documents[0].invoice.status == Status.REVIEW


@pytest.mark.parametrize(
    "changes",
    [{"currency": "CHF"}, {"invoice_date": "03/09/2026", "due_date": ""}],
    ids=["currency_not_allowed", "ambiguous_date"],
)
def test_no_retry_for_what_a_stronger_model_would_read_the_same(changes):
    outcome, fake = run({A: {HAIKU: reply(**changes)}})
    assert models_asked(fake, A) == [HAIKU]
    assert outcome.documents[0].invoice.status == Status.REVIEW


def test_same_supplier_and_number_is_a_duplicate():
    outcome, _ = run(
        {A: {HAIKU: reply()}, B: {HAIKU: reply(invoice_number="inv-2026-0697 ")}},
    )
    first, second = outcome.documents
    assert first.invoice.status == Status.OK
    assert second.invoice.status == Status.REVIEW
    assert [r.message for r in second.invoice.reasons] == [
        f"same supplier and invoice number (inv-2026-0697) as {A.name}"
    ]


def test_a_duplicate_of_an_earlier_run():
    memory = Memory(invoices={"harbourline packaging ltd|inv-2026-0697": "old.pdf"})
    outcome, _ = run({A: {HAIKU: reply()}}, memory=memory)
    assert outcome.documents[0].invoice.reasons[0].message.endswith("as old.pdf")


def test_a_different_supplier_with_the_same_number_is_not_a_duplicate():
    outcome, _ = run({A: {HAIKU: reply()}, B: {HAIKU: reply(supplier_name="Other Ltd")}})
    assert [d.invoice.status for d in outcome.documents] == [Status.OK, Status.OK]


def test_an_identical_file_is_flagged_without_a_second_call(tmp_path):
    copy = tmp_path / "045-copy.pdf"
    copy.write_bytes(A.read_bytes())
    files = [found(A), Found(copy, sha256_of(copy), 1, duplicate_of=A.name)]
    outcome, fake = run({A: {HAIKU: reply()}}, files=files)
    original, dup = outcome.documents
    assert len(fake.requests) == 1
    assert dup.extractions == []
    assert dup.invoice.status == Status.REVIEW
    assert dup.invoice.source_file.name == "045-copy.pdf"
    assert [r.message for r in dup.invoice.reasons] == [f"identical file to {A.name}"]
    assert dup.invoice.total == original.invoice.total
    assert original.invoice.reasons == []  # the copy's reason isn't shared with the original


def test_a_suppliers_date_order_is_learned_from_their_other_documents():
    outcome, _ = run(
        {
            A: {HAIKU: reply(invoice_date="03/09/2026", due_date="")},
            B: {HAIKU: reply(invoice_number="INV-2", invoice_date="13/09/2026", due_date="")},
        }
    )
    ambiguous, settled = outcome.documents
    assert settled.invoice.invoice_date == date(2026, 9, 13)
    assert ambiguous.invoice.invoice_date == date(2026, 9, 3)
    assert ambiguous.invoice.status == Status.OK
    assert outcome.learned_orders == {"harbourline packaging ltd": Order.DAY_FIRST}


def test_a_remembered_date_order_settles_a_date():
    memory = Memory(date_orders={"harbourline packaging ltd": Order.MONTH_FIRST})
    outcome, _ = run({A: {HAIKU: reply(invoice_date="03/09/2026", due_date="")}}, memory=memory)
    assert outcome.documents[0].invoice.invoice_date == date(2026, 3, 9)


def test_a_supplier_whose_documents_disagree_teaches_nothing():
    outcome, _ = run(
        {
            A: {HAIKU: reply(invoice_date="13/09/2026", due_date="")},
            B: {HAIKU: reply(invoice_number="INV-2", invoice_date="09/25/2026", due_date="")},
        }
    )
    assert outcome.learned_orders == {}


def test_an_api_setup_problem_stops_the_run():
    with pytest.raises(ApiSetupError):
        run({A: {HAIKU: error_response(401)}, B: {HAIKU: reply()}})


def test_counts_and_tokens():
    outcome, _ = run(
        {
            A: {HAIKU: reply()},
            B: {
                HAIKU: reply(invoice_number="2", total="£1.00"),
                SONNET: sonnet(invoice_number="2"),
            },
            C: {HAIKU: reply(invoice_number="3", currency="CHF")},
        }
    )
    assert [outcome.count(s) for s in Status] == [2, 1, 0]
    assert outcome.tokens() == {HAIKU: (12000, 1500), SONNET: (4100, 900)}


def test_progress_is_reported():
    seen = []
    run({A: {HAIKU: reply()}}, on_progress=seen.append)
    assert seen == [f"{A.name}: read by claude-haiku-5-5"]
