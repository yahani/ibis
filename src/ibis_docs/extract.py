"""Read a PDF with Claude (E3).

Claude only reads: it copies what is printed into `RawInvoice`, as text. Python then reads the
amounts, dates and currency (`to_extracted`), noting anything it couldn't read as an issue rather
than guessing. Checking that the numbers add up is validation's job (E5).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import anthropic
from pydantic import ValidationError

from ibis_docs.config import Config
from ibis_docs.dates import Order, infer_order, is_ambiguous, parse_date
from ibis_docs.models import (
    ExtractedInvoice,
    LineItem,
    RawInvoice,
    RawLineItem,
)
from ibis_docs.money import normalise_currency, parse_amount

# Symbols used by more than one currency; a supplier hint in config decides which.
SHARED_SYMBOLS = frozenset({"$", "kr", "¥", "R"})

# A 20-page invoice with ~50 lines a page fits comfortably; non-streaming stays under timeouts.
MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You read supplier invoices, credit notes and packing slips, and copy what is printed on them \
into the given JSON schema. Your output is checked by software and then by a person, so an \
honest empty field is always better than a plausible guess.

Rules:
- Copy every value exactly as printed. Keep amounts as text with their thousands separators, \
decimal marks, signs, brackets and currency symbols, e.g. "1.299,00 €", "(12.00)", "-45.00". \
Keep dates exactly as printed, e.g. "13/09/2026" or "18 August 2026".
- Never guess or work anything out. If a field isn't printed on the document, return an empty \
string "". Don't \
calculate a missing subtotal, tax or total, don't turn a tax rate into an amount, don't convert \
currencies or date formats, and don't derive a due date from payment terms such as "Net 30".
- Don't correct the document. If its numbers don't add up, copy them as printed anyway.
- If a value is printed but you can't read it with confidence (blurred, cut off, smudged), return \
an empty string rather than your best reading.
- Include every line item from every page, in order. Subtotal, tax, discount, shipping and total \
rows are not line items.
- The supplier is the business that issued the document, not the customer it is addressed to."""

# Fields read as amounts, with the plain names used in issues.
_AMOUNTS = {
    "subtotal": "subtotal",
    "tax": "tax",
    "discount": "discount",
    "shipping": "shipping",
    "total": "total",
}
_LINE_AMOUNTS = {
    "quantity": "quantity",
    "unit_price": "unit price",
    "line_total": "line total",
    "tax_rate": "tax rate",
}


class ApiSetupError(Exception):
    """The API can't be used at all (no key, bad key, unknown model): stop the run."""


@dataclass
class Extraction:
    """What one model call produced for one file."""

    file: str
    model: str
    invoice: ExtractedInvoice | None = None
    raw: RawInvoice | None = None
    issues: list[str] = field(default_factory=list)  # values that couldn't be read cleanly
    error: str | None = None  # why nothing usable came back
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and self.invoice is not None


def make_client() -> anthropic.Anthropic:
    """A client using credentials from the environment (ANTHROPIC_API_KEY, or a federation or
    `ant auth login` profile). Never a key from config."""
    try:
        return anthropic.Anthropic()
    except anthropic.CredentialsError as exc:
        raise _no_credentials(exc) from exc


def build_request(pdf: bytes, model: str, config: Config) -> dict:
    """The keyword arguments for `messages.parse`, without the output format."""
    text = "Read this document and fill in the schema."
    notes = [f"- {s.name}: {s.notes}" for s in config.suppliers if s.notes]
    if notes:
        text += (
            "\n\nLayout notes for known suppliers (use only if this document is from one):\n"
            + "\n".join(notes)
        )
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": text},
                ],
            }
        ],
    }


def extract(client: anthropic.Anthropic, path: Path, model: str, config: Config) -> Extraction:
    """Send one PDF to `model` and read the result. API problems with this one document come back
    as `Extraction.error`; problems that would hit every document raise ApiSetupError."""
    result = Extraction(file=path.name, model=model)
    request = build_request(path.read_bytes(), model, config)

    try:
        response = client.messages.parse(**request, output_format=RawInvoice)
    except TypeError as exc:
        if "authentication" in str(exc).lower():
            raise ApiSetupError(
                "no API credentials: set ANTHROPIC_API_KEY in .env (see .env.example)"
            ) from exc
        raise
    except anthropic.CredentialsError as exc:
        raise _no_credentials(exc) from exc
    except anthropic.AuthenticationError as exc:
        raise ApiSetupError("the API key was rejected: check ANTHROPIC_API_KEY in .env") from exc
    except anthropic.PermissionDeniedError as exc:
        raise ApiSetupError(f"the API key can't use {model}: {_api_message(exc)}") from exc
    except anthropic.NotFoundError as exc:
        raise ApiSetupError(f"unknown model {model!r}: check model in config.yaml") from exc
    except ValidationError:
        # parse() validates the reply itself; a refusal or a cut-off reply isn't valid JSON.
        result.error = "Claude's reply didn't match the invoice schema (refused or cut off)"
        return result
    except anthropic.RateLimitError:
        result.error = "rate limited by the API after retries; run again later"
        return result
    except anthropic.APIStatusError as exc:
        result.error = f"the API returned an error ({exc.status_code}): {_api_message(exc)}"
        return result
    except anthropic.APIConnectionError:
        result.error = "couldn't reach the API (network problem or timeout)"
        return result

    usage = response.usage
    result.input_tokens = (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
    )
    result.output_tokens = usage.output_tokens

    if response.stop_reason == "refusal":
        result.error = "Claude declined to read this document"
        return result
    if response.stop_reason == "max_tokens":
        result.error = "the reply was cut off (document too long for one request)"
        return result
    if response.parsed_output is None:
        result.error = "Claude returned no data for this document"
        return result

    result.raw = response.parsed_output
    result.invoice, result.issues = to_extracted(result.raw, config)
    return result


def _no_credentials(exc: Exception) -> ApiSetupError:
    return ApiSetupError(f"no usable API credentials: set ANTHROPIC_API_KEY in .env ({exc})")


def _api_message(exc: anthropic.APIStatusError) -> str:
    """The API's own error message, without the SDK's 'Error code: ...' wrapping."""
    body = exc.body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message") or exc.message)
    return exc.message


def to_extracted(raw: RawInvoice, config: Config) -> tuple[ExtractedInvoice, list[str]]:
    """Read the printed text into typed values. Anything unreadable becomes None plus an issue."""
    issues: list[str] = []

    currency = _currency(raw, config, issues)

    order, assumed = infer_order([raw.invoice_date, raw.due_date], currency)
    dates: dict[str, date | None] = {}
    for name, label in (("invoice_date", "invoice date"), ("due_date", "due date")):
        text = getattr(raw, name)
        try:
            dates[name] = parse_date(text, order)
        except ValueError:
            dates[name] = None
            issues.append(f"{label} '{text}' isn't a readable date")
            continue
        if assumed and is_ambiguous(text):
            first = "month" if order == Order.MONTH_FIRST else "day"
            issues.append(
                f"{label} '{text}' could be day/month or month/day; read as "
                f"{dates[name]:%d %b %Y} ({first} first, assumed from the currency)"
            )

    amounts = {name: _amount(getattr(raw, name), label, issues) for name, label in _AMOUNTS.items()}
    lines = [_line(i, item, issues) for i, item in enumerate(raw.line_items, start=1)]

    invoice = ExtractedInvoice(
        doc_type=raw.doc_type,
        supplier_name=raw.supplier_name,
        supplier_tax_id=raw.supplier_tax_id,
        invoice_number=raw.invoice_number,
        invoice_date=dates["invoice_date"],
        due_date=dates["due_date"],
        currency=currency,
        po_number=raw.po_number,
        line_items=lines,
        **amounts,
    )
    return invoice, issues


def _currency(raw: RawInvoice, config: Config, issues: list[str]) -> str | None:
    printed = (raw.currency or "").strip()
    hint = _supplier_currency(raw.supplier_name, config)
    if not printed:
        return None
    code = normalise_currency(printed, config.validation.symbols)
    if code is None:
        issues.append(f"currency '{printed}' isn't a known code or symbol")
        return None
    if hint and code != hint:
        # A symbol shared by several currencies ("$") is settled by a known supplier's hint.
        if printed in SHARED_SYMBOLS:
            return hint
        issues.append(f"currency printed as '{printed}' ({code}), but config says {hint}")
    return code


def _supplier_currency(name: str | None, config: Config) -> str | None:
    if not name:
        return None
    key = name.casefold().strip()
    for supplier in config.suppliers:
        if supplier.currency and supplier.name.casefold() == key:
            return supplier.currency
    return None


def _amount(text: str | None, label: str, issues: list[str]) -> Decimal | None:
    try:
        return parse_amount(text)
    except ValueError:
        issues.append(f"{label} '{text}' isn't a readable amount")
        return None


def _line(number: int, item: RawLineItem, issues: list[str]) -> LineItem:
    description = item.description.strip()
    if not description:
        issues.append(f"line {number} has no description")
    return LineItem(
        description=description or f"(line {number}: no description printed)",
        sku=item.sku,
        **{
            name: _amount(getattr(item, name), f"line {number} {label}", issues)
            for name, label in _LINE_AMOUNTS.items()
        },
    )
