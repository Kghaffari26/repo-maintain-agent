"""Month arithmetic for bucketing transactions."""

from __future__ import annotations

from datetime import date, timedelta


def month_key(day: date) -> str:
    """"2026-09" for any day in September 2026."""
    return f"{day.year:04d}-{day.month:02d}"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """The first and last day of a month."""
    first = date(year, month, 1)
    last = date(year, month + 1, 1) - timedelta(days=1)
    return first, last
