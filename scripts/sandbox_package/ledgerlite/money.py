"""Amounts as they appear in bank CSV exports."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_amount(text: str) -> Decimal:
    """Parse an amount like "12.50", "-3", "$4.99" or "(7.25)" (a debit)."""
    cleaned = text.strip().replace("$", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    try:
        value = Decimal(cleaned)
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {text!r}") from e
    return -value if negative else value


def format_amount(value: Decimal) -> str:
    """Two decimals with a thousands separator, e.g. "1,234.50"."""
    return f"{value:,.2f}"
