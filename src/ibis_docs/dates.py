"""Dates as printed on invoices.

Claude copies dates exactly as printed; this module turns them into `date` objects. Numeric dates
like 03/09/2026 are ambiguous (3 September in the UK, March 9 in the US), so the order is decided
per document: a part over 12 settles it, otherwise the currency suggests it and the caller is told
the order was assumed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
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

# Currencies whose invoices usually print month first. Everything else defaults to day first.
MONTH_FIRST_CURRENCIES = frozenset({"USD"})


class Order(StrEnum):
    DAY_FIRST = "day_first"
    MONTH_FIRST = "month_first"


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


def infer_order(texts: Iterable[str | None], currency: str | None = None) -> tuple[Order, bool]:
    """Pick day-first or month-first for a document's numeric dates.

    Returns the order and whether it was assumed (True when no date settles it on its own).
    """
    for text in texts:
        parts = _numeric_parts(text or "")
        if parts is None:
            continue
        first, second = parts
        if first > 12 >= second:
            return Order.DAY_FIRST, False
        if second > 12 >= first:
            return Order.MONTH_FIRST, False
    if currency in MONTH_FIRST_CURRENCIES:
        return Order.MONTH_FIRST, True
    return Order.DAY_FIRST, True


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
