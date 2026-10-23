"""The review page's logic (review.py) and the page itself (review_app.py, run headless)."""

import csv
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ibis_docs.config import Config
from ibis_docs.discover import discover
from ibis_docs.memory import Memory
from ibis_docs.models import Status
from ibis_docs.pipeline import process
from ibis_docs.results import load_all
from ibis_docs.review import (
    EditError,
    apply_edits,
    approve,
    discard,
    form_values,
    line_values,
    recheck,
)
from ibis_docs.run import keep, write_exports
from tests.fakes import FakeClaude, by_document, message
from tests.test_pipeline import GOOD, HAIKU, SONNET, TODAY

DEMO = Path(__file__).resolve().parent.parent / "demo" / "invoices"
A, B, C = (DEMO / n for n in ("001-inv-classic.pdf", "002-inv-modern.pdf", "003-inv-us_sales.pdf"))
NOW = datetime(2026, 10, 23, 10, 0)
APP = Path(__file__).resolve().parent.parent / "src" / "ibis_docs" / "review_app.py"

# B's total is misread (1,877.20 instead of 1,777.20) on both reads; C is in CHF.
BAD_TOTAL = {**GOOD, "invoice_number": "INV-2", "total": "£1,877.20"}
ANSWERS = {
    A: {HAIKU: message(GOOD)},
    B: {HAIKU: message(BAD_TOTAL), SONNET: message(BAD_TOTAL, model=SONNET)},
    C: {HAIKU: message({**GOOD, "invoice_number": "INV-3", "currency": "CHF"})},
}


@pytest.fixture
def project(tmp_path) -> Config:
    """A project after one run: A ok, B and C flagged, and a byte-identical copy of A."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for p in (A, B, C):
        shutil.copy(p, inbox / p.name)
    shutil.copy(A, inbox / "zz-copy.pdf")
    config = Config.model_validate({"base_dir": tmp_path})
    fake = FakeClaude(route=by_document(ANSWERS))
    outcome = process(discover(inbox, 20).found, config, fake.client, today=TODAY)
    keep(outcome, config, "r1", NOW)
    return config


def saved(config: Config) -> dict:
    return {r.name: r for r in load_all(config.output_path)}


def csv_rows(config: Config) -> list[dict]:
    with (config.output_path / "invoices.csv").open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# --- the logic ------------------------------------------------------------------------------


def test_the_form_shows_plain_values(project):
    inv = saved(project)[B.name].invoice
    values = form_values(inv)
    assert values["total"] == "1877.20" and values["invoice_date"] == "2026-08-18"
    assert values["discount"] == "" and values["doc_type"] == "invoice"
    assert line_values(inv)[0]["quantity"] == "20"


def test_fixing_a_misread_total_clears_the_reason(project):
    result = saved(project)[B.name]
    header = {**form_values(result.invoice), "total": "1777.20"}
    edited, changes = apply_edits(result.invoice, header, line_values(result.invoice), project)
    assert edited.total == Decimal("1777.20")
    assert [(c.field, c.before, c.after) for c in changes] == [("total", "1877.20", "1777.20")]
    assert recheck(edited, result, load_all(project.output_path), project, date(2026, 10, 23)) == []


def test_unreadable_form_values_are_explained(project):
    result = saved(project)[B.name]
    header = {
        **form_values(result.invoice),
        "invoice_date": "18/08/2026",
        "total": "lots",
        "currency": "dubloons",
    }
    lines = [*line_values(result.invoice), dict.fromkeys(line_values(result.invoice)[0], "")]
    lines.append({"description": "", "quantity": "3"})
    with pytest.raises(EditError) as err:
        apply_edits(result.invoice, header, lines, project)
    assert err.value.problems == [
        "invoice date '18/08/2026': use the form YYYY-MM-DD",
        "currency 'dubloons' isn't a currency code like GBP",
        "line 4 needs a description",
    ]
    fixed = {**header, "invoice_date": "2026-08-18", "currency": "GBP"}
    with pytest.raises(EditError, match="total"):
        apply_edits(result.invoice, fixed, line_values(result.invoice), project)


def test_adding_and_removing_lines_is_recorded(project):
    result = saved(project)[A.name]
    lines = line_values(result.invoice)
    lines[1]["line_total"] = "1447.01"
    _, changes = apply_edits(result.invoice, form_values(result.invoice), lines[:2], project)
    assert [(c.field, c.after) for c in changes] == [("line 2 line total", "1447.01")]
    _, changes = apply_edits(result.invoice, form_values(result.invoice), lines[:1], project)
    assert ("line_items", "2 lines", "1 lines") in [(c.field, c.before, c.after) for c in changes]


def test_recheck_finds_duplicates_of_earlier_documents(project):
    everything = load_all(project.output_path)
    copy = saved(project)["zz-copy.pdf"]
    reasons = recheck(copy.invoice, copy, everything, project, date(2026, 10, 23))
    assert [r.code for r in reasons] == ["duplicate_file", "duplicate"]

    # Giving B the same number as A makes it a duplicate of A, and not the other way round.
    b = saved(project)[B.name]
    header = {**form_values(b.invoice), "invoice_number": "INV-2026-0697", "total": "1777.20"}
    edited, _ = apply_edits(b.invoice, header, line_values(b.invoice), project)
    assert [r.code for r in recheck(edited, b, everything, project, date(2026, 10, 23))] == [
        "duplicate"
    ]
    a = saved(project)[A.name]
    assert recheck(a.invoice, a, everything, project, date(2026, 10, 23)) == []


def test_approving_saves_ok_moves_the_pdf_and_keeps_an_audit_trail(project):
    result = saved(project)[B.name]
    header = {**form_values(result.invoice), "total": "1777.20"}
    edited, changes = apply_edits(result.invoice, header, line_values(result.invoice), project)
    approve(project, result, edited, changes, open_reasons=[], note="", now=NOW)
    write_exports(project)

    after = saved(project)[B.name]
    assert after.invoice.status == Status.OK and after.invoice.reasons == []
    assert after.invoice.total == Decimal("1777.20")
    assert after.review.action == "approved" and after.review.at == NOW
    assert [(c.field, c.before, c.after) for c in after.review.changes] == [
        ("total", "1877.20", "1777.20")
    ]
    assert after.pdf_path == str(project.input_path / "processed" / B.name)
    assert Path(after.pdf_path).is_file()
    assert not (project.input_path / "needs-review" / B.name).exists()

    row = next(r for r in csv_rows(project) if r["File"] == B.name)
    assert row["Status"] == "ok" and row["Total"] == "1777.20"
    assert row["Reviewed"] == "approved 2026-10-23, edited total"


def test_approving_with_checks_still_failing_needs_a_note(project):
    result = saved(project)[C.name]
    open_reasons = recheck(result.invoice, result, load_all(project.output_path), project, TODAY)
    assert [r.code for r in open_reasons] == ["currency_not_allowed"]
    with pytest.raises(EditError, match="say why"):
        approve(project, result, result.invoice, [], open_reasons=open_reasons, note=" ", now=NOW)
    approve(
        project,
        result,
        result.invoice,
        [],
        open_reasons=open_reasons,
        note="We do pay this supplier in CHF",
        now=NOW,
    )
    after = saved(project)[C.name]
    assert after.invoice.status == Status.OK
    assert after.review.note == "We do pay this supplier in CHF"
    assert after.review.overridden == ["CHF isn't one of your allowed currencies (USD, GBP, EUR)"]
    write_exports(project)
    row = next(r for r in csv_rows(project) if r["File"] == C.name)
    assert row["Reviewed"] == "approved 2026-10-23, 1 check(s) overridden"


def test_an_approved_invoice_is_remembered_for_duplicates(project):
    result = saved(project)[C.name]
    approve(project, result, result.invoice, [], open_reasons=[], note="", now=NOW)
    assert Memory.load(project.output_path).invoices["harbourline packaging ltd|inv-3"] == C.name


def test_discarding_takes_a_document_out_of_the_sheets(project):
    copy = saved(project)["zz-copy.pdf"]
    discard(project, copy, note="same email saved twice", now=NOW)
    write_exports(project)

    after = saved(project)["zz-copy.pdf"]
    assert after.discarded and after.review.note == "same email saved twice"
    assert after.pdf_path == str(project.input_path / "needs-review" / "discarded" / "zz-copy.pdf")
    assert Path(after.pdf_path).is_file()
    assert "zz-copy.pdf" not in [r["File"] for r in csv_rows(project)]
    # Nothing is deleted: the result is still on disk.
    assert (project.output_path / "results" / after.filename()).is_file()


def test_a_missing_pdf_is_left_alone(project):
    result = saved(project)[C.name]
    Path(result.pdf_path).unlink()
    approve(project, result, result.invoice, [], open_reasons=[], note="", now=NOW)
    assert saved(project)[C.name].pdf_path == result.pdf_path


# --- the page, run headless -----------------------------------------------------------------


@pytest.fixture
def page(project, monkeypatch):
    monkeypatch.setenv("IBIS_CONFIG", str(project.base_dir / "config.yaml"))
    (project.base_dir / "config.yaml").write_text("input_dir: inbox\noutput_dir: output\n")

    def open_page():
        at = AppTest.from_file(str(APP), default_timeout=30)
        at.run()
        assert not at.exception, [e.message for e in at.exception]
        return at

    return open_page


def test_the_page_lists_what_needs_a_look(page):
    at = page()
    assert "4 documents · **3 need a look**" in [m.value for m in at.sidebar.markdown]
    assert at.sidebar.radio[1].options == [
        f"! {B.name}",
        f"! {C.name}",
        "! zz-copy.pdf",
    ]
    assert at.subheader[0].value == B.name
    assert at.warning[0].value.startswith("the total doesn't add up")
    assert [b.label for b in at.button] == ["Approve", "Discard"]


def test_fixing_a_field_on_the_page_and_approving(page, project):
    at = page()
    at.text_input(key=f"{saved(project)[B.name].filename()}-total").set_value("1777.20").run()
    assert any("Everything checks out" in m.value for m in at.markdown)
    at.button(key=f"{saved(project)[B.name].filename()}-approve").click().run()
    assert not at.exception
    assert saved(project)[B.name].invoice.status == Status.OK
    assert at.sidebar.radio[1].options == [f"! {C.name}", "! zz-copy.pdf"]


def test_approving_with_open_checks_asks_for_a_note(page, project):
    at = page()
    at.sidebar.radio[1].set_value(at.sidebar.radio[1].options[1]).run()  # C, the CHF invoice
    key = saved(project)[C.name].filename()
    at.button(key=f"{key}-approve").click().run()
    assert "say why you're approving it with checks still failing" in [e.value for e in at.error]
    assert saved(project)[C.name].invoice.status == Status.REVIEW


def test_discard_needs_a_tick_first(page, project):
    at = page()
    key = saved(project)[B.name].filename()
    assert at.button(key=f"{key}-discard").disabled
    at.checkbox(key=f"{key}-confirm").check().run()
    at.button(key=f"{key}-discard").click().run()
    assert saved(project)[B.name].discarded


def test_a_bad_value_on_the_page_is_explained(page, project):
    at = page()
    key = saved(project)[B.name].filename()
    at.text_input(key=f"{key}-invoice_date").set_value("18/08/2026").run()
    assert "invoice date '18/08/2026': use the form YYYY-MM-DD" in [e.value for e in at.error]
    assert not any(b.label == "Approve" for b in at.button)


def test_the_page_with_nothing_to_review(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("input_dir: inbox\noutput_dir: output\n")
    monkeypatch.setenv("IBIS_CONFIG", str(tmp_path / "config.yaml"))
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    assert [s.value for s in at.success] == ["Nothing needs a look."]


def test_the_page_with_a_bad_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IBIS_CONFIG", str(tmp_path / "missing.yaml"))
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    assert "config file not found" in at.error[0].value
