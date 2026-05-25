"""Deterministic generators shared by live entities and recorder backfill."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
from typing import Any

from .const import (
    CONF_BASE_OFFSET,
    CONF_CLOUD_FACTOR,
    CONF_FACTOR,
    CONF_INITIAL_TOTAL_KWH,
    CONF_NOISE,
    CONF_PEAK_KWH,
    CONF_PHASE_HOURS,
    CONF_PROFILE,
    CONF_SEED,
    CONF_SLOT_MINUTES,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    FUTURE_WINDOW_HOURS,
)


@dataclass(frozen=True, slots=True)
class GenerationContext:
    """Shared generation context for one live or historical data window."""

    start_at: datetime
    slot_minutes: int
    slots: int
    seed: int = 1


def as_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def floor_to_slot(value: datetime, slot_minutes: int) -> datetime:
    """Floor a datetime to the nearest slot boundary."""
    value = as_utc(value)
    slot_seconds = slot_minutes * 60
    floored = (int(value.timestamp()) // slot_seconds) * slot_seconds
    return datetime.fromtimestamp(floored, tz=UTC)


def context_from_config(config: dict[str, Any], *, now: datetime | None = None) -> GenerationContext:
    """Build a fixed 24-hour future generation context from config."""
    slot_minutes = int(
        config.get(
            CONF_UPDATE_INTERVAL_MINUTES,
            config.get(CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES),
        )
    )
    slots = max(1, int(round((FUTURE_WINDOW_HOURS * 60) / slot_minutes)))
    return GenerationContext(
        start_at=floor_to_slot(now or datetime.now(tz=UTC), slot_minutes),
        slot_minutes=slot_minutes,
        slots=slots,
        seed=int(config.get(CONF_SEED, 1)),
    )


def _noise(index: int, seed: int) -> float:
    """Return stable pseudo-noise in the range [-1, 1]."""
    # A small sine hash is enough here and avoids a runtime dependency.
    value = math.sin((index + 1) * 12.9898 + seed * 78.233) * 43758.5453
    return (value - math.floor(value)) * 2.0 - 1.0


def _point(start: datetime, value: float) -> dict[str, Any]:
    """Return one WattPlan-compatible forecast point."""
    return {"start": start.isoformat(), "value": round(float(value), 6)}


def price_points(config: dict[str, Any], ctx: GenerationContext) -> list[dict[str, Any]]:
    """Return price future values."""
    base = float(config.get(CONF_BASE_OFFSET, 0.3))
    factor = float(config.get(CONF_FACTOR, 1.0))
    noise = float(config.get(CONF_NOISE, 0.03))
    phase_hours = float(config.get(CONF_PHASE_HOURS, 0.0))
    points: list[dict[str, Any]] = []
    for index in range(ctx.slots):
        at = ctx.start_at + timedelta(minutes=index * ctx.slot_minutes)
        hour = (at.hour + (at.minute / 60.0) + phase_hours) % 24
        daily = math.sin(((hour - 7.0) / 24.0) * math.tau) * 0.10
        evening = max(0.0, math.sin(((hour - 15.0) / 8.0) * math.pi)) * 0.16
        value = base + (daily + evening) * factor + (_noise(index, ctx.seed) * noise)
        points.append(_point(at, max(-5.0, value)))
    return points


def _pv_kw(
    config: dict[str, Any], ctx: GenerationContext, index: int, at: datetime
) -> float:
    """Return generated PV power for one point in kW."""
    peak_kw = float(config.get(CONF_PEAK_KWH, 1.2))
    cloud_factor = float(config.get(CONF_CLOUD_FACTOR, 0.15))
    factor = float(config.get(CONF_FACTOR, 1.0))
    phase_hours = float(config.get(CONF_PHASE_HOURS, 0.0))
    hour = (at.hour + (at.minute / 60.0) + phase_hours) % 24
    sun = max(0.0, math.sin(((hour - 6.0) / 12.0) * math.pi))
    cloud = 1.0 - max(0.0, cloud_factor) * (
        (1.0 + _noise(index, ctx.seed + 11)) / 2.0
    )
    return peak_kw * sun * max(0.0, factor) * max(0.0, cloud)


def pv_power_points(
    config: dict[str, Any], ctx: GenerationContext
) -> list[dict[str, Any]]:
    """Return solar production power points in watts."""
    points: list[dict[str, Any]] = []
    for index in range(ctx.slots):
        at = ctx.start_at + timedelta(minutes=index * ctx.slot_minutes)
        points.append(_point(at, _pv_kw(config, ctx, index, at) * 1000.0))
    return points


def pv_energy_points(config: dict[str, Any], ctx: GenerationContext) -> list[dict[str, Any]]:
    """Return solar production energy future values in kWh per slot."""
    slot_hours = ctx.slot_minutes / 60.0
    points: list[dict[str, Any]] = []
    for index in range(ctx.slots):
        at = ctx.start_at + timedelta(minutes=index * ctx.slot_minutes)
        value = _pv_kw(config, ctx, index, at) * slot_hours
        points.append(_point(at, value))
    return points


def load_power_points(config: dict[str, Any], ctx: GenerationContext) -> list[dict[str, Any]]:
    """Return load future values in watts."""
    profile = str(config.get(CONF_PROFILE, "medium"))
    base_kw = {"light": 0.45, "medium": 0.9, "heavy": 2.0}.get(profile, 0.9)
    factor = float(config.get(CONF_FACTOR, 1.0))
    noise = float(config.get(CONF_NOISE, 0.04))
    phase_hours = float(config.get(CONF_PHASE_HOURS, 0.0))
    points: list[dict[str, Any]] = []
    for index in range(ctx.slots):
        at = ctx.start_at + timedelta(minutes=index * ctx.slot_minutes)
        hour = (at.hour + (at.minute / 60.0) + phase_hours) % 24
        morning = max(0.0, math.sin(((hour - 5.0) / 5.0) * math.pi)) * 0.35
        evening = max(0.0, math.sin(((hour - 16.0) / 7.0) * math.pi)) * 0.65
        kw = base_kw * max(0.0, factor) * (1.0 + morning + evening)
        kw += _noise(index, ctx.seed + 23) * noise
        points.append(_point(at, max(0.0, kw * 1000.0)))
    return points


def load_energy_points(config: dict[str, Any], ctx: GenerationContext) -> list[dict[str, Any]]:
    """Return load energy future values in kWh per slot."""
    slot_hours = ctx.slot_minutes / 60.0
    return [
        _point(datetime.fromisoformat(str(point["start"])), float(point["value"]) / 1000.0 * slot_hours)
        for point in load_power_points(config, ctx)
    ]


def load_points(config: dict[str, Any], ctx: GenerationContext) -> list[dict[str, Any]]:
    """Compatibility alias for load energy future values in kWh per slot."""
    return load_energy_points(config, ctx)


def pv_points(config: dict[str, Any], ctx: GenerationContext) -> list[dict[str, Any]]:
    """Compatibility alias for PV energy future values in kWh per slot."""
    return pv_energy_points(config, ctx)


def cumulative_load_states(
    config: dict[str, Any],
    *,
    start_at: datetime,
    end_at: datetime,
    slot_minutes: int,
    seed: int,
) -> list[tuple[datetime, float]]:
    """Return cumulative load states for a historical window."""
    start_at = floor_to_slot(start_at, slot_minutes)
    end_at = floor_to_slot(end_at, slot_minutes)
    slots = max(1, int((end_at - start_at).total_seconds() // (slot_minutes * 60)))
    ctx = GenerationContext(start_at=start_at, slot_minutes=slot_minutes, slots=slots, seed=seed)
    total = float(config.get(CONF_INITIAL_TOTAL_KWH, 1000.0))
    rows: list[tuple[datetime, float]] = [(start_at, total)]
    for point in load_energy_points(config, ctx):
        total += float(point["value"])
        rows.append(
            (
                datetime.fromisoformat(point["start"])
                + timedelta(minutes=slot_minutes),
                total,
            )
        )
    return rows


def power_points_to_wh_hours(
    points: list[dict[str, Any]], *, slot_minutes: int
) -> dict[str, float]:
    """Convert power future values in watts to HA Energy wh_hours."""
    slot_hours = slot_minutes / 60.0
    return {str(point["start"]): float(point["value"]) * slot_hours for point in points}


def points_to_wh_hours(points: list[dict[str, Any]]) -> dict[str, float]:
    """Compatibility helper converting kWh values to HA Energy wh_hours."""
    return {str(point["start"]): float(point["value"]) * 1000.0 for point in points}
