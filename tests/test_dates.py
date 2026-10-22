from datetime import date

import pytest

from ibis_docs.dates import DateOrder, Order, Settled, is_ambiguous, parse_date, resolve_order


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


TODAY = date(2026, 10, 22)
DF, MF = Order.DAY_FIRST, Order.MONTH_FIRST


def resolve(invoice, due=None, hint=None):
    return resolve_order(invoice, due, today=TODAY, hint=hint)


def test_a_part_over_12_settles_every_date_on_the_document():
    assert resolve("06/08/2026", "20/08/2026") == DateOrder(DF, Settled.PRINTED)
    assert resolve("07/01/2026", "09/25/2026") == DateOrder(MF, Settled.PRINTED)
    # Printed evidence beats a hint.
    assert resolve("13/09/2026", hint=MF) == DateOrder(DF, Settled.PRINTED)


def test_dates_that_contradict_each_other():
    assert resolve("13/09/2026", "09/25/2026") == DateOrder(None, Settled.CONFLICT)


def test_a_due_date_before_the_invoice_date_rules_an_order_out():
    # Day first: 5 Aug -> 4 Sep. Month first: May 8 -> Apr 9, due before issued.
    assert resolve("05/08/2026", "04/09/2026") == DateOrder(DF, Settled.RULED_OUT)
    assert resolve("08/05/2026", "09/04/2026") == DateOrder(MF, Settled.RULED_OUT)


def test_a_date_far_in_the_future_rules_an_order_out():
    # On 22 Oct 2026, 12/09/2026 month first is 9 Dec: more than a month ahead.
    assert resolve("12/09/2026") == DateOrder(DF, Settled.RULED_OUT)
    # 10/11/2026 day first is 10 Nov, only 19 days ahead: not ruled out.
    assert resolve("10/11/2026") == DateOrder(None, Settled.AMBIGUOUS)


def test_an_unreadable_date_rules_nothing_out():
    assert resolve("02/30/2026") == DateOrder(MF, Settled.PRINTED)  # 30 > 12
    # 31/02 is day first by its 31, and then isn't a real date: reported when it's parsed.
    assert resolve("31/02/2026") == DateOrder(DF, Settled.PRINTED)
    # An unreadable due date doesn't make the invoice date look contradictory.
    assert resolve("03/09/2026", "soon") == DateOrder(None, Settled.AMBIGUOUS)


def test_a_hint_settles_what_the_document_does_not():
    assert resolve("03/09/2026", hint=MF) == DateOrder(MF, Settled.HINT)
    assert resolve("03/09/2026") == DateOrder(None, Settled.AMBIGUOUS)


def test_no_order_needed():
    assert resolve("18 August 2026", "2026-09-17") == DateOrder(DF, Settled.NOT_NEEDED)
    assert resolve(None, None) == DateOrder(DF, Settled.NOT_NEEDED)
    assert resolve("09/09/2026") == DateOrder(DF, Settled.NOT_NEEDED)  # same either way


def test_is_ambiguous():
    assert is_ambiguous("03/09/2026")
    assert not is_ambiguous("13/09/2026")
    assert not is_ambiguous("09/09/2026")  # same either way
    assert not is_ambiguous("18 August 2026")
    assert not is_ambiguous(None)
