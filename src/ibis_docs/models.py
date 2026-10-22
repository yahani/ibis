"""Invoice and line-item schemas.

One model drives the API schema (session 2), validation (session 3) and export (session 4).
`ExtractedInvoice` holds only what is printed on the document, so it is what Claude is asked to
fill. `Invoice` adds what the pipeline decides: source file, status, reasons, model.

Money is Decimal, never float. Anything the document doesn't show stays None, never zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ibis_docs.money import normalise_currency, parse_amount


def _amount(value: object) -> Decimal | None:
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

PRINTED = "Exactly as printed, including any sign, brackets or symbol. null if not printed."


class RawLineItem(_Model):
    description: str = Field(description="The line's description as printed")
    sku: str | None = Field(default=None, description="Item code or SKU. null if not printed.")
    quantity: str | None = Field(default=None, description=PRINTED)
    unit_price: str | None = Field(default=None, description=PRINTED)
    line_total: str | None = Field(default=None, description=PRINTED)
    tax_rate: str | None = Field(
        default=None,
        description="This line's tax rate as printed, e.g. '20%'. null if not printed.",
    )


class RawInvoice(_Model):
    doc_type: DocType = Field(
        description="invoice, credit_note (credits money back), packing_slip (no prices), or other"
    )
    supplier_name: str | None = Field(
        default=None, description="The supplier (seller) who issued the document, not the customer"
    )
    supplier_tax_id: str | None = Field(
        default=None, description="The supplier's VAT, GST or tax number. null if not printed."
    )
    invoice_number: str | None = Field(
        default=None,
        description="The document's own number (for a packing slip, the slip number). "
        "null if not printed; never use a PO, order or customer number instead.",
    )
    invoice_date: str | None = Field(default=None, description="Exactly as printed")
    due_date: str | None = Field(
        default=None,
        description="Exactly as printed. null if no date is printed, even when payment terms "
        "like 'Net 30' are shown.",
    )
    currency: str | None = Field(
        default=None,
        description="The currency code if printed (e.g. 'GBP'), otherwise the symbol used on "
        "the amounts (e.g. '£'). null if neither is printed.",
    )
    po_number: str | None = Field(
        default=None, description="The buyer's purchase order number. null if not printed."
    )
    subtotal: str | None = Field(default=None, description=PRINTED)
    tax: str | None = Field(default=None, description="Total tax amount. " + PRINTED)
    discount: str | None = Field(default=None, description=PRINTED)
    shipping: str | None = Field(
        default=None, description="Shipping, delivery or freight charge. " + PRINTED
    )
    total: str | None = Field(default=None, description="The amount due. " + PRINTED)
    line_items: list[RawLineItem] = Field(
        default_factory=list, description="Every line item, on every page, in order"
    )


class SourceFile(_Model):
    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Invoice(ExtractedInvoice):
    """An extracted invoice plus what the pipeline decided about it."""

    source_file: SourceFile
    status: Status = Status.REVIEW
    reasons: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, description="Model that produced the final result")

    @classmethod
    def from_extracted(
        cls, extracted: ExtractedInvoice, source_file: SourceFile, model: str | None = None
    ) -> Invoice:
        return cls(**extracted.model_dump(), source_file=source_file, model=model)
