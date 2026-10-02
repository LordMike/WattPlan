"""Datetime parsing helpers shared across WattPlan."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise
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


def typical_step(timestamps: list[datetime]) -> timedelta | None:
    """Return the most common positive spacing between sorted timestamps.

    Ties go to the shorter step. Unlike the last or the mean step, this is
    not skewed by a hole or by one short trailing interval.
    """
    steps = Counter(right - left for left, right in pairwise(timestamps) if right > left)
    if not steps:
        return None
    return min(steps, key=lambda step: (-steps[step], step))
