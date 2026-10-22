"""The demo set and its answer key: clean documents add up, broken ones break as described."""

import hashlib
import importlib.util
import json
import sys
from decimal import Decimal

import pytest

from ibis_docs.models import ExtractedInvoice
from tests.conftest import ROOT

DEMO = ROOT / "demo"
KEY = json.loads((DEMO / "answers.json").read_text(encoding="utf-8"))
DOCS = KEY["documents"]
CLEAN = [d for d in DOCS if not d["expected_reasons"]]
BROKEN = {d["expected_reasons"][0]: d for d in DOCS if d["expected_reasons"]}


def _load_generator():
    spec = importlib.util.spec_from_file_location("demo_generate", DEMO / "generate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


def inv(doc: dict) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate(doc["fields"])


def lines_ok(i: ExtractedInvoice) -> bool:
    return all(li.quantity * li.unit_price == li.line_total for li in i.line_items)


def lines_sum_ok(i: ExtractedInvoice) -> bool:
    return sum(li.line_total for li in i.line_items) == i.subtotal


def total_ok(i: ExtractedInvoice) -> bool:
    expected = i.subtotal + (i.tax or 0) - (i.discount or 0) + (i.shipping or 0)
    return expected == i.total


def test_every_pdf_is_in_the_key_and_back():
    on_disk = {p.name for p in (DEMO / "invoices").glob("*.pdf")}
    assert on_disk == {d["file"] for d in DOCS}


def test_set_size_and_variety():
    assert len(CLEAN) >= 30
    assert len({d["layout"] for d in DOCS}) == 6
    assert {d["fields"]["doc_type"] for d in DOCS} == {"invoice", "packing_slip", "credit_note"}
    assert set(KEY["allowed_currencies"]) == {"USD", "GBP", "EUR"}
    assert {d["fields"]["currency"] for d in CLEAN} - {None} == {"USD", "GBP", "EUR"}


def test_set_has_missing_optional_fields():
    invoices = [inv(d) for d in CLEAN if d["fields"]["doc_type"] == "invoice"]
    assert any(i.po_number is None for i in invoices)
    assert any(i.due_date is None for i in invoices)
    assert any(i.shipping is None for i in invoices) and any(i.shipping for i in invoices)
    assert any(i.discount for i in invoices)
    assert any(i.supplier_tax_id for i in invoices)


def test_multi_page_invoices_exist():
    assert sum(1 for d in DOCS if d["note"].startswith("multi-page")) >= 2


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d["file"])
def test_key_matches_the_schema(doc):
    inv(doc)  # raises if a field isn't a valid amount, date or currency


@pytest.mark.parametrize(
    "doc", [d for d in CLEAN if d["fields"]["doc_type"] != "packing_slip"], ids=lambda d: d["file"]
)
def test_clean_documents_add_up(doc):
    i = inv(doc)
    assert lines_ok(i) and lines_sum_ok(i) and total_ok(i)
    assert i.invoice_number
    assert i.currency in KEY["allowed_currencies"]
    assert i.due_date is None or i.due_date >= i.invoice_date


def test_credit_notes_are_negative():
    notes = [inv(d) for d in CLEAN if d["fields"]["doc_type"] == "credit_note"]
    assert notes
    for n in notes:
        assert n.total < 0
        assert all(li.quantity < 0 for li in n.line_items)


def test_packing_slips_have_no_prices():
    slips = [inv(d) for d in CLEAN if d["fields"]["doc_type"] == "packing_slip"]
    assert slips
    for s in slips:
        assert s.total is None and s.currency is None
        assert all(li.unit_price is None and li.quantity for li in s.line_items)


def test_every_broken_kind_is_present():
    assert set(BROKEN) == {
        "line_math", "lines_vs_subtotal", "total_mismatch", "due_before_invoice",
        "missing_invoice_number", "currency_not_allowed", "duplicate", "duplicate_file",
        "hard_to_read",
    }  # fmt: skip


def test_broken_line_math():
    i = inv(BROKEN["line_math"])
    assert not lines_ok(i)
    assert lines_sum_ok(i) and total_ok(i)  # only the one fault


def test_broken_lines_vs_subtotal():
    i = inv(BROKEN["lines_vs_subtotal"])
    assert lines_ok(i) and not lines_sum_ok(i) and total_ok(i)
    assert i.subtotal - sum(li.line_total for li in i.line_items) == Decimal("12.40")


def test_broken_total():
    i = inv(BROKEN["total_mismatch"])
    assert lines_ok(i) and lines_sum_ok(i) and not total_ok(i)


def test_broken_due_date():
    i = inv(BROKEN["due_before_invoice"])
    assert i.due_date < i.invoice_date
    assert lines_ok(i) and lines_sum_ok(i) and total_ok(i)


def test_broken_missing_number():
    i = inv(BROKEN["missing_invoice_number"])
    assert i.invoice_number is None
    assert lines_ok(i) and lines_sum_ok(i) and total_ok(i)


def test_broken_currency():
    i = inv(BROKEN["currency_not_allowed"])
    assert i.currency not in KEY["allowed_currencies"]
    assert lines_ok(i) and lines_sum_ok(i) and total_ok(i)


def test_duplicate_matches_an_earlier_invoice():
    dup = BROKEN["duplicate"]
    earlier = [d for d in DOCS if d["file"] < dup["file"] and d is not dup]
    assert any(
        d["fields"]["supplier_name"] == dup["fields"]["supplier_name"]
        and d["fields"]["invoice_number"] == dup["fields"]["invoice_number"]
        for d in earlier
    )
    original = next(d for d in earlier if d["fields"] == dup["fields"])
    assert _sha(original["file"]) != _sha(dup["file"])  # same invoice, different bytes


def test_duplicate_file_is_byte_identical():
    dup = BROKEN["duplicate_file"]
    assert any(_sha(d["file"]) == _sha(dup["file"]) for d in DOCS if d is not dup)


def test_scan_is_flag_or_correct():
    scan = BROKEN["hard_to_read"]
    assert scan["expected_status"] == "ok_or_review"
    i = inv(scan)
    assert lines_ok(i) and lines_sum_ok(i) and total_ok(i)


def _sha(name: str) -> str:
    return hashlib.sha256((DEMO / "invoices" / name).read_bytes()).hexdigest()


def test_committed_set_matches_the_generator(tmp_path):
    """The PDFs in the repo are exactly what generate.py makes (no stale files)."""
    gen = _load_generator()
    gen.generate(tmp_path, ["USD", "GBP", "EUR"])
    assert json.loads((tmp_path / "answers.json").read_text()) == KEY
    for d in DOCS:
        new = (tmp_path / "invoices" / d["file"]).read_bytes()
        if "hard_to_read" in d["expected_reasons"]:
            # Rasterising and JPEG encoding differ slightly between platforms; check its shape.
            assert new.startswith(b"%PDF") and b"/DCTDecode" in new and new.count(b"/Page\n") <= 1
            continue
        assert new == (DEMO / "invoices" / d["file"]).read_bytes(), d["file"]


def test_other_currencies_plug_in(tmp_path):
    gen = _load_generator()
    answers = gen.generate(tmp_path, ["aud", "JPY", "NZD"])
    key = json.loads((tmp_path / "answers.json").read_text())
    assert key["allowed_currencies"] == ["AUD", "JPY", "NZD"]
    clean = [a for a in answers if not a["expected_reasons"]]
    assert {a["fields"]["currency"] for a in clean} - {None} == {"AUD", "JPY", "NZD"}
    for a in clean:
        i = ExtractedInvoice.model_validate(a["fields"])
        if i.currency == "JPY":
            assert i.total == i.total.to_integral_value()  # yen has no decimals
        if i.total is not None:
            assert lines_ok(i) and lines_sum_ok(i) and total_ok(i)
    # Unknown-to-the-generator currencies still render, printed with their code.
    gen.generate(tmp_path / "x", ["ZAR"])
    assert (tmp_path / "x" / "answers.json").is_file()


@pytest.mark.parametrize("bad", [[], [" "], ["EURO"], ["U5D"], ["XYZ"]])
def test_generator_rejects_bad_currency_lists(tmp_path, bad):
    with pytest.raises(ValueError):
        _load_generator().generate(tmp_path, bad)
