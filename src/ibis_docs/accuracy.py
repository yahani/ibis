"""Score extracted documents against the demo answer key (demo/answers.json)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from ibis_docs.models import ExtractedInvoice

HEADER_FIELDS = [
    "doc_type",
    "supplier_name",
    "supplier_tax_id",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "po_number",
    "subtotal",
    "tax",
    "discount",
    "shipping",
    "total",
]
LINE_FIELDS = ["description", "sku", "quantity", "unit_price", "line_total", "tax_rate"]


@dataclass
class Score:
    header_right: int = 0
    header_total: int = len(HEADER_FIELDS)
    differences: list[str] = field(default_factory=list)


def same(expected: object, got: object) -> bool:
    """Compare an answer-key value (JSON: ISO dates, amounts as strings) with an extracted one.
    Text compares ignoring spacing and letter case."""
    if expected is None or got is None:
        return expected is None and got is None
    if isinstance(got, Decimal):
        return Decimal(str(expected)) == got
    if isinstance(got, date):
        return date.fromisoformat(str(expected)) == got
    if isinstance(got, Enum):
        got = got.value
    # Spacing and letter case aren't data: "KITTIWAKE COFFEE ROASTERS" as printed is right.
    return " ".join(str(expected).split()).casefold() == " ".join(str(got).split()).casefold()


def score(invoice: ExtractedInvoice, answer: dict) -> Score:
    """Compare one document's fields with its answer-key entry (`fields` in answers.json)."""
    result = Score()
    for name in HEADER_FIELDS:
        expected, got = answer[name], getattr(invoice, name)
        if same(expected, got):
            result.header_right += 1
        else:
            result.differences.append(f"{name}: expected {expected!r}, got {got!r}")

    expected_lines = answer["line_items"]
    if len(expected_lines) != len(invoice.line_items):
        result.differences.append(
            f"line items: expected {len(expected_lines)}, got {len(invoice.line_items)}"
        )
    for i, (exp, got) in enumerate(zip(expected_lines, invoice.line_items, strict=False), 1):
        for name in LINE_FIELDS:
            if not same(exp[name], getattr(got, name)):
                result.differences.append(
                    f"line {i} {name}: expected {exp[name]!r}, got {getattr(got, name)!r}"
                )
    return result
