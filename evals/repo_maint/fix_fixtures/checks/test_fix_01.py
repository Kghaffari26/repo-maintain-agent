from decimal import Decimal

from ledgerlite.money import parse_amount


def test_thousands_separator():
    assert parse_amount("1,234.50") == Decimal("1234.50")
    assert parse_amount("$12,000") == Decimal("12000")
    assert parse_amount("(1,000.01)") == Decimal("-1000.01")
