"""Paging through long transaction lists in the CLI."""

from __future__ import annotations

from collections.abc import Sequence


def page_count(total: int, per_page: int) -> int:
    """How many pages ``total`` items fill."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return (total + per_page - 1) // per_page


def paginate[T](items: Sequence[T], page: int, per_page: int = 10) -> list[T]:
    """Items on ``page`` (1-based)."""
    if page < 1:
        raise ValueError("page is 1-based")
    start = page * per_page
    return list(items[start : start + per_page])
