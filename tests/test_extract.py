import base64
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from ibis_docs.config import Config
from ibis_docs.extract import SYSTEM_PROMPT, ApiSetupError, extract, make_client, to_extracted
from ibis_docs.models import DocType, RawInvoice
from tests.fakes import FakeClaude, error_response, message

PDF = Path(__file__).resolve().parent.parent / "demo" / "invoices" / "001-inv-classic.pdf"
HAIKU = "claude-haiku-5-5"

# A reply as Claude is asked to give it: everything as printed, absent fields null.
REPLY = {
    "doc_type": "invoice",
    "supplier_name": "Harbourline Packaging Ltd",
    "supplier_tax_id": "GB861142741",
    "invoice_number": "INV-2026-0697",
    "invoice_date": "18 August 2026",
    "due_date": None,
    "currency": "£",
    "po_number": None,
    "subtotal": "£1,510.72",
    "tax": "£302.14",
    "discount": None,
    "shipping": None,
    "total": "£1,812.86",
    "line_items": [
        {
            "description": "Double-wall shipping box 40x30x20",
            "sku": "BX-964",
            "quantity": "20",
            "unit_price": "1.70",
            "line_total": "34.00",
            "tax_rate": None,
        },
        {
            "description": "Paper void fill roll 450mm",
            "sku": None,
            "quantity": "50",
            "unit_price": "28.94",
            "line_total": "1,447.00",
            "tax_rate": "20%",
        },
    ],
}


def reply(**changes) -> dict:
    return {**REPLY, **changes}


def config(**changes) -> Config:
    return Config.model_validate(changes)


def run(*responses, cfg: Config | None = None):
    fake = FakeClaude(*responses)
    return extract(fake.client, PDF, HAIKU, cfg or config()), fake


# --- the request ---------------------------------------------------------------------------


def test_request_sends_the_pdf_with_the_schema_and_no_guessing_prompt():
    result, fake = run(message(REPLY))
    assert result.ok
    (sent,) = fake.requests
    assert sent["model"] == HAIKU
    assert sent["system"] == SYSTEM_PROMPT
    assert "Never guess" in SYSTEM_PROMPT and "return null" in SYSTEM_PROMPT

    document, text = sent["messages"][0]["content"]
    assert document["type"] == "document"
    assert document["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(document["source"]["data"]) == PDF.read_bytes()
    assert text["type"] == "text"

    schema = sent["output_config"]["format"]
    assert schema["type"] == "json_schema"
    props = schema["schema"]["properties"]
    # Amounts and dates go to Claude as text, so they come back exactly as printed.
    assert props["total"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert props["invoice_date"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "thinking" not in sent and "temperature" not in sent


def test_supplier_notes_go_to_claude_but_only_when_set():
    cfg = config(suppliers=[{"name": "Acme", "notes": "The total is bottom left"}, {"name": "B"}])
    _, fake = run(message(REPLY), cfg=cfg)
    text = fake.requests[0]["messages"][0]["content"][1]["text"]
    assert "- Acme: The total is bottom left" in text
    assert "- B" not in text

    _, fake = run(message(REPLY))
    assert "Layout notes" not in fake.requests[0]["messages"][0]["content"][1]["text"]


# --- reading the reply ---------------------------------------------------------------------


def test_reply_is_read_into_typed_values():
    result, _ = run(message(REPLY, input_tokens=5210, output_tokens=812, cache_read=100))
    assert result.ok and result.issues == []
    assert result.model == HAIKU and result.file == PDF.name
    assert (result.input_tokens, result.output_tokens) == (5310, 812)

    inv = result.invoice
    assert inv.doc_type == DocType.INVOICE
    assert inv.invoice_date == date(2026, 8, 18)
    assert inv.currency == "GBP"
    assert (inv.subtotal, inv.tax, inv.total) == (
        Decimal("1510.72"),
        Decimal("302.14"),
        Decimal("1812.86"),
    )
    # Not printed stays empty, never zero.
    assert inv.due_date is None and inv.discount is None and inv.shipping is None
    second = inv.line_items[1]
    assert second.line_total == Decimal("1447.00")
    assert second.tax_rate == Decimal("20")
    assert second.sku is None
    # The raw reply is kept, so every number can be traced to the printed text.
    assert result.raw.total == "£1,812.86"


def test_european_amounts_and_credit_note_signs():
    raw = RawInvoice.model_validate(
        reply(
            doc_type="credit_note",
            currency="€",
            subtotal="-1.299,00 €",
            tax="(246,81)",
            total="1.545,81-",
            invoice_date="26.08.2026",
        )
    )
    inv, issues = to_extracted(raw, config())
    assert issues == []
    assert inv.doc_type == DocType.CREDIT_NOTE
    assert inv.currency == "EUR"
    assert (inv.subtotal, inv.tax, inv.total) == (
        Decimal("-1299.00"),
        Decimal("-246.81"),
        Decimal("-1545.81"),
    )
    assert inv.invoice_date == date(2026, 8, 26)


def test_packing_slip_without_prices():
    lines = [{"description": "Kraft mailer bag", "quantity": "12"}]
    raw = RawInvoice.model_validate(
        {"doc_type": "packing_slip", "invoice_number": "PS-1", "line_items": lines}
    )
    inv, issues = to_extracted(raw, config())
    assert issues == []
    assert inv.total is None and inv.currency is None
    assert inv.line_items[0].quantity == Decimal("12")
    assert inv.line_items[0].unit_price is None


def test_dates_use_the_documents_own_order():
    raw = RawInvoice.model_validate(reply(invoice_date="09/20/2026", due_date="10/05/2026"))
    inv, issues = to_extracted(raw, config())
    assert (inv.invoice_date, inv.due_date) == (date(2026, 9, 20), date(2026, 10, 5))
    assert issues == []


def test_ambiguous_dates_are_read_by_currency_and_noted():
    raw = RawInvoice.model_validate(reply(invoice_date="03/09/2026", currency="$"))
    inv, issues = to_extracted(raw, config())
    assert inv.invoice_date == date(2026, 3, 9)
    assert issues == [
        "invoice date '03/09/2026' could be day/month or month/day; read as "
        "09 Mar 2026 (month first, assumed from the currency)"
    ]

    inv, issues = to_extracted(
        RawInvoice.model_validate(reply(invoice_date="03/09/2026")), config()
    )
    assert inv.invoice_date == date(2026, 9, 3)
    assert "day first" in issues[0]


def test_unreadable_values_become_empty_with_an_issue():
    lines = [{"description": "  ", "quantity": "a few", "line_total": "34.00"}]
    raw = RawInvoice.model_validate(
        reply(total="see attached", invoice_date="soon", currency="dubloons", line_items=lines)
    )
    inv, issues = to_extracted(raw, config())
    assert inv.total is None and inv.invoice_date is None and inv.currency is None
    assert inv.line_items[0].quantity is None
    assert inv.line_items[0].description == "(line 1: no description printed)"
    assert issues == [
        "currency 'dubloons' isn't a known code or symbol",
        "invoice date 'soon' isn't a readable date",
        "total 'see attached' isn't a readable amount",
        "line 1 has no description",
        "line 1 quantity 'a few' isn't a readable amount",
    ]


def test_currency_symbols_from_config_and_supplier_hints():
    raw = RawInvoice.model_validate(reply(currency="$", supplier_name="Outback Supplies"))
    # A bare "$" is USD by default...
    assert to_extracted(raw, config())[0].currency == "USD"
    # ...a client's symbol table can remap it...
    remapped = config(validation={"allowed_currencies": ["AUD"], "currency_symbols": {"$": "AUD"}})
    assert to_extracted(raw, remapped)[0].currency == "AUD"
    # ...and a known supplier's hint settles a shared symbol.
    hinted = config(
        validation={"allowed_currencies": ["AUD", "USD"]},
        suppliers=[{"name": "outback supplies", "currency": "AUD"}],
    )
    inv, issues = to_extracted(raw, hinted)
    assert inv.currency == "AUD" and issues == []


def test_a_printed_code_that_contradicts_the_hint_is_kept_and_noted():
    raw = RawInvoice.model_validate(reply(currency="EUR"))
    cfg = config(suppliers=[{"name": "Harbourline Packaging Ltd", "currency": "GBP"}])
    inv, issues = to_extracted(raw, cfg)
    assert inv.currency == "EUR"
    assert issues == ["currency printed as 'EUR' (EUR), but config says GBP"]


# --- when things go wrong ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            message("I can't help with that document.", stop_reason="refusal"),
            "Claude's reply didn't match the invoice schema (refused or cut off)",
        ),
        (
            message('{"doc_type": "invoice", "line_items": [{"descr', stop_reason="max_tokens"),
            "Claude's reply didn't match the invoice schema (refused or cut off)",
        ),
        (message(REPLY, stop_reason="refusal"), "Claude declined to read this document"),
        (
            message(REPLY, stop_reason="max_tokens"),
            "the reply was cut off (document too long for one request)",
        ),
        (message(""), "Claude returned no data for this document"),
        (error_response(429), "rate limited by the API after retries; run again later"),
        (error_response(500, "overloaded"), "the API returned an error (500): overloaded"),
        (
            error_response(400, "Could not process PDF"),
            "the API returned an error (400): Could not process PDF",
        ),
        (
            httpx2.ConnectError("no route"),
            "couldn't reach the API (network problem or timeout)",
        ),
    ],
)
def test_problems_with_one_document_come_back_as_an_error(response, error):
    result, _ = run(response)
    assert not result.ok
    assert result.error == error
    assert result.invoice is None


@pytest.mark.parametrize(
    ("status", "message_part"),
    [
        (401, "the API key was rejected"),
        (403, "can't use claude-haiku-5-5"),
        (404, "unknown model 'claude-haiku-5-5'"),
    ],
)
def test_problems_that_hit_every_document_stop_the_run(status, message_part):
    with pytest.raises(ApiSetupError, match=message_part):
        run(error_response(status))


def test_missing_credentials_say_where_the_key_goes(monkeypatch):
    def no_auth(**_):
        # What the SDK raises when no key, token or profile is set.
        raise TypeError("Could not resolve authentication method. Expected one of api_key, ...")

    client = make_client()
    monkeypatch.setattr(client.messages, "parse", no_auth)
    with pytest.raises(ApiSetupError, match=r"no API credentials: set ANTHROPIC_API_KEY in \.env"):
        extract(client, PDF, HAIKU, config())


def test_a_broken_login_profile_says_where_the_key_goes(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_PROFILE", "missing")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ApiSetupError, match=r"no usable API credentials: set ANTHROPIC_API_KEY"):
        make_client()


def test_other_type_errors_are_not_hidden():
    class Broken:
        class messages:
            @staticmethod
            def parse(**_):
                raise TypeError("a bug")

    with pytest.raises(TypeError, match="a bug"):
        extract(Broken(), PDF, HAIKU, config())
