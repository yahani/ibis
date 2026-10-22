from datetime import date

import pytest

from ibis_docs.config import Config
from ibis_docs.models import ExtractedInvoice, Reason, Status
from ibis_docs.validate import RETRY_CODES, status_for, validate, worth_retrying

TODAY = date(2026, 10, 22)

# A clean GBP invoice: 20 x 1.70 = 34.00, 50 x 28.94 = 1,447.00; 1,481.00 + 296.20 VAT.
CLEAN = {
    "doc_type": "invoice",
    "supplier_name": "Harbourline Packaging Ltd",
    "invoice_number": "INV-2026-0697",
    "invoice_date": "2026-08-18",
    "due_date": "2026-09-17",
    "currency": "GBP",
    "subtotal": "1481.00",
    "tax": "296.20",
    "total": "1777.20",
    "line_items": [
        {"description": "Box", "quantity": "20", "unit_price": "1.70", "line_total": "34.00"},
        {
            "description": "Void fill",
            "quantity": "50",
            "unit_price": "28.94",
            "line_total": "1447.00",
        },
    ],
}


def check(config: Config | None = None, **changes) -> dict[str, str]:
    invoice = ExtractedInvoice.model_validate({**CLEAN, **changes})
    return {r.code: r.message for r in validate(invoice, config or Config(), today=TODAY)}


def lines(*rows: tuple[str, str, str]) -> list[dict]:
    return [
        {"description": f"item {i}", "quantity": q, "unit_price": p, "line_total": t}
        for i, (q, p, t) in enumerate(rows, start=1)
    ]


def test_a_clean_invoice_has_no_reasons():
    assert check() == {}


def test_line_maths():
    bad = lines(("20", "1.70", "34.00"), ("5", "12.00", "600.00"))
    assert check(line_items=bad, subtotal="634.00", tax="126.80", total="760.80") == {
        "line_math": "line 2: 5 × £12.00 = £60.00, but the line total is £600.00"  # noqa: RUF001
    }


def test_line_maths_allows_rounding_within_the_tolerance():
    rounded = lines(("3", "0.333", "1.00"))
    assert check(line_items=rounded, subtotal="1.00", tax="0.20", total="1.20") == {}


def test_lines_that_dont_add_up_to_the_subtotal():
    assert check(subtotal="1493.40", total="1789.60") == {
        "lines_vs_subtotal": "the lines add up to £1,481.00, but the subtotal is £1,493.40 "
        "(£12.40 more)"
    }


def test_a_total_that_doesnt_add_up():
    assert check(total="1877.20") == {
        "total_mismatch": "the total doesn't add up: subtotal £1,481.00 + tax £296.20 = "
        "£1,777.20, but the printed total is £1,877.20 (£100.00 more)"
    }


def test_discount_and_shipping_are_in_the_total():
    assert check(discount="74.05", shipping="12.82", total="1715.97") == {}
    assert check(discount="74.05", shipping="12.82", total="1777.20") == {
        "total_mismatch": "the total doesn't add up: subtotal £1,481.00 + tax £296.20 - "
        "discount £74.05 + shipping £12.82 = £1,715.97, but the printed total is £1,777.20 "
        "(£61.23 more)"
    }


def test_without_a_subtotal_the_lines_stand_in_for_it():
    assert check(subtotal=None) == {}
    assert check(subtotal=None, total="1700.00") == {
        "total_mismatch": "the total doesn't add up: lines £1,481.00 + tax £296.20 = "
        "£1,777.20, but the printed total is £1,700.00 (£77.20 less)"
    }


def test_missing_line_amounts_skip_the_checks_they_need():
    partial = lines(("20", None, "34.00"), ("50", "28.94", None))
    assert check(line_items=partial) == {}


def test_due_date_before_invoice_date():
    assert check(due_date="2026-08-03") == {
        "due_before_invoice": "the due date 3 Aug 2026 is before the invoice date 18 Aug 2026"
    }


def test_an_invoice_date_in_the_future():
    assert check(invoice_date="2026-10-23", due_date=None) == {}  # a day's grace
    assert check(invoice_date="2026-12-01", due_date=None) == {
        "future_date": "the invoice date 1 Dec 2026 is in the future"
    }


def test_a_currency_the_client_doesnt_take():
    assert check(currency="CHF") == {
        "currency_not_allowed": "CHF isn't one of your allowed currencies (USD, GBP, EUR)"
    }
    cfg = Config.model_validate({"validation": {"allowed_currencies": ["CHF"]}})
    assert check(cfg, currency="CHF") == {}


@pytest.mark.parametrize(
    ("field", "code", "message"),
    [
        ("supplier_name", "missing_supplier", "no supplier name on the document"),
        ("invoice_number", "missing_invoice_number", "no invoice number on the document"),
        ("invoice_date", "missing_date", "no invoice date on the document"),
        ("total", "missing_total", "no total on the document"),
        ("currency", "missing_currency", "no currency shown on the document"),
    ],
)
def test_required_fields(field, code, message):
    assert check(**{field: None}) == {code: message}


def test_a_field_already_explained_isnt_also_called_missing():
    invoice = ExtractedInvoice.model_validate({**CLEAN, "invoice_date": None, "due_date": None})
    unreadable = Reason(code="unreadable_date", message="...", field="invoice_date")
    reasons = validate(invoice, Config(), today=TODAY, issues=[unreadable])
    assert [r.code for r in reasons] == ["unreadable_date"]


def test_packing_slips_need_no_money():
    slip = {
        "doc_type": "packing_slip",
        "supplier_name": "Saltmarsh Candle Works",
        "invoice_number": "PS-SCW-0215",
        "invoice_date": "2026-08-14",
        "line_items": [{"description": "Wax", "quantity": "3"}],
    }
    invoice = ExtractedInvoice.model_validate(slip)
    assert validate(invoice, Config(), today=TODAY) == []
    no_number = ExtractedInvoice.model_validate({**slip, "invoice_number": None})
    assert [r.message for r in validate(no_number, Config(), today=TODAY)] == [
        "no slip number on the document"
    ]


def test_credit_notes():
    credit = {
        "doc_type": "credit_note",
        "subtotal": "-1481.00",
        "tax": "-296.20",
        "total": "-1777.20",
        "due_date": None,
        "line_items": lines(("-20", "1.70", "-34.00"), ("-50", "28.94", "-1447.00")),
    }
    assert check(**credit) == {}
    positive = {**credit, "subtotal": "1481.00", "tax": "296.20", "total": "1777.20"}
    positive["line_items"] = lines(("20", "1.70", "34.00"), ("50", "28.94", "1447.00"))
    assert check(**positive) == {
        "credit_sign": "credit note with a positive total (£1,777.20); check the sign"
    }


def test_reading_issues_come_first():
    invoice = ExtractedInvoice.model_validate({**CLEAN, "total": "1877.20"})
    issue = Reason(code="ambiguous_date", message="...", field="due_date")
    codes = [r.code for r in validate(invoice, Config(), today=TODAY, issues=[issue])]
    assert codes == ["ambiguous_date", "total_mismatch"]


def test_status():
    assert status_for([]) == Status.OK
    assert status_for([Reason(code="x", message="y")]) == Status.REVIEW
    assert status_for([], failed=True) == Status.FAILED


def test_what_is_worth_a_retry():
    def reasons(*codes):
        return [Reason(code=c, message="") for c in codes]

    assert worth_retrying(reasons("total_mismatch"))
    assert worth_retrying(reasons("duplicate", "unreadable_amount"))
    # A stronger model would come to the same answer on these.
    for code in ("duplicate", "duplicate_file", "currency_not_allowed", "ambiguous_date"):
        assert code not in RETRY_CODES
        assert not worth_retrying(reasons(code))
