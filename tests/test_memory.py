import json

from ibis_docs.dates import Order
from ibis_docs.memory import Memory, invoice_key
from ibis_docs.models import ExtractedInvoice


def test_invoice_key_compares_loosely():
    a = ExtractedInvoice(supplier_name="Larchmont Labels Inc.", invoice_number="INV 0697")
    b = ExtractedInvoice(supplier_name="larchmont labels inc", invoice_number="inv0697")
    assert invoice_key(a) == invoice_key(b) == "larchmont labels inc|inv0697"
    assert invoice_key(ExtractedInvoice(supplier_name="X")) is None
    assert invoice_key(ExtractedInvoice(invoice_number="1")) is None


def test_memory_round_trip(tmp_path):
    assert Memory.load(tmp_path) == Memory()
    memory = Memory(
        invoices={"larchmont labels inc|000442": "003-inv-us_sales.pdf"},
        date_orders={"copperleaf print co": Order.DAY_FIRST},
    )
    memory.save(tmp_path)
    assert Memory.load(tmp_path) == memory
    # Names, numbers and orders only: no amounts or invoice contents.
    saved = json.loads((tmp_path / ".ibis" / "invoices.json").read_text())
    assert saved == {"larchmont labels inc|000442": "003-inv-us_sales.pdf"}
