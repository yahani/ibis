"""Check the numbers (E5), find duplicates (E6), and decide each document's status (E7).

Every check is plain Python on the values Claude copied: the model never does the arithmetic.
Each problem becomes a `Reason` with a stable code and a message a bookkeeper can act on, with
the actual amounts in it. Only a document with no reasons is `ok`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

from ibis_docs.config import Config
from ibis_docs.models import DocType, ExtractedInvoice, Reason, Status
from ibis_docs.money import format_amount

# An invoice dated more than this after the run date is flagged (allows for time zones).
FUTURE_GRACE = timedelta(days=1)

# Problems a stronger model might read differently, so they're worth one retry (E4). The rest
# (a duplicate, a currency the client doesn't take, a date only the supplier can settle) would
# come out the same on any model.
RETRY_CODES = frozenset(
    {
        "line_math",
        "lines_vs_subtotal",
        "total_mismatch",
        "due_before_invoice",
        "future_date",
        "credit_sign",
        "missing_supplier",
        "missing_invoice_number",
        "missing_date",
        "missing_total",
        "missing_currency",
        "unreadable_amount",
        "unreadable_date",
        "unknown_currency",
        "ambiguous_amount",
        "conflicting_dates",
        "missing_description",
    }
)


def validate(
    invoice: ExtractedInvoice, config: Config, *, today: date, issues: Iterable[Reason] = ()
) -> list[Reason]:
    """All the reasons a document needs a look: the reading `issues` first, then the checks."""
    reasons = list(issues)
    unclear = {r.field for r in reasons if r.field}  # already explained; don't also say "missing"
    money = _Money(invoice.currency)

    reasons += _required(invoice, unclear)
    if invoice.doc_type != DocType.PACKING_SLIP:
        reasons += _line_maths(invoice, config.validation.total_tolerance, money)
        reasons += _totals(invoice, config.validation.total_tolerance, money)
        reasons += _currency(invoice, config)
        if invoice.doc_type == DocType.CREDIT_NOTE and invoice.total and invoice.total > 0:
            reasons.append(
                Reason(
                    code="credit_sign",
                    message=f"credit note with a positive total ({money(invoice.total)}); "
                    "check the sign",
                    field="total",
                )
            )
    reasons += _dates(invoice, today)
    return reasons


def status_for(reasons: list[Reason], *, failed: bool = False) -> Status:
    if failed:
        return Status.FAILED
    return Status.REVIEW if reasons else Status.OK


def worth_retrying(reasons: Iterable[Reason]) -> bool:
    return any(r.code in RETRY_CODES for r in reasons)


class _Money:
    def __init__(self, currency: str | None):
        self.currency = currency

    def __call__(self, amount: Decimal) -> str:
        return format_amount(amount, self.currency)


def _required(invoice: ExtractedInvoice, unclear: set[str]) -> list[Reason]:
    needed = [
        ("supplier_name", "missing_supplier", "no supplier name on the document"),
        ("invoice_number", "missing_invoice_number", "no invoice number on the document"),
        ("invoice_date", "missing_date", "no invoice date on the document"),
    ]
    if invoice.doc_type == DocType.PACKING_SLIP:
        needed[1] = ("invoice_number", "missing_invoice_number", "no slip number on the document")
        needed[2] = ("invoice_date", "missing_date", "no date on the document")
    else:
        needed += [
            ("total", "missing_total", "no total on the document"),
            ("currency", "missing_currency", "no currency shown on the document"),
        ]
    return [
        Reason(code=code, message=message, field=name)
        for name, code, message in needed
        if getattr(invoice, name) is None and name not in unclear
    ]


def _line_maths(invoice: ExtractedInvoice, tolerance: Decimal, money: _Money) -> list[Reason]:
    reasons = []
    for i, line in enumerate(invoice.line_items, start=1):
        if line.quantity is None or line.unit_price is None or line.line_total is None:
            continue
        expected = line.quantity * line.unit_price
        if abs(expected - line.line_total) > tolerance:
            reasons.append(
                Reason(
                    code="line_math",
                    message=f"line {i}: {_qty(line.quantity)} × {money(line.unit_price)} = "  # noqa: RUF001
                    f"{money(expected)}, but the line total is {money(line.line_total)}",
                    field=f"line_items[{i - 1}].line_total",
                )
            )
    return reasons


def _totals(invoice: ExtractedInvoice, tolerance: Decimal, money: _Money) -> list[Reason]:
    reasons = []
    lines = invoice.line_items
    line_sum = (
        sum((line.line_total for line in lines), Decimal(0))
        if lines and all(line.line_total is not None for line in lines)
        else None
    )

    if invoice.subtotal is not None and line_sum is not None:
        diff = invoice.subtotal - line_sum
        if abs(diff) > tolerance:
            reasons.append(
                Reason(
                    code="lines_vs_subtotal",
                    message=f"the lines add up to {money(line_sum)}, but the subtotal is "
                    f"{money(invoice.subtotal)} ({_off_by(diff, money)})",
                    field="subtotal",
                )
            )

    base, base_label = (
        (invoice.subtotal, "subtotal") if invoice.subtotal is not None else (line_sum, "lines")
    )
    if invoice.total is None or base is None:
        return reasons
    expected = base + (invoice.tax or 0) - (invoice.discount or 0) + (invoice.shipping or 0)
    diff = invoice.total - expected
    if abs(diff) > tolerance:
        parts = [f"{base_label} {money(base)}"]
        if invoice.tax is not None:
            parts.append(f"+ tax {money(invoice.tax)}")
        if invoice.discount is not None:
            parts.append(f"- discount {money(invoice.discount)}")
        if invoice.shipping is not None:
            parts.append(f"+ shipping {money(invoice.shipping)}")
        reasons.append(
            Reason(
                code="total_mismatch",
                message=f"the total doesn't add up: {' '.join(parts)} = {money(expected)}, but "
                f"the printed total is {money(invoice.total)} ({_off_by(diff, money)})",
                field="total",
            )
        )
    return reasons


def _currency(invoice: ExtractedInvoice, config: Config) -> list[Reason]:
    allowed = config.validation.allowed_currencies
    if invoice.currency is None or invoice.currency in allowed:
        return []
    return [
        Reason(
            code="currency_not_allowed",
            message=f"{invoice.currency} isn't one of your allowed currencies "
            f"({', '.join(allowed)})",
            field="currency",
        )
    ]


def _dates(invoice: ExtractedInvoice, today: date) -> list[Reason]:
    reasons = []
    issued, due = invoice.invoice_date, invoice.due_date
    if issued and due and due < issued:
        reasons.append(
            Reason(
                code="due_before_invoice",
                message=f"the due date {_day(due)} is before the invoice date {_day(issued)}",
                field="due_date",
            )
        )
    if issued and issued > today + FUTURE_GRACE:
        reasons.append(
            Reason(
                code="future_date",
                message=f"the invoice date {_day(issued)} is in the future",
                field="invoice_date",
            )
        )
    return reasons


def _qty(quantity: Decimal) -> str:
    return f"{quantity.normalize():,f}"


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def _off_by(diff: Decimal, money: _Money) -> str:
    return f"{money(abs(diff))} {'more' if diff > 0 else 'less'}"
