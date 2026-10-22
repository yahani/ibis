"""Dates as printed on invoices.

Claude copies dates exactly as printed; this module turns them into `date` objects. Numeric dates
like 03/09/2026 are ambiguous (3 September in the UK, March 9 in the US), so the order is settled
per document (`resolve_order`), in this order:

1. A part over 12 on any of the document's dates (13/09/2026 can only be day first).
2. Ruling out impossible readings: an invoice date well in the future, or a due date before the
   invoice date.
3. A hint from outside the document: the supplier's setting in config, the order remembered from
   their earlier documents, or their printed country.

If none of these settles it, the order stays unknown and the document goes to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}

# A reading this far after the run date is ruled out. Generous, so an invoice dated a few days
# ahead is never "corrected" to a reading months earlier.
FUTURE_MARGIN = timedelta(days=31)


class Order(StrEnum):
    DAY_FIRST = "day_first"
    MONTH_FIRST = "month_first"


class Settled(StrEnum):
    PRINTED = "printed"  # a part over 12 on the document
    RULED_OUT = "ruled_out"  # the other order gives an impossible date
    HINT = "hint"  # supplier setting, remembered order or country
    NOT_NEEDED = "not_needed"  # no date on the document reads two ways
    AMBIGUOUS = "ambiguous"  # nothing settles it
    CONFLICT = "conflict"  # the document's dates contradict each other


@dataclass(frozen=True)
class DateOrder:
    order: Order | None  # None when ambiguous or conflicting
    settled_by: Settled


_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DOTTED = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})")
_NUMERIC = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})")
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-.]+([a-z]+)\.?,?[\s\-.]+(\d{4})")
_MONTH_DAY_YEAR = re.compile(r"([a-z]+)\.?[\s\-.]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-.]+(\d{4})")


def _year(text: str) -> int:
    year = int(text)
    return 2000 + year if year < 100 else year


def _numeric_parts(text: str) -> tuple[int, int] | None:
    """The first two numbers of a slash or dash date like 03/09/2026, else None."""
    match = _NUMERIC.fullmatch(text.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def resolve_order(
    invoice_date: str | None,
    due_date: str | None,
    *,
    today: date,
    hint: Order | None = None,
) -> DateOrder:
    """Settle day-first or month-first for a document's numeric dates (see the module docstring)."""
    numeric = [t for t in (invoice_date, due_date) if _numeric_parts(t or "")]
    if not numeric:
        return DateOrder(Order.DAY_FIRST, Settled.NOT_NEEDED)

    candidates = {Order.DAY_FIRST, Order.MONTH_FIRST}
    for text in numeric:
        first, second = _numeric_parts(text)
        if first > 12:
            candidates &= {Order.DAY_FIRST}
        if second > 12:
            candidates &= {Order.MONTH_FIRST}
    if not candidates:
        return DateOrder(None, Settled.CONFLICT)
    if len(candidates) == 1:
        return DateOrder(candidates.pop(), Settled.PRINTED)

    for order in list(candidates):
        issued, due = _quiet_parse(invoice_date, order), _quiet_parse(due_date, order)
        if (issued and issued > today + FUTURE_MARGIN) or (issued and due and due < issued):
            candidates.discard(order)
    if not candidates:
        return DateOrder(None, Settled.CONFLICT)
    if len(candidates) == 1:
        return DateOrder(candidates.pop(), Settled.RULED_OUT)

    if not any(is_ambiguous(t) for t in numeric):
        return DateOrder(Order.DAY_FIRST, Settled.NOT_NEEDED)  # e.g. 09/09/2026
    if hint:
        return DateOrder(hint, Settled.HINT)
    return DateOrder(None, Settled.AMBIGUOUS)


def _quiet_parse(text: str | None, order: Order) -> date | None:
    """A date if `text` reads as one; an unreadable date is reported elsewhere."""
    try:
        return parse_date(text, order)
    except ValueError:
        return None


def is_numeric(text: str | None) -> bool:
    """True for a slash or dash date like 03/09/2026, whose order has to be settled."""
    return _numeric_parts(text or "") is not None


def is_ambiguous(text: str | None) -> bool:
    """True for a numeric date whose day and month could swap (both 12 or under and different)."""
    parts = _numeric_parts(text or "")
    return parts is not None and parts[0] <= 12 and parts[1] <= 12 and parts[0] != parts[1]


def parse_date(text: str | None, order: Order = Order.DAY_FIRST) -> date | None:
    """Read a printed date: '2026-08-18', '18 August 2026', 'Aug 18, 2026', '26.08.2026',
    '13/09/2026'. `order` settles numeric dates with slashes or dashes.

    Returns None for empty input. Raises ValueError when the text isn't a date.
    """
    if text is None or not text.strip():
        return None
    original = text
    text = text.strip().lower()
    # Drop a leading weekday ("Tuesday, 18 August 2026").
    text = re.sub(r"^[a-z]+day,?\s+", "", text)

    try:
        if match := _ISO.fullmatch(text):
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if match := _DOTTED.fullmatch(text):
            return date(_year(match.group(3)), int(match.group(2)), int(match.group(1)))
        if match := _NUMERIC.fullmatch(text):
            a, b = int(match.group(1)), int(match.group(2))
            day, month = (a, b) if order == Order.DAY_FIRST else (b, a)
            return date(_year(match.group(3)), month, day)
        if (match := _DAY_MONTH_YEAR.fullmatch(text)) and match.group(2) in MONTHS:
            return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
        if (match := _MONTH_DAY_YEAR.fullmatch(text)) and match.group(1) in MONTHS:
            return date(int(match.group(3)), MONTHS[match.group(1)], int(match.group(2)))
    except ValueError as exc:  # e.g. 31 February
        raise ValueError(f"not a real date: {original!r}") from exc
    raise ValueError(f"not a date: {original!r}")
