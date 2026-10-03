"""Datetime parsing helpers shared across WattPlan."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from homeassistant.util import dt as dt_util


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


def as_utc_assuming_local(value: datetime) -> datetime:
    """Return value in UTC; naive values are read in Home Assistant's time zone.

    Use this for external source data. Internally produced timestamps are
    already aware and unaffected.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return value.astimezone(UTC)


def typical_step(timestamps: list[datetime]) -> timedelta | None:
    """Return the most common positive spacing between sorted timestamps.

    Ties go to the shorter step. Unlike the last or the mean step, this is
    not skewed by a hole or by one short trailing interval.
    """
    steps = Counter(right - left for left, right in pairwise(timestamps) if right > left)
    if not steps:
        return None
    return min(steps, key=lambda step: (-steps[step], step))
