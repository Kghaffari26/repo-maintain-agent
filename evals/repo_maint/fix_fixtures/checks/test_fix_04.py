from decimal import Decimal

from ledgerlite.report import monthly_summary
from ledgerlite.stats import average


def test_empty_month():
    assert average([]) == Decimal("0.00")
    assert monthly_summary([], "2026-01") == {"ALL": "0.00 (avg 0.00)"}
    assert average([Decimal("1"), Decimal("2")]) == Decimal("1.50")
