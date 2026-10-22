from datetime import date

import pytest

from ibis_docs.dates import Order, infer_order, is_ambiguous, parse_date


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-08-18", date(2026, 8, 18)),
        ("2026/08/18", date(2026, 8, 18)),
        ("18 August 2026", date(2026, 8, 18)),
        ("18th Aug 2026", date(2026, 8, 18)),
        ("18-Aug-2026", date(2026, 8, 18)),
        ("August 18, 2026", date(2026, 8, 18)),
        ("Aug. 18 2026", date(2026, 8, 18)),
        ("Sept 3, 2026", date(2026, 9, 3)),
        ("Tuesday, 18 August 2026", date(2026, 8, 18)),
        ("26.08.2026", date(2026, 8, 26)),
        ("26.08.26", date(2026, 8, 26)),
        ("13/09/2026", date(2026, 9, 13)),
        ("  13-09-2026 ", date(2026, 9, 13)),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_order_settles_numeric_dates():
    assert parse_date("03/09/2026", Order.DAY_FIRST) == date(2026, 9, 3)
    assert parse_date("03/09/2026", Order.MONTH_FIRST) == date(2026, 3, 9)
    assert parse_date("09/20/2026", Order.MONTH_FIRST) == date(2026, 9, 20)
    # Unambiguous formats ignore the order.
    assert parse_date("26.08.2026", Order.MONTH_FIRST) == date(2026, 8, 26)
    assert parse_date("2026-03-09", Order.MONTH_FIRST) == date(2026, 3, 9)


@pytest.mark.parametrize("text", [None, "", "   "])
def test_empty_is_none(text):
    assert parse_date(text) is None


@pytest.mark.parametrize("text", ["soon", "Net 30", "18 Smarch 2026", "2026-13-45", "31/02/2026"])
def test_not_a_date(text):
    with pytest.raises(ValueError, match="date"):
        parse_date(text)


def test_infer_order_from_a_part_over_12():
    assert infer_order(["13/09/2026", "03/10/2026"]) == (Order.DAY_FIRST, False)
    assert infer_order(["09/20/2026", None]) == (Order.MONTH_FIRST, False)
    # Even when the currency suggests the other way.
    assert infer_order(["13/09/2026"], "USD") == (Order.DAY_FIRST, False)


def test_infer_order_falls_back_to_currency():
    assert infer_order(["03/09/2026"], "USD") == (Order.MONTH_FIRST, True)
    assert infer_order(["03/09/2026"], "GBP") == (Order.DAY_FIRST, True)
    assert infer_order([None, "18 August 2026"], None) == (Order.DAY_FIRST, True)


def test_is_ambiguous():
    assert is_ambiguous("03/09/2026")
    assert not is_ambiguous("13/09/2026")
    assert not is_ambiguous("09/09/2026")  # same either way
    assert not is_ambiguous("18 August 2026")
    assert not is_ambiguous(None)
