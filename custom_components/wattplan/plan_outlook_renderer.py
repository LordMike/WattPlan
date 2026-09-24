"""English rendering for language-neutral Plan Outlook models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from .plan_outlook_types import OutlookFact, OutlookModel

VALUE_RANK = {"low": 0, "medium": 1, "high": 2}


def _time(value: datetime) -> str:
    return value.strftime("%H:%M")


def _range_text(start: datetime, end: datetime) -> str:
    return f"{_time(start)}-{_time(end)}"


def _period_label(value: datetime, *, now: datetime) -> str:
    if value.date() == now.date():
        return "today"
    if value.date() == (now + timedelta(days=1)).date():
        return "tomorrow"
    return value.strftime("%A")


def _stable_number(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _values(fact: OutlookFact) -> dict[str, Any]:
    return dict(fact.values)


def _value_time(values: dict[str, Any], key: str, fallback: datetime) -> datetime:
    value = values.get(key)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _duration_text(minutes: int) -> str:
    if minutes >= 120:
        return f"{minutes // 60} hours"
    if minutes >= 60:
        return "an hour"
    return f"{max(1, minutes)} minutes"


def _phrase_variant(
    fact: OutlookFact,
    *,
    model: OutlookModel,
    now: datetime,
    option_count: int,
    variation_seed: str,
) -> int:
    """Choose wording only after the semantic report model is complete."""
    local_hour = (
        now.astimezone(model.start.tzinfo).hour if model.start.tzinfo else now.hour
    )
    period = (
        "morning" if local_hour < 12 else "afternoon" if local_hour < 18 else "evening"
    )
    period_offset = {"morning": 0, "afternoon": 1, "evening": 2}[period]
    seed = (
        f"{model.seed_prefix}:{now.date().isoformat()}:{fact.fact_id}:"
        f"{variation_seed}:english"
    )
    return (_stable_number(seed) + period_offset) % option_count


def render_fact_english(
    fact: OutlookFact,
    *,
    model: OutlookModel,
    now: datetime,
    variation_seed: str,
    forced_variant: int | None = None,
) -> tuple[str, int]:
    """Render one fact and return its deterministic wording variant."""
    values = _values(fact)
    kind = fact.kind

    variants: tuple[str, ...] | None = None
    if kind == "grid_price_rise":
        variants = (
            "Grid prices rise later in the outlook.",
            "Grid prices build later in the outlook.",
            "Higher grid prices arrive later in the outlook.",
        )
    elif kind == "grid_price_fall":
        variants = (
            "Grid prices ease later in the outlook.",
            "Grid prices fall later in the outlook.",
            "Lower grid prices arrive later in the outlook.",
        )
    elif kind == "limited_grid_use":
        variants = (
            "Little grid use is expected across the covered period.",
            "Grid use is expected to remain limited across the covered period.",
            "Only limited grid use is expected across the covered period.",
        )
    if variants is not None:
        variant = (
            forced_variant % len(variants)
            if forced_variant is not None
            else _phrase_variant(
                fact,
                model=model,
                now=now,
                option_count=len(variants),
                variation_seed=variation_seed,
            )
        )
        return variants[variant], variant

    if kind == "flat_grid_prices":
        covered_from = max(fact.start, now)
        if covered_from.date() == now.date() and fact.start < now:
            label = "the rest of today"
        else:
            label = _period_label(covered_from, now=now)
        return f"Grid prices remain fairly level for {label}.", 0
    if kind == "negative_grid_price":
        return f"Grid prices dip below zero {_range_text(fact.start, fact.end)}.", 0
    if kind == "cheaper_grid_prices":
        return (
            (
                f"A cheaper grid-price stretch is expected "
                f"{_range_text(fact.start, fact.end)}."
            ),
            0,
        )
    if kind == "grid_price_swing":
        turn_at = _value_time(values, "turn_at", fact.start)
        if values.get("direction") == "ease_then_rise":
            return f"Grid prices ease around {_time(turn_at)}, then rise later.", 0
        return f"Grid prices rise around {_time(turn_at)}, then ease later.", 0
    if kind == "solar_surplus":
        peak_at = _value_time(values, "peak_at", fact.start)
        return (
            (
                f"Solar builds towards {_time(peak_at)}, with surplus expected "
                f"{_range_text(fact.start, fact.end)}."
            ),
            0,
        )
    if kind == "solar_modest":
        peak_at = _value_time(values, "peak_at", fact.start)
        return (
            (
                f"Solar builds gently towards {_time(peak_at)}, remaining below "
                "expected use."
            ),
            0,
        )
    if kind == "solar_fading":
        return f"Solar fades from around {_time(fact.start)}.", 0
    if kind == "low_reserve":
        return f"{fact.subject} reserves are near minimum.", 0
    if kind == "grid_charge":
        return f"Grid charging is planned {_range_text(fact.start, fact.end)}.", 0
    if kind == "battery_preserve":
        return (
            (
                f"{fact.subject} preservation is planned "
                f"{_range_text(fact.start, fact.end)}."
            ),
            0,
        )
    if kind == "battery_self_consume":
        return (
            (
                f"{fact.subject} self-consumption remains planned through most of "
                "the outlook."
            ),
            0,
        )
    if kind == "battery_full":
        return f"{fact.subject} is expected full by {_time(fact.start)}.", 0
    if kind == "target_shortfall":
        expected = values.get("expected_percent")
        requested = values.get("requested_percent")
        if isinstance(expected, int | float) and isinstance(requested, int | float):
            return (
                (
                    f"{fact.subject} is expected at {expected:.0f}% by "
                    f"{_time(fact.start)}, short of the requested {requested:.0f}%."
                ),
                0,
            )
        return f"{fact.subject} remains short of its {_time(fact.start)} target.", 0
    if kind == "target_reached":
        requested = values.get("requested_percent")
        if isinstance(requested, int | float):
            return (
                (
                    f"{fact.subject} reserves are expected to reach {requested:.0f}% "
                    f"by {_time(fact.start)}."
                ),
                0,
            )
        return (
            f"{fact.subject} is expected to reach its target by {_time(fact.start)}.",
            0,
        )
    if kind == "comfort_timing":
        return (
            (
                f"{fact.subject.capitalize()} is planned "
                f"{_range_text(fact.start, fact.end)}, overlapping expected solar "
                "production."
            ),
            0,
        )
    if kind == "optional_start":
        alternative = values.get("alternative_at")
        if isinstance(alternative, str):
            try:
                alternative_at = datetime.fromisoformat(alternative)
            except ValueError:
                alternative_at = None
            if alternative_at is not None:
                return (
                    (
                        f"The preferred {fact.subject} start is {_time(fact.start)}; "
                        f"an alternative is {_time(alternative_at)}."
                    ),
                    0,
                )
        return f"A favourable {fact.subject} start is {_time(fact.start)}.", 0
    if kind == "grid_export":
        return (
            (
                f"Surplus is expected to feed the grid "
                f"{_range_text(fact.start, fact.end)}."
            ),
            0,
        )
    if kind == "heavy_grid_use":
        return "Heavy grid use is expected across the covered period.", 0
    if kind == "charging_dominates_imports":
        return "Most grid use is expected during battery charging.", 0
    if kind == "grid_use_increase":
        return "Grid use is expected to increase later in the covered period.", 0
    if kind == "grid_use_decrease":
        return "Grid use is expected to ease later in the covered period.", 0
    if kind == "source_problem":
        elapsed = _duration_text(int(values.get("elapsed_minutes", 1) or 1))
        if fact.subject.endswith("pv"):
            if values.get("stale") is True:
                return (
                    (
                        f"Solar updates remain delayed for {elapsed}; the outlook "
                        "uses the earlier forecast."
                    ),
                    0,
                )
            return (
                (
                    f"Solar forecasts have been missing for {elapsed}, so the plan "
                    "currently allows no solar contribution."
                ),
                0,
            )
        label = fact.subject.removeprefix("source_").replace("_", " ")
        return f"{label.capitalize()} updates remain unavailable for {elapsed}.", 0
    if kind == "plan_refresh_failure":
        elapsed = _duration_text(int(values.get("elapsed_minutes", 1) or 1))
        return (
            (
                f"No fresh plan for {elapsed}; the last valid schedule still covers "
                "the current period."
            ),
            0,
        )
    if kind == "plan_unavailable":
        return "Planning is currently unavailable; no plan has been accepted.", 0
    if kind == "plan_unusable":
        return (
            "Planning is interrupted; the retained schedule is not currently usable.",
            0,
        )
    if kind == "plan_expired":
        return "Planning remains interrupted; the previous schedule has expired.", 0
    if kind == "restored_unvalidated":
        return "A fresh plan is still awaited after restart.", 0
    if kind == "recommendations_unavailable":
        return "No current charging or appliance recommendations are available.", 0
    if kind == "stored_recommendations_unvalidated":
        return (
            (
                "Stored charging and appliance suggestions are not yet available as "
                "current recommendations."
            ),
            0,
        )
    if kind == "quiet":
        return (
            "No material plan change is expected in the remaining covered period.",
            0,
        )
    return kind.replace("_", " ").capitalize() + ".", 0


def _semantic_fact_payload(fact: OutlookFact) -> dict[str, Any]:
    """Return only values that define the meaning of the rendered statement."""
    payload: dict[str, Any] = {
        "id": fact.fact_id,
        "kind": fact.kind,
        "topic": fact.topic,
        "information_value": fact.information_value,
        "subject": fact.subject,
        "values": dict(fact.values),
    }
    interval_kinds = {
        "negative_grid_price",
        "cheaper_grid_prices",
        "solar_surplus",
        "grid_charge",
        "battery_preserve",
        "comfort_timing",
        "grid_export",
    }
    point_kinds = {
        "solar_fading",
        "battery_full",
        "target_shortfall",
        "target_reached",
        "optional_start",
    }
    if fact.kind in interval_kinds:
        payload["start"] = fact.start.isoformat()
        payload["end"] = fact.end.isoformat()
    elif fact.kind in point_kinds:
        payload["start"] = fact.start.isoformat()
    return payload


def _semantic_id(
    selected: tuple[OutlookFact, ...], *, information_value: str
) -> str:
    payload = [_semantic_fact_payload(fact) for fact in selected]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:10]
    return f"{selected[0].kind}_{information_value}_{digest}"


def render_plan_outlook(
    model: OutlookModel,
    *,
    now: datetime,
    previous_outlook: dict[str, Any] | None = None,
    variation_seed: str | None = None,
) -> dict[str, Any]:
    """Render one selected model without changing its semantic content."""
    selected = model.selected_facts
    if not selected:
        return {}

    information_value = max(
        (fact.information_value for fact in selected), key=VALUE_RANK.__getitem__
    )
    semantic_id = _semantic_id(selected, information_value=information_value)
    previous_variants: dict[str, Any] = {}
    if (
        isinstance(previous_outlook, dict)
        and previous_outlook.get("semantic_id") == semantic_id
        and isinstance(previous_outlook.get("_render_variants"), dict)
    ):
        previous_variants = previous_outlook["_render_variants"]

    rendered: list[str] = []
    variants: dict[str, int] = {}
    effective_seed = variation_seed or semantic_id
    for fact in selected:
        previous_variant = previous_variants.get(fact.fact_id)
        text, variant = render_fact_english(
            fact,
            model=model,
            now=now,
            variation_seed=effective_seed,
            forced_variant=(
                previous_variant if isinstance(previous_variant, int) else None
            ),
        )
        rendered.append(text)
        variants[fact.fact_id] = variant

    line_1 = rendered[0]
    line_2 = " ".join(rendered[1:])
    topic_fact = next(
        (fact for fact in selected if fact.topic == "reliability"),
        max(selected, key=lambda fact: VALUE_RANK[fact.information_value]),
    )
    report_payload = {
        "facts": [
            {
                "id": fact.fact_id,
                "kind": fact.kind,
                "topic": fact.topic,
                "information_value": fact.information_value,
                "subject": fact.subject,
                "values": dict(fact.values),
            }
            for fact in selected
        ],
        "variants": variants,
        "rendered": rendered,
    }
    digest = hashlib.sha256(
        json.dumps(report_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:10]
    report_id = f"{selected[0].kind}_{information_value}_{digest}"
    return {
        "report_id": report_id,
        "semantic_id": semantic_id,
        "headline": line_1,
        "line_1": line_1,
        "line_2": line_2,
        "text": " ".join(rendered),
        "language": "en",
        "information_value": information_value,
        "topic": topic_fact.topic,
        "selected_facts": [fact.fact_id for fact in selected],
        "fact_details": [
            {
                "id": fact.fact_id,
                "basis": fact.basis,
                "start": fact.start.isoformat(),
                "end": fact.end.isoformat(),
                "required_inputs": list(fact.required_inputs),
                "values": dict(fact.values),
            }
            for fact in selected
        ],
        "basis": sorted({fact.basis for fact in selected}),
        "covered_start": max(model.start, now).isoformat(),
        "covered_end": model.horizon_end.isoformat(),
        "valid_until": model.horizon_end.isoformat(),
        "plan_created_at": (
            model.plan_created_at.isoformat()
            if model.plan_created_at is not None
            else None
        ),
        "_render_variants": variants,
    }
