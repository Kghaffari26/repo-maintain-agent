from datetime import date
from decimal import Decimal

from ledgerlite.dates import month_bounds, month_key
from ledgerlite.money import format_amount, parse_amount
from ledgerlite.pagination import page_count
from ledgerlite.report import Transaction, monthly_summary
from ledgerlite.stats import average, total
from ledgerlite.text import slugify


def test_parse_simple_amounts():
    assert parse_amount("12.50") == Decimal("12.50")
    assert parse_amount("$4.99") == Decimal("4.99")
    assert parse_amount("(7.25)") == Decimal("-7.25")


def test_format_amount():
    assert format_amount(Decimal("1234.5")) == "1,234.50"


def test_month_key_and_bounds():
    assert month_key(date(2026, 9, 27)) == "2026-09"
    assert month_bounds(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))


def test_page_count():
    assert page_count(25, 10) == 3


def test_stats():
    values = [Decimal("1.00"), Decimal("2.00")]
    assert total(values) == Decimal("3.00")
    assert average(values) == Decimal("1.50")


def test_slugify_simple():
    assert slugify("Eating Out") == "eating-out"


def test_monthly_summary():
    txns = [
        Transaction(date(2026, 9, 1), "Groceries", Decimal("10.00")),
        Transaction(date(2026, 9, 2), "Groceries", Decimal("20.00")),
        Transaction(date(2026, 8, 30), "Rent", Decimal("900.00")),
    ]
    assert monthly_summary(txns, "2026-09") == {
        "Groceries": "30.00 (avg 15.00)",
        "ALL": "30.00 (avg 15.00)",
    }
