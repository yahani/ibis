"""Amounts and currencies as printed on invoices.

Money is always Decimal, never float. Currencies are ISO 4217 codes; any active code can be
allowed in config, and the symbol table can be extended or overridden per client (a store in
Australia maps "$" to AUD).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

# Active ISO 4217 currency codes (excluding funds, metals and testing codes).
ISO_4217 = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN
    BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD
    FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP
    MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR
    RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT
    TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR
    ZMW ZWG
    """.split()  # noqa: SIM905 - one code per word reads better than a list
)

# Default symbol → code. Longer symbols are matched first, so "A$" wins over "$".
# A bare "$" is ambiguous; it defaults to USD and a client config can remap it.
DEFAULT_SYMBOLS: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "A$": "AUD",
    "AU$": "AUD",
    "C$": "CAD",
    "CA$": "CAD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "S$": "SGD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "R$": "BRL",
    "₩": "KRW",
    "₪": "ILS",
    "₺": "TRY",
    "zł": "PLN",
    "kr": "SEK",
    "Fr.": "CHF",
}

# Symbol and code characters stripped from an amount before reading the number.
_NOT_NUMBER = re.compile(r"[^\d.,'\s\u00a0\u202f()\-\u2212+]")
_SPACES = re.compile(r"[\s\u00a0\u202f']")


def is_iso_currency(code: str) -> bool:
    return code.upper() in ISO_4217


def symbol_table(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """The default symbol table with per-client overrides applied."""
    table = dict(DEFAULT_SYMBOLS)
    if overrides:
        table.update({sym: code.upper() for sym, code in overrides.items()})
    return table


def normalise_currency(text: str | None, symbols: Mapping[str, str] | None = None) -> str | None:
    """Turn '£', 'gbp', 'GBP' or '£51.77' into 'GBP'. None when it can't be told.

    An ISO code anywhere in the text wins over a symbol, so 'CAD $12.00' is CAD, not USD.
    """
    if not text:
        return None
    if text.strip().upper() in ISO_4217:
        return text.strip().upper()
    # Inside longer text only an upper-case code counts, so "Top" or "all" aren't TOP or ALL.
    for word in re.findall(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])", text):
        if word in ISO_4217:
            return word
    table = symbols if symbols is not None else DEFAULT_SYMBOLS
    for sym in sorted(table, key=len, reverse=True):
        if sym in text:
            return table[sym].upper()
    return None


def parse_amount(value: str | int | float | Decimal | None) -> Decimal | None:
    """Read an amount as printed: '1,299.00', '1.299,00', '£51.77', '-12.00', '(12.00)'.

    Returns None for empty input. Raises ValueError when the text isn't an amount.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"not an amount: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Through str, so 0.1 stays 0.1 rather than 0.1000000000000000055…
        return Decimal(str(value))

    text = value.strip()
    if not text:
        return None
    original = text

    # A dot that ends an abbreviation ("Fr. 12.50", "Rs.500") belongs to the symbol.
    text = re.sub(r"(?<=[^\W\d_])\.", "", text)
    text = _NOT_NUMBER.sub("", text)
    text = _SPACES.sub("", text).rstrip(".,")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.startswith(("-", "\u2212")):
        negative, text = True, text[1:]
    elif text.endswith(("-", "\u2212")):  # "12.00-" as some accounting systems print credits
        negative, text = True, text[:-1]
    text = text.removeprefix("+")

    if not re.fullmatch(r"[\d.,]*\d[\d.,]*", text):
        raise ValueError(f"not an amount: {original!r}")

    text = _to_plain_decimal(text, original)
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex above
        raise ValueError(f"not an amount: {original!r}") from exc
    return -amount if negative else amount


def _to_plain_decimal(text: str, original: str) -> str:
    """Decide which of '.' and ',' is the decimal mark and return '1299.00' style text."""
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        decimal_mark = "." if text.rfind(".") > text.rfind(",") else ","
    elif has_dot or has_comma:
        mark = "." if has_dot else ","
        parts = text.split(mark)
        # One mark followed by exactly three digits is a thousands separator ("1,299", "1.299");
        # anything else ("12.5", "12,50") is a decimal mark. Repeated marks are thousands.
        # A leading "0" ("0.075") is never followed by a thousands separator.
        thousands_only = len(parts) > 2 or (len(parts[-1]) == 3 and parts[0] not in ("", "0"))
        decimal_mark = "" if thousands_only else mark
    else:
        return text

    thousands = {".", ","} - {decimal_mark}
    integer, _, fraction = text.rpartition(decimal_mark) if decimal_mark else (text, "", "")
    for sep in thousands:
        if sep in fraction:
            raise ValueError(f"not an amount: {original!r}")
        integer = integer.replace(sep, "")
    if decimal_mark and not fraction:
        raise ValueError(f"not an amount: {original!r}")
    if decimal_mark and decimal_mark in integer:
        raise ValueError(f"not an amount: {original!r}")
    if not _grouped_ok(text.rpartition(decimal_mark)[0] if decimal_mark else text):
        raise ValueError(f"not an amount: {original!r}")
    return f"{integer or '0'}.{fraction}" if decimal_mark else integer


def _grouped_ok(integer: str) -> bool:
    """Thousands groups after the first are 3 digits (or 2, for Indian '1,29,999')."""
    groups = re.split(r"[.,]", integer)
    if len(groups) == 1:
        return True
    return (
        1 <= len(groups[0]) <= 3
        and len(groups[-1]) == 3
        and all(len(g) in (2, 3) for g in groups[1:-1])
    )
