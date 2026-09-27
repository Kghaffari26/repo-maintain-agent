"""The monthly summary printed by ``python -m ledgerlite``."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ledgerlite.dates import month_key
from ledgerlite.money import format_amount
from ledgerlite.stats import average, total


@dataclass
class Transaction:
    day: date
    category: str
    amount: Decimal


def monthly_summary(transactions: list[Transaction], month: str) -> dict[str, str]:
    """Total and average spend per category for one month ("2026-09")."""
    by_category: dict[str, list[Decimal]] = defaultdict(list)
    for t in transactions:
        if month_key(t.day) == month:
            by_category[t.category].append(t.amount)
    everything = [a for amounts in by_category.values() for a in amounts]
    summary = {
        category: f"{format_amount(total(amounts))} (avg {format_amount(average(amounts))})"
        for category, amounts in sorted(by_category.items())
    }
    overall = f"{format_amount(total(everything))} (avg {format_amount(average(everything))})"
    summary["ALL"] = overall
    return summary
