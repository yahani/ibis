"""Invoice and line-item schemas.

One model drives the API schema (session 2), validation (session 3) and export (session 4).
`ExtractedInvoice` holds only what is printed on the document, so it is what Claude is asked to
fill. `Invoice` adds what the pipeline decides: source file, status, reasons, model.

Money is Decimal, never float. Anything the document doesn't show stays None, never zero.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ibis_docs.money import normalise_currency, parse_amount

# A plain machine-written number ("28.945", "-1777.20"), as Decimal is saved in JSON. Read exactly:
# a dot here is always the decimal point, never a thousands separator.
_CANONICAL = re.compile(r"-?\d+(\.\d+)?")


def _amount(value: object) -> Decimal | None:
    if isinstance(value, str) and _CANONICAL.fullmatch(value.strip()):
        return Decimal(value.strip())
    if isinstance(value, str | int | float | Decimal) or value is None:
        return parse_amount(value)
    raise ValueError(f"not an amount: {value!r}")


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


Money = Annotated[Decimal | None, BeforeValidator(_amount)]
OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
OptionalDate = Annotated[date | None, BeforeValidator(_blank_to_none)]


class DocType(StrEnum):
    INVOICE = "invoice"
    PACKING_SLIP = "packing_slip"
    CREDIT_NOTE = "credit_note"
    OTHER = "other"


class Status(StrEnum):
    OK = "ok"
    REVIEW = "review"
    FAILED = "failed"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LineItem(_Model):
    description: str = Field(min_length=1)
    sku: OptionalText = None
    quantity: Money = None
    unit_price: Money = None
    line_total: Money = None
    tax_rate: Money = Field(default=None, description="Percent, e.g. 20 for 20% VAT")


class ExtractedInvoice(_Model):
    """Fields as printed on the document. Nothing here is computed or guessed."""

    doc_type: DocType = DocType.INVOICE
    supplier_name: OptionalText = None
    supplier_tax_id: OptionalText = None
    invoice_number: OptionalText = None
    invoice_date: OptionalDate = None
    due_date: OptionalDate = None
    currency: OptionalText = Field(default=None, description="ISO 4217 code, e.g. GBP")
    po_number: OptionalText = None
    subtotal: Money = None
    tax: Money = None
    discount: Money = None
    shipping: Money = None
    total: Money = None
    line_items: list[LineItem] = Field(default_factory=list)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_code(cls, value: object) -> object:
        # Accept a symbol or code here; whether it's *allowed* is a validation (E5) question.
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str):
            code = normalise_currency(value)
            if code is None:
                raise ValueError(f"not a currency code or symbol: {value!r}")
            return code
        return value


# What Claude fills (E3). Amounts, dates and the currency are copied as printed, as text, and
# read in Python (money.py, dates.py), so the model never does arithmetic or format conversion.
# Field descriptions are part of the JSON schema Claude sees.
#
# Every field is a required plain string, with "" for "not printed": the API rejects a schema with
# this many optional or `string | null` fields as too complex. Python reads "" as empty (None).

NOT_PRINTED = 'Empty string "" if not printed.'
PRINTED = "Exactly as printed, including any sign, brackets or symbol. " + NOT_PRINTED


class RawLineItem(_Model):
    description: str = Field(
        description="The line's description as printed, leaving out the item code if it goes in sku"
    )
    sku: str = Field(description="Item code or SKU. " + NOT_PRINTED)
    quantity: str = Field(description=PRINTED)
    unit_price: str = Field(description=PRINTED)
    line_total: str = Field(description=PRINTED)
    tax_rate: str = Field(description="This line's tax rate as printed, e.g. '20%'. " + NOT_PRINTED)


class RawInvoice(_Model):
    doc_type: DocType = Field(
        description="invoice, credit_note (credits money back), packing_slip (no prices), or other"
    )
    supplier_name: str = Field(
        description="The supplier (seller) who issued the document, not the customer"
    )
    supplier_tax_id: str = Field(
        description="The supplier's VAT, GST or other tax number. " + NOT_PRINTED
    )
    supplier_country: str = Field(
        description="The country in the supplier's printed address, as printed. Empty string if "
        "the address doesn't name a country; don't work it out from a city or postcode."
    )
    invoice_number: str = Field(
        description="The document's own number (for a packing slip, the slip number). "
        "Never use a PO, order or customer number instead. " + NOT_PRINTED
    )
    invoice_date: str = Field(
        description="The document's date exactly as printed (for a packing slip, its ship or "
        "slip date). " + NOT_PRINTED
    )
    due_date: str = Field(
        description="Exactly as printed. Empty string if no due date is printed, even when "
        "payment terms like 'Net 30' are shown."
    )
    currency: str = Field(
        description="The currency code if printed (e.g. 'GBP'), otherwise the symbol used on "
        "the amounts (e.g. '£'). Empty string if neither is printed."
    )
    po_number: str = Field(description="The buyer's purchase order number. " + NOT_PRINTED)
    subtotal: str = Field(description=PRINTED)
    tax: str = Field(description="Total tax amount. " + PRINTED)
    discount: str = Field(description=PRINTED)
    shipping: str = Field(description="Shipping, delivery or freight charge. " + PRINTED)
    total: str = Field(description="The amount due. " + PRINTED)
    line_items: list[RawLineItem] = Field(description="Every line item, on every page, in order")


class Reason(_Model):
    """Why a document needs a look: a stable code for tests and exports, and a plain message."""

    code: str
    message: str
    field: str | None = Field(default=None, description="The field it's about, if one")


def supplier_key(name: str | None) -> str | None:
    """Supplier names compared loosely: 'Larchmont Labels Inc.' == 'larchmont labels inc'."""
    if not name:
        return None
    return " ".join(re.sub(r"[^\w]+", " ", name.casefold()).split()) or None


class SourceFile(_Model):
    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Invoice(ExtractedInvoice):
    """An extracted invoice plus what the pipeline decided about it."""

    source_file: SourceFile
    status: Status = Status.REVIEW
    reasons: list[Reason] = Field(default_factory=list)
    model: str | None = Field(default=None, description="Model that produced the final result")

    @classmethod
    def from_extracted(
        cls, extracted: ExtractedInvoice, source_file: SourceFile, model: str | None = None
    ) -> Invoice:
        return cls(**extracted.model_dump(), source_file=source_file, model=model)
