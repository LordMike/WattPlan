"""Datetime parsing helpers shared across WattPlan."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_datetime_like(value: Any) -> datetime | None:
    """Return a datetime for native datetimes or ISO-8601 strings."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
