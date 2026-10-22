from decimal import Decimal

import pytest

from ibis_docs.money import (
    DEFAULT_SYMBOLS,
    is_iso_currency,
    normalise_currency,
    parse_amount,
    symbol_table,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,299.00", "1299.00"),
        ("1.299,00", "1299.00"),
        ("£51.77", "51.77"),
        ("$0.50", "0.50"),
        ("12.00 €", "12.00"),
        ("€ 1.234.567,89", "1234567.89"),
        ("1 299,00 kr", "1299.00"),
        ("1\u00a0299,00", "1299.00"),
        ("CHF 1'299.00", "1299.00"),
        ("Fr. 12.50", "12.50"),
        ("Rs.500", "500"),
        ("1,29,999.00", "129999.00"),
        ("12,5", "12.5"),
        ("12,50", "12.50"),
        ("1,299", "1299"),
        ("1.299", "1299"),
        ("1.299.000", "1299000"),
        ("42", "42"),
        ("0.075", "0.075"),
        (".5", "0.5"),
        ("+3.00", "3.00"),
        ("USD 1,000.00", "1000.00"),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text", ["-12.00", "\u221212.00", "(12.00)", "12.00-", "-£12.00", "£-12.00", "(1.299,00 €)"]
)
def test_negative_credit_note_amounts(text):
    assert parse_amount(text) < 0


def test_keeps_exact_decimal_places():
    assert str(parse_amount("51.70")) == "51.70"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("   ", None), (12, Decimal(12)), (0.1, Decimal("0.1"))],
)
def test_non_text_inputs(value, expected):
    assert parse_amount(value) == expected


def test_decimal_passes_through():
    value = Decimal("3.14")
    assert parse_amount(value) is value


@pytest.mark.parametrize(
    "text", ["abc", "1,2,3", "12.34.56,7,8", "1,2345.00", "--", "£", "12,345,67.00", "1.2.3,45"]
)
def test_rejects_non_amounts(text):
    with pytest.raises(ValueError, match="not an amount"):
        parse_amount(text)


def test_rejects_bool():
    with pytest.raises(ValueError):
        parse_amount(True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GBP", "GBP"),
        ("gbp", "GBP"),
        (" eur ", "EUR"),
        ("£51.77", "GBP"),
        ("12,00 €", "EUR"),
        ("$12", "USD"),
        ("A$12", "AUD"),
        ("NZ$ 5.00", "NZD"),
        ("CAD $12.00", "CAD"),
        ("Total in JPY", "JPY"),
        ("", None),
        (None, None),
        ("12.00", None),
        ("Top of the page", None),
        ("XYZ", None),
    ],
)
def test_normalise_currency(text, expected):
    assert normalise_currency(text) == expected


def test_client_can_remap_dollar_and_add_symbols():
    table = symbol_table({"$": "aud", "Rp": "IDR"})
    assert normalise_currency("$12.00", table) == "AUD"
    assert normalise_currency("Rp 15.000", table) == "IDR"
    # Defaults are untouched for other clients.
    assert DEFAULT_SYMBOLS["$"] == "USD"


def test_iso_codes():
    for code in ("USD", "gbp", "EUR", "AUD", "JPY", "NZD", "CHF", "INR"):
        assert is_iso_currency(code)
    for code in ("XXX", "ABC", "US", "EURO"):
        assert not is_iso_currency(code)


# --- the document's decimal mark (session 3) --------------------------------------------------


@pytest.mark.parametrize(
    ("texts", "mark"),
    [
        (["1.299,00 €"], ","),
        (["12,50"], ","),
        (["1.234.567"], ","),
        (["1,812.86"], "."),
        (["£51.77", "0.75"], "."),
        (["1,234,567"], "."),
        (["1,299", "12.125", None, ""], None),  # nothing settles it
        (["1,812.86", "1.299,00"], None),  # amounts disagree
    ],
)
def test_infer_decimal_mark(texts, mark):
    from ibis_docs.money import infer_decimal_mark

    assert infer_decimal_mark(texts) == mark


def test_a_known_decimal_mark_settles_three_digit_amounts():
    assert parse_amount("12,125", ",") == Decimal("12.125")
    assert parse_amount("12,125", ".") == Decimal("12125")
    assert parse_amount("1.299", ",") == Decimal("1299")
    assert parse_amount("1.234.567", ",") == Decimal("1234567")


def test_is_ambiguous_amount():
    from ibis_docs.money import is_ambiguous_amount

    assert is_ambiguous_amount("1,299") and is_ambiguous_amount("£12.125")
    assert not is_ambiguous_amount("0.125")  # a leading 0 is never followed by thousands
    assert not is_ambiguous_amount("1,299.00") and not is_ambiguous_amount("12.50")
    assert not is_ambiguous_amount(None) and not is_ambiguous_amount("1.234.567")


@pytest.mark.parametrize(
    ("amount", "currency", "text"),
    [
        ("1812.86", "GBP", "£1,812.86"),
        ("-12.4", "EUR", "-€12.40"),
        ("431.28", "CHF", "CHF 431.28"),
        ("1.250", "KWD", "KWD 1.250"),
        ("5", None, "5.00"),
        ("2504.51", "USD", "$2,504.51"),
    ],
)
def test_format_amount(amount, currency, text):
    from ibis_docs.money import format_amount

    assert format_amount(Decimal(amount), currency) == text
