from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ibis_docs.models import (
    DocType,
    ExtractedInvoice,
    Invoice,
    LineItem,
    SourceFile,
    Status,
)

HASH = "a" * 64


def extracted(**overrides) -> ExtractedInvoice:
    data = {
        "supplier_name": "Acme Packaging Ltd",
        "invoice_number": "INV-1001",
        "invoice_date": "2026-10-01",
        "currency": "£",
        "subtotal": "1,299.00",
        "tax": "259.80",
        "total": "£1,558.80",
        "line_items": [
            {
                "description": "Boxes",
                "quantity": "100",
                "unit_price": "12.99",
                "line_total": "1,299.00",
            }
        ],
    }
    return ExtractedInvoice.model_validate({**data, **overrides})


def test_amounts_become_exact_decimals():
    inv = extracted()
    assert inv.subtotal == Decimal("1299.00")
    assert inv.total == Decimal("1558.80")
    assert isinstance(inv.line_items[0].unit_price, Decimal)


def test_currency_symbol_becomes_code():
    assert extracted().currency == "GBP"
    assert extracted(currency="eur").currency == "EUR"


def test_unknown_currency_is_rejected():
    with pytest.raises(ValidationError, match="not a currency"):
        extracted(currency="zorkmids")


def test_missing_values_stay_empty_not_zero():
    inv = extracted(discount="", shipping=None)
    assert inv.discount is None
    assert inv.shipping is None
    assert inv.due_date is None
    assert inv.po_number is None


def test_blank_text_and_currency_are_none():
    inv = extracted(po_number="  ", currency="", due_date="")
    assert inv.po_number is None
    assert inv.currency is None
    assert inv.due_date is None


def test_dates_parse():
    inv = extracted(due_date="2026-10-31")
    assert inv.invoice_date == date(2026, 10, 1)
    assert inv.due_date == date(2026, 10, 31)


def test_bad_amount_is_a_validation_error():
    with pytest.raises(ValidationError, match="not an amount"):
        extracted(total="lots")
    with pytest.raises(ValidationError, match="not an amount"):
        extracted(total=["1"])


def test_credit_note_negative_amounts():
    inv = extracted(doc_type="credit_note", total="(51.77)", subtotal="-51.77")
    assert inv.doc_type is DocType.CREDIT_NOTE
    assert inv.total == Decimal("-51.77")


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError, match="Extra inputs"):
        extracted(colour="red")


def test_packing_slip_line_without_prices():
    item = LineItem(description="Mailer bags", sku="MB-10", quantity="250")
    assert item.unit_price is None and item.line_total is None


def test_invoice_from_extracted_defaults_to_review():
    inv = Invoice.from_extracted(extracted(), SourceFile(name="a.pdf", sha256=HASH), "m")
    assert inv.status is Status.REVIEW  # nothing is ok until validation says so
    assert inv.reasons == []
    assert inv.model == "m"
    assert inv.total == Decimal("1558.80")


def test_source_file_needs_sha256():
    with pytest.raises(ValidationError):
        SourceFile(name="a.pdf", sha256="nope")


def test_json_schema_for_api_has_no_pipeline_fields():
    props = ExtractedInvoice.model_json_schema()["properties"]
    assert "status" not in props and "reasons" not in props and "model" not in props
    assert "line_items" in props
