"""Build language-neutral Plan Outlook models from accepted schedules."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .plan_outlook_renderer import render_plan_outlook
from .plan_outlook_selection import build_statements, select_best_set
from .plan_outlook_types import (
    OutlookFact,
    OutlookModel,
    OutlookValue,
    model_from_dict,
    model_to_dict,
    semantic_key,
)

VALUE_RANK = {"low": 0, "medium": 1, "high": 2}
INFO_TO_SIGNIFICANCE = {"low": 0.2, "medium": 0.5, "high": 0.9}
BASIS_CONFIDENCE = {"observed": 0.9, "planned": 0.85, "forecast": 0.8}
KIND_DEVIATION = {
    "negative_grid_price": 0.9,
    "target_shortfall": 0.8,
    "heavy_grid_use": 0.7,
    "low_reserve": 0.7,
    "source_problem": 0.7,
    "plan_refresh_failure": 0.8,
    "grid_price_rise": 0.6,
    "grid_price_fall": 0.6,
    "grid_price_swing": 0.6,
    "solar_surplus": 0.5,
    "grid_export": 0.5,
    "grid_use_increase": 0.5,
    "grid_use_decrease": 0.5,
    "charging_dominates_imports": 0.5,
    "battery_full": 0.5,
    "cheaper_grid_prices": 0.4,
    "target_reached": 0.4,
    "comfort_timing": 0.4,
    "optional_start": 0.4,
    "limited_grid_use": 0.4,
    "grid_charge": 0.3,
    "battery_preserve": 0.2,
    "solar_fading": 0.2,
    "solar_modest": 0.15,
    "flat_grid_prices": 0.1,
    "battery_self_consume": 0.1,
    "quiet": 0.05,
}
MAX_HISTORY = 12
FACT_GROUPS = {
    "flat_grid_prices": "price",
    "negative_grid_price": "price",
    "cheaper_grid_prices": "price",
    "grid_price_rise": "price",
    "grid_price_fall": "price",
    "grid_price_swing": "price",
    "solar_surplus": "solar",
    "solar_modest": "solar",
    "solar_fading": "solar",
    "grid_charge": "battery_action",
    "battery_preserve": "battery_action",
    "battery_self_consume": "battery_action",
    "low_reserve": "battery_state",
    "battery_full": "battery_state",
    "target_shortfall": "target",
    "target_reached": "target",
    "comfort_timing": "load_timing",
    "optional_start": "load_timing",
    "grid_export": "grid_balance",
    "limited_grid_use": "grid_balance",
    "heavy_grid_use": "grid_balance",
    "charging_dominates_imports": "grid_balance",
    "grid_use_increase": "grid_balance",
    "grid_use_decrease": "grid_balance",
}
INCOMPATIBLE_FACT_PAIRS = {
    frozenset(("solar_surplus", "grid_export")),
}


def _display_duration_minutes(minutes: int) -> int:
    """Return the duration precision that the renderer will actually display."""
    minutes = max(1, minutes)
    if minutes < 60:
        return minutes
    if minutes < 120:
        return 60
    return minutes // 60 * 60


def _at(start: datetime, slots: int, slot_minutes: int) -> datetime:
    """Advance by real slot duration while retaining the display timezone."""
    return (start.astimezone(UTC) + timedelta(minutes=slots * slot_minutes)).astimezone(
        start.tzinfo
    )


def _runs(indices: Iterable[int]) -> list[tuple[int, int]]:
    """Return inclusive/exclusive consecutive index runs."""
    ordered = sorted(set(indices))
    if not ordered:
        return []
    runs: list[tuple[int, int]] = []
    run_start = previous = ordered[0]
    for index in ordered[1:]:
        if index != previous + 1:
            runs.append((run_start, previous + 1))
            run_start = index
        previous = index
    runs.append((run_start, previous + 1))
    return runs


def _interval(
    run: tuple[int, int], start: datetime, slot_minutes: int
) -> tuple[datetime, datetime]:
    return (
        _at(start, run[0], slot_minutes),
        _at(start, run[1], slot_minutes),
    )


def _source_is_usable(provenance: dict[str, Any], key: str) -> bool:
    source = provenance.get(key)
    if not isinstance(source, dict) or not source.get("configured", False):
        return False
    return bool(source.get("trusted", source.get("status", "ok") == "ok"))


def _schedule_slots(entity: dict[str, Any], state: str) -> list[int]:
    return [
        index
        for index, point in enumerate(entity.get("schedule", []))
        if isinstance(point, dict) and point.get("state") == state
    ]


def _fact(
    kind: str,
    topic: str,
    value: str,
    basis: str,
    start: datetime,
    end: datetime,
    *,
    subject: str = "site",
    related: tuple[str, ...] = (),
    required_inputs: tuple[str, ...] = (),
    values: tuple[tuple[str, OutlookValue], ...] = (),
    deviation: float | None = None,
    confidence: float | None = None,
) -> OutlookFact:
    if not required_inputs:
        if kind.startswith("grid_price") or kind in {
            "negative_grid_price",
            "flat_grid_prices",
            "cheaper_grid_prices",
        }:
            required_inputs = ("import_price",)
        elif kind.startswith("solar"):
            required_inputs = ("pv", "usage")
        elif kind in {
            "grid_export",
            "limited_grid_use",
            "heavy_grid_use",
            "charging_dominates_imports",
            "grid_use_increase",
            "grid_use_decrease",
        }:
            required_inputs = ("usage", "pv", "energy_flows")
        elif kind == "source_problem":
            required_inputs = ("source_health",)
        elif kind == "optional_start":
            required_inputs = ("optional_options",)
        elif kind == "comfort_timing":
            required_inputs = ("comfort_schedule", "pv")
        else:
            required_inputs = ("battery_schedule",)
    return OutlookFact(
        fact_id=f"{kind}:{subject}",
        kind=kind,
        topic=topic,
        basis=basis,
        start=start,
        end=end,
        subject=subject,
        related=related,
        required_inputs=required_inputs,
        values=values,
        significance=INFO_TO_SIGNIFICANCE.get(value, 0.5),
        confidence=(
            confidence
            if confidence is not None
            else BASIS_CONFIDENCE.get(basis, 0.8)
        ),
        deviation=(
            deviation if deviation is not None else KIND_DEVIATION.get(kind, 0.4)
        ),
    )


def _price_facts(
    prices: list[float], start: datetime, slot_minutes: int, now: datetime
) -> list[OutlookFact]:
    if len(prices) < 2:
        return []
    end = _at(start, len(prices), slot_minutes)
    low, high = min(prices), max(prices)
    spread = high - low
    scale = max(abs(low), abs(high), 0.01)
    meaningful = spread >= max(scale * 0.18, 0.005)
    negative = [index for index, value in enumerate(prices) if value < 0]
    facts: list[OutlookFact] = []
    if negative:
        run = max(_runs(negative), key=lambda item: item[1] - item[0])
        run_start, run_end = _interval(run, start, slot_minutes)
        facts.append(
            _fact(
                "negative_grid_price",
                "opportunity",
                "high",
                "forecast",
                run_start,
                run_end,
                related=("grid_charge",),
            )
        )
    if not meaningful:
        facts.append(
            _fact(
                "flat_grid_prices",
                "routine",
                "low",
                "forecast",
                start,
                end,
            )
        )
        return facts

    cheap = [index for index, value in enumerate(prices) if value <= low + spread * 0.2]
    cheap_runs = [run for run in _runs(cheap) if run[1] - run[0] >= 2]
    if cheap_runs and not negative:
        run_start, run_end = _interval(
            max(cheap_runs, key=lambda item: item[1] - item[0]),
            start,
            slot_minutes,
        )
        facts.append(
            _fact(
                "cheaper_grid_prices",
                "opportunity",
                "medium",
                "forecast",
                run_start,
                run_end,
                related=("grid_charge", "optional_start"),
            )
        )

    third = max(1, len(prices) // 3)
    early = sum(prices[:third]) / third
    late = sum(prices[-third:]) / third
    values: tuple[tuple[str, OutlookValue], ...] = ()
    if late > early + spread * 0.25:
        kind = "grid_price_rise"
    elif late < early - spread * 0.25:
        kind = "grid_price_fall"
    else:
        minimum = prices.index(low)
        maximum = prices.index(high)
        if minimum < maximum:
            turn_at = _at(start, minimum, slot_minutes)
            direction = "ease_then_rise"
        else:
            turn_at = _at(start, maximum, slot_minutes)
            direction = "rise_then_ease"
        kind = "grid_price_swing"
        values = (("turn_at", turn_at.isoformat()), ("direction", direction))
    strong = spread >= scale * 0.45
    facts.append(
        _fact(
            kind,
            "opportunity",
            "high" if strong else "medium",
            "forecast",
            start,
            end,
            related=("grid_charge", "battery_preserve", "optional_start"),
            values=values,
            deviation=0.8 if strong else 0.4,
        )
    )
    return facts


def _solar_facts(
    solar: list[float], usage: list[float], start: datetime, slot_minutes: int
) -> list[OutlookFact]:
    if not solar or max(solar, default=0.0) <= 0:
        return []
    end = _at(start, len(solar), slot_minutes)
    total = sum(solar)
    peak_index = max(range(len(solar)), key=solar.__getitem__)
    peak_at = _at(start, peak_index, slot_minutes)
    scheduled_use = usage[: len(solar)]
    surplus = [
        index
        for index, value in enumerate(solar)
        if index < len(scheduled_use) and value - scheduled_use[index] >= 0.05
    ]
    if len(surplus) >= 2:
        run = max(_runs(surplus), key=lambda item: item[1] - item[0])
        run_start, run_end = _interval(run, start, slot_minutes)
        facts = [
            _fact(
                "solar_surplus",
                "energy_balance",
                "medium",
                "forecast",
                run_start,
                run_end,
                related=(
                    "battery_full",
                    "grid_export",
                    "optional_start",
                    "comfort_timing",
                ),
                values=(("peak_at", peak_at.isoformat()),),
            )
        ]
    elif total >= 0.2:
        facts = [
            _fact(
                "solar_modest",
                "routine",
                "low",
                "forecast",
                start,
                end,
                values=(("peak_at", peak_at.isoformat()),),
            )
        ]
    else:
        facts = []
    tail = solar[-max(2, len(solar) // 4) :]
    if (
        peak_index < len(solar) * 0.75
        and tail
        and max(tail) <= solar[peak_index] * 0.35
    ):
        fade_at = _at(start, len(solar) - len(tail), slot_minutes)
        facts.append(
            _fact(
                "solar_fading",
                "routine",
                "low",
                "forecast",
                fade_at,
                end,
                related=("grid_use_increase", "battery_preserve"),
            )
        )
    return facts


def _battery_facts(
    batteries: list[dict[str, Any]],
    params: dict[str, Any],
    start: datetime,
    slot_minutes: int,
    now: datetime,
) -> list[OutlookFact]:
    facts: list[OutlookFact] = []
    param_by_name = {
        str(item.get("name")): item for item in params.get("battery_entities", [])
    }
    for entity in batteries:
        name = str(entity.get("name", "Battery"))
        schedule = entity.get("schedule", [])
        settings = param_by_name.get(name, {})
        capacity = float(settings.get("capacity_kwh", 0) or 0)
        initial = float(settings.get("initial_kwh", 0) or 0)
        minimum = float(settings.get("minimum_kwh", 0) or 0)
        initial_known = settings.get("initial_kwh", None) is not None
        if capacity > 0 and initial <= minimum + max(capacity * 0.05, 0.1):
            facts.append(
                _fact(
                    "low_reserve",
                    "battery",
                    "high",
                    "observed",
                    start,
                    _at(start, 1, slot_minutes),
                    subject=name,
                    related=("grid_charge", "target_shortfall"),
                    # Unknown starting SoC is weak evidence: keep the fact
                    # but let eligibility filtering suppress it.
                    confidence=0.9 if initial_known else 0.3,
                )
            )

        for state, kind in (
            ("grid_charge", "grid_charge"),
            ("preserve", "battery_preserve"),
        ):
            runs = _runs(_schedule_slots(entity, state))
            if not runs:
                continue
            run = next(
                (item for item in runs if _at(start, item[1], slot_minutes) > now),
                runs[-1],
            )
            run_start, run_end = _interval(run, start, slot_minutes)
            facts.append(
                _fact(
                    kind,
                    "battery",
                    "medium",
                    "planned",
                    run_start,
                    run_end,
                    subject=name,
                    related=(
                        "grid_price_rise",
                        "grid_price_fall",
                        "negative_grid_price",
                        "low_reserve",
                    ),
                )
            )

        if (
            not _schedule_slots(entity, "grid_charge")
            and not _schedule_slots(entity, "preserve")
            and schedule
        ):
            facts.append(
                _fact(
                    "battery_self_consume",
                    "routine",
                    "low",
                    "planned",
                    start,
                    _at(start, len(schedule), slot_minutes),
                    subject=name,
                )
            )

        if capacity > 0:
            for index, point in enumerate(schedule):
                if (
                    isinstance(point, dict)
                    and float(point.get("level", 0) or 0) >= capacity * 0.98
                ):
                    at = _at(start, index + 1, slot_minutes)
                    facts.append(
                        _fact(
                            "battery_full",
                            "battery",
                            "medium",
                            "forecast",
                            at,
                            at,
                            subject=name,
                            related=("solar_surplus",),
                        )
                    )
                    break

        target = settings.get("target")
        if isinstance(target, dict) and schedule:
            target_slot = int(target.get("timeslot", -1))
            if 0 <= target_slot < len(schedule):
                point = schedule[target_slot]
                expected = (
                    float(point.get("level", 0) or 0) if isinstance(point, dict) else 0
                )
                requested = float(target.get("soc_kwh", 0) or 0)
                at = _at(start, target_slot + 1, slot_minutes)
                if expected + 0.05 < requested:
                    values = (
                        (
                            "expected_percent",
                            round(expected / capacity * 100) if capacity else None,
                        ),
                        (
                            "requested_percent",
                            round(requested / capacity * 100) if capacity else None,
                        ),
                    )
                    facts.append(
                        _fact(
                            "target_shortfall",
                            "target",
                            "high",
                            "forecast",
                            at,
                            at,
                            subject=name,
                            related=("grid_charge",),
                            values=values,
                        )
                    )
                else:
                    facts.append(
                        _fact(
                            "target_reached",
                            "target",
                            "medium",
                            "forecast",
                            at,
                            at,
                            subject=name,
                            related=("grid_charge",),
                            values=(
                                (
                                    "requested_percent",
                                    round(requested / capacity * 100)
                                    if capacity
                                    else None,
                                ),
                            ),
                        )
                    )
    return facts


def _comfort_facts(
    entities: list[dict[str, Any]],
    start: datetime,
    slot_minutes: int,
    solar: list[float],
) -> list[OutlookFact]:
    facts: list[OutlookFact] = []
    for entity in entities:
        enabled = [
            index
            for index, point in enumerate(entity.get("schedule", []))
            if isinstance(point, dict) and point.get("enabled") is True
        ]
        if not enabled:
            continue
        run = _runs(enabled)[0]
        run_start, run_end = _interval(run, start, slot_minutes)
        overlap = any(
            index < len(solar) and solar[index] >= 0.05 for index in range(*run)
        )
        if overlap:
            name = str(entity.get("name", "Comfort load"))
            facts.append(
                _fact(
                    "comfort_timing",
                    "opportunity",
                    "medium",
                    "planned",
                    run_start,
                    run_end,
                    subject=name,
                    related=("solar_surplus",),
                )
            )
    return facts


def _optional_facts(
    optionals: list[dict[str, Any]], start: datetime, slot_minutes: int, now: datetime
) -> list[OutlookFact]:
    facts: list[OutlookFact] = []
    for optional in optionals:
        valid: list[tuple[int, datetime]] = []
        for option in optional.get("options", []):
            slot = int(option.get("start_timeslot", -1))
            when = _at(start, slot, slot_minutes)
            if slot >= 0 and when >= now:
                valid.append((slot, when))
        if not valid:
            continue
        name = str(optional.get("name", "appliance"))
        preferred = valid[0][1]
        values: tuple[tuple[str, OutlookValue], ...] = ()
        if len(valid) > 1:
            values = (("alternative_at", valid[1][1].isoformat()),)
        facts.append(
            _fact(
                "optional_start",
                "opportunity",
                "medium",
                "planned",
                preferred,
                preferred,
                subject=name,
                related=("solar_surplus", "grid_price_rise", "grid_price_fall"),
                values=values,
            )
        )
    return facts


def _balance_facts(
    flows: list[dict[str, Any]], start: datetime, slot_minutes: int
) -> list[OutlookFact]:
    if len(flows) < 2:
        return []
    end = _at(start, len(flows), slot_minutes)
    imports = [max(0.0, float(item.get("grid_import_kwh", 0) or 0)) for item in flows]
    exports = [max(0.0, float(item.get("grid_export_kwh", 0) or 0)) for item in flows]
    base_and_comfort = [
        max(
            0.0,
            float(item.get("base_load_kwh", 0) or 0)
            + float(item.get("comfort_load_kwh", 0) or 0),
        )
        for item in flows
    ]
    grid_charge = [
        max(0.0, float(item.get("grid_charge_kwh", 0) or 0)) for item in flows
    ]
    total_import = sum(imports)
    expected_use = sum(base_and_comfort)
    facts: list[OutlookFact] = []
    export_slots = [index for index, value in enumerate(exports) if value >= 0.05]
    if len(export_slots) >= 2 and sum(exports) >= 0.2:
        run = max(_runs(export_slots), key=lambda item: item[1] - item[0])
        run_start, run_end = _interval(run, start, slot_minutes)
        facts.append(
            _fact(
                "grid_export",
                "energy_balance",
                "medium",
                "forecast",
                run_start,
                run_end,
                related=("solar_surplus", "battery_full"),
            )
        )
    if total_import <= max(0.25, expected_use * 0.12):
        facts.append(
            _fact(
                "limited_grid_use",
                "energy_balance",
                "medium",
                "forecast",
                start,
                end,
                related=("solar_surplus", "grid_export"),
            )
        )
    elif expected_use > 0 and total_import >= max(1.0, expected_use * 0.8):
        facts.append(
            _fact(
                "heavy_grid_use",
                "energy_balance",
                "high",
                "forecast",
                start,
                end,
                related=("grid_price_fall", "grid_charge"),
            )
        )
    if total_import >= 0.5 and sum(grid_charge) >= total_import * 0.6:
        facts = [fact for fact in facts if fact.kind != "heavy_grid_use"]
        facts.append(
            _fact(
                "charging_dominates_imports",
                "energy_balance",
                "medium",
                "forecast",
                start,
                end,
                related=("grid_charge",),
            )
        )
    split = max(1, len(imports) // 2)
    early_import = sum(imports[:split])
    later_import = sum(imports[split:])
    if later_import >= max(0.5, early_import * 1.8):
        facts.append(
            _fact(
                "grid_use_increase",
                "energy_balance",
                "medium",
                "forecast",
                _at(start, split, slot_minutes),
                end,
                related=("solar_fading", "grid_price_fall"),
            )
        )
    elif early_import >= max(0.5, later_import * 1.8):
        facts.append(
            _fact(
                "grid_use_decrease",
                "energy_balance",
                "medium",
                "forecast",
                start,
                end,
                related=("solar_surplus",),
            )
        )
    return facts


def _reliability_facts(
    source_health: dict[str, dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    now: datetime | None = None,
) -> list[OutlookFact]:
    facts: list[OutlookFact] = []
    for key, status in source_health.items():
        if (
            not isinstance(status, dict)
            or status.get("status") == "ok"
            or status.get("configured") is False
        ):
            continue
        count = int(status.get("failure_slot_count", 0) or 0)
        if count < 4:
            continue
        minutes = int(status.get("failure_elapsed_minutes", 0) or 0)
        started_at = status.get("failure_started_at")
        if isinstance(started_at, str):
            try:
                started = datetime.fromisoformat(started_at)
                current = now or datetime.now(tz=UTC)
                if current.tzinfo is None:
                    current = current.replace(tzinfo=UTC)
                else:
                    current = current.astimezone(UTC)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                else:
                    started = started.astimezone(UTC)
                minutes = max(1, int((current - started).total_seconds() // 60))
            except ValueError:
                pass
        stale = bool(status.get("is_stale"))
        facts.append(
            _fact(
                "source_problem",
                "reliability",
                "medium" if stale else "high",
                "observed",
                start,
                end,
                subject=key,
                values=(
                    ("stale", stale),
                    ("elapsed_minutes", _display_duration_minutes(minutes)),
                ),
            )
        )
    return facts


def _select_facts(
    facts: list[OutlookFact],
    *,
    previous: OutlookModel | None,
    now: datetime | None = None,
) -> list[OutlookFact]:
    """Select semantic facts via salience-scored candidate sets."""
    if not facts:
        return []
    history = list(previous.selection_history[-MAX_HISTORY:]) if previous else []
    if now is None:
        try:
            now = min(fact.start for fact in facts)
        except ValueError:
            return []
    return select_best_set(facts, now, history)


def _facts_are_compatible(left: OutlookFact, right: OutlookFact) -> bool:
    """Return whether two facts can form one concise, non-redundant report."""
    left_group = FACT_GROUPS.get(left.kind)
    right_group = FACT_GROUPS.get(right.kind)
    if left_group is not None and left_group == right_group:
        return False
    if frozenset((left.kind, right.kind)) in INCOMPATIBLE_FACT_PAIRS:
        return False
    if "negative_grid_price" in {left.kind, right.kind}:
        return {left.kind, right.kind} == {"negative_grid_price", "grid_charge"}
    return True


def _select_model(
    facts: list[OutlookFact],
    *,
    start: datetime,
    horizon_end: datetime,
    now: datetime,
    seed_prefix: str,
    plan_created_at: datetime | None,
    previous: OutlookModel | None,
) -> OutlookModel:
    """Return the language-neutral report model for currently valid facts."""
    facts = [fact for fact in facts if fact.end >= now]
    history = list(previous.selection_history[-MAX_HISTORY:]) if previous else []
    selected = select_best_set(facts, now, history)
    if not selected:
        quiet = _fact(
            "quiet",
            "routine",
            "low",
            "forecast",
            max(start, now),
            horizon_end,
        )
        facts.append(quiet)
        selected = [quiet]

    statements = build_statements(selected)
    selected_ids = tuple(fact.fact_id for fact in selected)
    previous_ids = previous.selected_fact_ids if previous else ()
    if selected_ids != previous_ids:
        try:
            display_tz = start.tzinfo
            history.append(
                semantic_key(
                    selected[0].kind,
                    selected[0].significance,
                    selected[0].deviation,
                    selected[0].start,
                    display_tz,
                )
            )
        except Exception:
            history.append(selected[0].kind)
    return OutlookModel(
        facts=tuple(facts),
        selected_fact_ids=selected_ids,
        selection_history=tuple(history[-MAX_HISTORY:]),
        start=start,
        horizon_end=horizon_end,
        plan_created_at=plan_created_at,
        seed_prefix=seed_prefix,
        statements=tuple(statements),
    )


def _status_model(
    kind: str,
    *,
    now: datetime,
    horizon_end: datetime | None = None,
    seed_prefix: str = "",
) -> OutlookModel:
    """Return a semantic model for a plan availability status."""
    end = horizon_end or now
    value = "medium" if kind == "restored_unvalidated" else "high"
    facts = [_fact(kind, "reliability", value, "observed", now, end, subject="plan")]
    companion_kind = (
        "stored_recommendations_unvalidated"
        if kind == "restored_unvalidated"
        else "recommendations_unavailable"
    )
    facts.append(
        _fact(
            companion_kind,
            "reliability",
            value,
            "observed",
            now,
            end,
            subject="plan",
        )
    )
    return _select_model(
        facts,
        start=now,
        horizon_end=end,
        now=now,
        seed_prefix=seed_prefix,
        plan_created_at=None,
        previous=None,
    )


def build_status_plan_outlook(
    kind: str,
    *,
    now: datetime,
    horizon_end: datetime | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Build and render a plan availability report."""
    model = _status_model(kind, now=now, horizon_end=horizon_end)
    rendered = _render_outlook(model, now=now, language=language)
    rendered["_model"] = model_to_dict(model)
    return rendered


def _render_outlook(
    model: OutlookModel,
    *,
    now: datetime,
    language: str,
    previous_outlook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render one selected model in the requested language."""
    rendered = render_plan_outlook(
        model,
        now=now,
        language=language,
        previous_outlook=previous_outlook,
    )
    return rendered


def _previous_rendering(
    outlook: dict[str, Any], language: str
) -> dict[str, Any] | None:
    """Return the retained rendering for one language, including old English data."""
    renderings = outlook.get("_renderings")
    if isinstance(renderings, dict) and isinstance(renderings.get(language), dict):
        return renderings[language]
    if language == "en":
        return outlook
    return None


def render_stored_plan_outlook(
    outlook: dict[str, Any],
    *,
    now: datetime,
    source_health: dict[str, dict[str, Any]] | None = None,
    plan_failure_status: dict[str, Any] | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Re-select and render a retained semantic model against current status."""
    raw_model = outlook.get("_model")
    if not isinstance(raw_model, dict) or (model := model_from_dict(raw_model)) is None:
        return build_status_plan_outlook("plan_unavailable", now=now, language=language)
    start = model.start
    horizon_end = model.horizon_end
    if now.tzinfo is None:
        now = now.replace(tzinfo=start.tzinfo or UTC)
    elif start.tzinfo is not None:
        now = now.astimezone(start.tzinfo)
    facts = [
        fact
        for fact in model.facts
        if fact.kind not in {"source_problem", "plan_refresh_failure", "quiet"}
    ]
    facts.extend(_reliability_facts(source_health or {}, start, horizon_end, now=now))
    failure = plan_failure_status or {}
    if int(failure.get("slot_count", 0) or 0) >= 4:
        started_at = failure.get("started_at")
        elapsed_minutes = 1
        if isinstance(started_at, datetime):
            elapsed_minutes = max(
                1,
                int(
                    (now.astimezone(UTC) - started_at.astimezone(UTC)).total_seconds()
                    // 60
                ),
            )
        facts.append(
            _fact(
                "plan_refresh_failure",
                "reliability",
                "high",
                "observed",
                start,
                horizon_end,
                subject="plan",
                values=(
                    (
                        "elapsed_minutes",
                        _display_duration_minutes(elapsed_minutes),
                    ),
                ),
            )
        )
    current_model = _select_model(
        facts,
        start=start,
        horizon_end=horizon_end,
        now=now,
        seed_prefix=model.seed_prefix,
        plan_created_at=model.plan_created_at,
        previous=model,
    )
    rendered = _render_outlook(
        current_model,
        now=now,
        language=language,
        previous_outlook=_previous_rendering(outlook, language),
    )
    rendered["_model"] = model_to_dict(current_model)
    return rendered


def build_plan_outlook_model(
    *,
    request: dict[str, Any],
    result: dict[str, Any],
    source_health: dict[str, dict[str, Any]] | None = None,
    plan_validated: bool = True,
    now: datetime | None = None,
    previous_outlook: dict[str, Any] | None = None,
) -> OutlookModel | None:
    """Build selected semantic content without rendering any prose."""
    params = request["optimizer_params"]
    start = request["window"].start_at
    try:
        display_zone = ZoneInfo(str(request.get("local_timezone", "UTC")))
    except ZoneInfoNotFoundError:
        display_zone = ZoneInfo("UTC")
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    start = start.astimezone(display_zone)
    slot_minutes = int(request["slot_minutes"])
    prices = [float(value) for value in params.get("grid_import_price_per_kwh", [])]
    horizon_end = _at(start, len(prices), slot_minutes)
    now = now or start
    if now.tzinfo is None and start.tzinfo is not None:
        now = now.replace(tzinfo=start.tzinfo)
    elif now.tzinfo is not None:
        now = now.astimezone(display_zone)
    if not prices:
        return None
    if not plan_validated:
        return _status_model(
            "restored_unvalidated",
            now=now,
            horizon_end=horizon_end,
            seed_prefix=str(request.get("entry_id", "")),
        )

    provenance = request.get("source_provenance", {})
    source_health = source_health or {}
    health_names = {
        "source_import_price": "import_price",
        "source_export_price": "export_price",
        "source_usage": "usage",
        "source_pv": "pv",
    }
    provenance = {
        key: dict(value) if isinstance(value, dict) else {}
        for key, value in provenance.items()
    }
    for source_key, status in source_health.items():
        name = health_names.get(source_key, source_key.removeprefix("source_"))
        target = provenance.setdefault(name, {"configured": True})
        if isinstance(status, dict):
            target["status"] = status.get("status", "ok")
            target["trusted"] = status.get("status", "ok") == "ok"
    usage = [float(value) for value in params.get("usage_kwh", [])]
    solar = [float(value) for value in params.get("solar_input_kwh", [])]
    entities = [item for item in result.get("entities", []) if isinstance(item, dict)]
    batteries = [item for item in entities if item.get("type") == "battery"]
    comforts = [item for item in entities if item.get("type") == "comfort"]
    facts = _price_facts(prices, start, slot_minutes, now)
    if _source_is_usable(provenance, "pv"):
        facts.extend(_solar_facts(solar, usage, start, slot_minutes))
    facts.extend(_battery_facts(batteries, params, start, slot_minutes, now))
    facts.extend(
        _comfort_facts(
            comforts,
            start,
            slot_minutes,
            solar if _source_is_usable(provenance, "pv") else [],
        )
    )
    facts.extend(
        _optional_facts(
            result.get("optional_entity_options", []), start, slot_minutes, now
        )
    )
    flows = result.get("energy_flows", {}).get("per_slot", [])
    if (
        _source_is_usable(provenance, "usage")
        and _source_is_usable(provenance, "pv")
        and isinstance(flows, list)
    ):
        facts.extend(_balance_facts(flows, start, slot_minutes))
    facts.extend(_reliability_facts(source_health, start, horizon_end, now=now))
    previous_model = None
    if isinstance(previous_outlook, dict):
        raw_previous = previous_outlook.get("_model")
        if isinstance(raw_previous, dict):
            previous_model = model_from_dict(raw_previous)
    return _select_model(
        facts,
        start=start,
        horizon_end=horizon_end,
        now=now,
        seed_prefix=str(request.get("entry_id", "")),
        plan_created_at=now,
        previous=previous_model,
    )


def build_plan_outlook(
    *,
    request: dict[str, Any],
    result: dict[str, Any],
    source_health: dict[str, dict[str, Any]] | None = None,
    plan_validated: bool = True,
    now: datetime | None = None,
    previous_outlook: dict[str, Any] | None = None,
    languages: tuple[str, ...] = ("en",),
) -> dict[str, Any]:
    """Build one semantic model and render it in each active language."""
    model = build_plan_outlook_model(
        request=request,
        result=result,
        source_health=source_health,
        plan_validated=plan_validated,
        now=now,
        previous_outlook=previous_outlook,
    )
    if model is None:
        return {}
    render_at = now or model.plan_created_at or model.start
    if render_at.tzinfo is None:
        render_at = render_at.replace(tzinfo=model.start.tzinfo or UTC)
    elif model.start.tzinfo is not None:
        render_at = render_at.astimezone(model.start.tzinfo)
    renderings: dict[str, dict[str, Any]] = {}
    for language in languages:
        renderings[language] = _render_outlook(
            model,
            now=render_at,
            language=language,
            previous_outlook=(
                _previous_rendering(previous_outlook, language)
                if isinstance(previous_outlook, dict)
                else None
            ),
        )
    if not renderings:
        return {}
    rendered = dict(next(iter(renderings.values())))
    rendered["_model"] = model_to_dict(model)
    rendered["_renderings"] = renderings
    return rendered
