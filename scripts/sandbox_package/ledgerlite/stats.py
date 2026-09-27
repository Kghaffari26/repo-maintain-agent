"""Summary statistics over transaction amounts."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal


def total(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0"))


def average(values: Sequence[Decimal]) -> Decimal:
    """Mean amount, rounded to cents."""
    return (total(values) / len(values)).quantize(Decimal("0.01"))
