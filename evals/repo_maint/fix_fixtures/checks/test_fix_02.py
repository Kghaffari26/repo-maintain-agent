from datetime import date

from ledgerlite.dates import month_bounds


def test_december():
    assert month_bounds(2025, 12) == (date(2025, 12, 1), date(2025, 12, 31))
    assert month_bounds(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))
