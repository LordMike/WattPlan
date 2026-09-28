"""Project Fluent rendering for language-neutral Plan Outlook models."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fluent.runtime import FluentBundle
from fluent.syntax import FluentParser
from fluent.syntax.ast import Junk

from .plan_outlook_types import OutlookFact, OutlookModel

VALUE_RANK = {"low": 0, "medium": 1, "high": 2}

_BATTERY_KINDS = {
    "low_reserve",
    "grid_charge",
    "battery_preserve",
    "battery_self_consume",
    "battery_full",
    "target_shortfall",
    "target_reached",
}

_SOLO_CAPABLE_IDS = {
    "outlook-low-reserve",
    "outlook-grid-charge",
    "outlook-battery-preserve",
    "outlook-battery-self-consume",
    "outlook-battery-full",
    "outlook-target-shortfall-known",
    "outlook-target-shortfall-missing",
    "outlook-target-reached-known",
    "outlook-target-reached-missing",
}


def _solo_battery_subject(model: OutlookModel | None) -> str | None:
    """Return the single battery subject, or None when naming is needed."""
    if model is None:
        return None
    subjects = {
        fact.subject
        for fact in model.facts
        if fact.kind in _BATTERY_KINDS and fact.subject != "site"
    }
    if len(subjects) == 1:
        return next(iter(subjects))
    return None


def _statement_payload(statement: Any) -> dict[str, Any]:
    """Return a JSON-serializable fingerprint of one outlook statement."""
    fact_ids = getattr(statement, "fact_ids", ()) or ()
    return {
        "statement_id": str(getattr(statement, "statement_id", "")),
        "fact_ids": [str(value) for value in fact_ids],
        "relation": str(getattr(statement, "relation", "single")),
        "headline_fact_id": str(getattr(statement, "headline_fact_id", "")),
    }


def _ordered_by_statements(
    selected: tuple[OutlookFact, ...],
    statements: tuple[Any, ...],
) -> list[OutlookFact]:
    """Group selected facts statement-by-statement for coherent rendering.

    Linked facts read as one progression, so each statement's facts stay
    adjacent; facts within a statement keep selection (salience) order.
    Facts missing from every statement keep selection order at the end.
    """
    if not statements:
        return list(selected)
    by_id = {fact.fact_id: fact for fact in selected}
    ordered: list[OutlookFact] = []
    seen: set[str] = set()
    for statement in statements:
        for fact_id in getattr(statement, "fact_ids", ()) or ():
            key = str(fact_id)
            if key in by_id and key not in seen:
                ordered.append(by_id[key])
                seen.add(key)
    for fact in selected:
        if fact.fact_id not in seen:
            ordered.append(fact)
            seen.add(fact.fact_id)
    return ordered


def _salience_scores(
    model: OutlookModel, now: datetime
) -> dict[str, float]:
    """Best-effort per-fact salience for display tie-breaks (never fatal)."""
    try:
        from .plan_outlook_selection import salience_of
    except Exception:
        return {}
    scores: dict[str, float] = {}
    for fact in model.selected_facts:
        try:
            score, _ = salience_of(fact, now, model.selection_history)
        except Exception:
            continue
        scores[fact.fact_id] = score
    return scores


def _info_value_of(fact: OutlookFact) -> str:
    """Derive report-level information value without a magic fact field."""
    info = getattr(fact, "information_value", None)
    if isinstance(info, str) and info in VALUE_RANK:
        return info
    try:
        sig = float(getattr(fact, "significance", 0.5))
    except (TypeError, ValueError):
        return "medium"
    if sig > 1.0:  # legacy int rank 0..3
        return "high" if sig >= 2 else "medium" if sig >= 1 else "low"
    if sig >= 2.0 / 3.0:
        return "high"
    if sig >= 1.0 / 3.0:
        return "medium"
    return "low"
_CATALOG_ROOT = Path(__file__).with_name("locales")


def _time(value: datetime) -> str:
    return value.strftime("%H:%M")


def _stable_number(seed: str) -> int:
    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")


def _value_time(values: dict[str, Any], key: str, fallback: datetime) -> datetime:
    value = values.get(key)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _duration_parts(minutes: int) -> dict[str, int | str]:
    """Keep the established duration rounding while leaving plural wording to FTL."""
    minutes = max(1, minutes)
    if minutes >= 120:
        return {"elapsed_value": minutes // 60, "elapsed_unit": "hour"}
    if minutes >= 60:
        return {"elapsed_value": 1, "elapsed_unit": "hour"}
    return {"elapsed_value": minutes, "elapsed_unit": "minute"}


@lru_cache(maxsize=2)
def _bundle(language: str) -> FluentBundle:
    """Load a single checked-in catalogue; English is the rendering fallback."""
    locale = language if language in {"en", "da"} else "en"
    resource_path = _CATALOG_ROOT / locale / "plan_outlook.ftl"
    source = resource_path.read_text(encoding="utf-8")
    parsed = FluentParser().parse(source)
    junk = [entry for entry in parsed.body if isinstance(entry, Junk)]
    if junk:
        raise ValueError(f"Invalid Plan Outlook Fluent catalogue: {resource_path}")
    bundle = FluentBundle([locale], use_isolating=False)
    bundle.add_resource(parsed)
    return bundle


@lru_cache(maxsize=2)
def _variant_counts(language: str) -> dict[str, int]:
    """Read each catalogue's declared numeric variants once per locale."""
    locale = language if language in {"en", "da"} else "en"
    source = (_CATALOG_ROOT / locale / "plan_outlook.ftl").read_text(encoding="utf-8")
    messages = re.split(r"(?m)^(?=[A-Za-z][\w-]*\s*=)", source)
    counts: dict[str, int] = {}
    for message in messages:
        match = re.match(r"([\w-]+)\s*=", message)
        if not match:
            continue
        variants = re.findall(r"(?m)^\s*\*?\[([\w-]+)\]", message)
        counts[match.group(1)] = len(variants) or 1
    return counts


def preload_plan_outlook_catalogs(languages: tuple[str, ...]) -> None:
    """Load configured Fluent catalogues before rendering on the event loop."""
    for language in {*languages, "en"}:
        _bundle(language)
        _variant_counts(language)


def _fallback(message_id: str, arguments: dict[str, Any]) -> str:
    """Keep catalogue failures visible without recursively formatting a fallback."""
    kind = str(arguments.get("kind") or message_id.removeprefix("outlook-")).replace(
        "-", " "
    )
    return f"Plan update: {kind}."


def _format(language: str, message_id: str, arguments: dict[str, Any]) -> str:
    for locale in (language, "en") if language != "en" else ("en",):
        try:
            bundle = _bundle(locale)
            pattern = bundle.get_message(message_id).value
            if pattern is None:
                continue
            text, errors = bundle.format_pattern(pattern, arguments)
        except Exception:
            continue
        if not errors:
            return text
    return _fallback(message_id, arguments)


def _message_id(fact: OutlookFact, model: OutlookModel | None = None) -> str:
    values = dict(fact.values)
    if fact.kind == "source_problem":
        source = fact.subject.removeprefix("source_").replace("_", "-")
        state = "stale" if values.get("stale") is True else "unavailable"
        return f"outlook-source-problem-{source}-{state}"
    if fact.kind == "grid_price_swing":
        direction = str(values.get("direction", "rise_then_ease")).replace("_", "-")
        return f"outlook-grid-price-swing-{direction}"
    if fact.kind == "flat_grid_prices":
        return "outlook-flat-grid-prices"
    if fact.kind == "optional_start":
        return (
            "outlook-optional-start-alternative"
            if "alternative_at" in values
            else "outlook-optional-start-single"
        )
    if fact.kind == "target_shortfall":
        return (
            "outlook-target-shortfall-known"
            if values.get("expected_percent") is not None
            and values.get("requested_percent") is not None
            else "outlook-target-shortfall-missing"
        )
    if fact.kind == "target_reached":
        base_id = (
            "outlook-target-reached-known"
            if values.get("requested_percent") is not None
            else "outlook-target-reached-missing"
        )
    else:
        base_id = f"outlook-{fact.kind.replace('_', '-')}"
    if base_id in _SOLO_CAPABLE_IDS and model is not None:
        solo = _solo_battery_subject(model)
        if solo is not None and fact.subject == solo:
            return f"{base_id}-solo"
    return base_id


def _variant(
    fact: OutlookFact,
    *,
    model: OutlookModel,
    now: datetime,
    variation_seed: str,
    language: str,
    forced_variant: int | None,
) -> int:
    message_id = _message_id(fact, model)
    try:
        variant_count = _variant_counts(language).get(message_id, 1)
    except Exception:
        variant_count = 1
    if forced_variant is not None:
        return forced_variant % variant_count
    local_hour = (
        now.astimezone(model.start.tzinfo).hour if model.start.tzinfo else now.hour
    )
    period = 0 if local_hour < 12 else 1 if local_hour < 18 else 2
    seed = (
        f"{model.seed_prefix}:{now.date().isoformat()}:{fact.fact_id}:"
        f"{variation_seed}:{language}"
    )
    return (_stable_number(seed) + period) % variant_count


def _period(fact: OutlookFact, now: datetime) -> tuple[str, int]:
    if fact.start < now:
        return "rest-of-today", fact.start.weekday()
    if fact.start.date() == now.date():
        return "today", fact.start.weekday()
    if fact.start.date() == now.date() + timedelta(days=1):
        return "tomorrow", fact.start.weekday()
    return "weekday", fact.start.weekday()


def _day_reference(value: datetime, now: datetime) -> tuple[str, int]:
    """Classify a clock time's day relative to now for day-aware wording.

    A specific time earlier today is still ``today`` (unlike period wording,
    which uses ``rest-of-today`` for a span that started in the past).
    """
    if value.date() == now.date():
        return "today", value.weekday()
    if value.date() == now.date() + timedelta(days=1):
        return "tomorrow", value.weekday()
    return "weekday", value.weekday()


def _time_reference(value: datetime, now: datetime, language: str) -> str:
    """Localized day-aware clock time, e.g. ``09:00 today`` / ``09:00 i dag``."""
    period, weekday = _day_reference(value, now)
    return _format(
        language,
        "outlook-time",
        {"time": _time(value), "period": period, "weekday": weekday},
    )


def _time_range(start: datetime, end: datetime, now: datetime, language: str) -> str:
    """Localized day-aware time range.

    Same-day ranges mention the day once (``from 09:00 to 10:00 today``);
    cross-day ranges label each endpoint (``from 09:00 today to 01:00 tomorrow``).
    """
    if start.date() == end.date():
        period, weekday = _day_reference(start, now)
        return _format(
            language,
            "outlook-time-range-same-day",
            {
                "start": _time(start),
                "end": _time(end),
                "period": period,
                "weekday": weekday,
            },
        )
    return _format(
        language,
        "outlook-time-range",
        {
            "start": _time_reference(start, now, language),
            "end": _time_reference(end, now, language),
        },
    )


def _arguments(fact: OutlookFact, now: datetime, language: str) -> dict[str, Any]:
    values = dict(fact.values)
    alternative = _value_time(values, "alternative_at", fact.start)
    turn_at = _value_time(values, "turn_at", fact.start)
    peak_at = _value_time(values, "peak_at", fact.start)
    arguments: dict[str, Any] = {
        "variant": 0,
        "kind": fact.kind.replace("_", " "),
        "subject": fact.subject,
        "start": _time_reference(fact.start, now, language),
        "turn_at": _time_reference(turn_at, now, language),
        "peak_at": _time_reference(peak_at, now, language),
        "alternative_at": _time_reference(alternative, now, language),
        "range": _time_range(fact.start, fact.end, now, language),
        "expected": values.get("expected_percent"),
        "requested": values.get("requested_percent"),
    }
    arguments.update(_duration_parts(int(values.get("elapsed_minutes", 1) or 1)))
    arguments["period"], arguments["weekday"] = _period(fact, now)
    arguments["duration"] = _format(
        language,
        "outlook-duration",
        {
            "value": arguments["elapsed_value"],
            "unit": arguments["elapsed_unit"],
        },
    )
    arguments["period_text"] = _format(
        language,
        "outlook-period",
        {"period": arguments["period"], "weekday": arguments["weekday"]},
    )
    return arguments


def _render_fact(
    fact: OutlookFact,
    *,
    model: OutlookModel,
    now: datetime,
    variation_seed: str,
    language: str,
    forced_variant: int | None = None,
) -> tuple[str, int]:
    variant = _variant(
        fact,
        model=model,
        now=now,
        variation_seed=variation_seed,
        language=language,
        forced_variant=forced_variant,
    )
    arguments = _arguments(fact, now, language)
    arguments["variant"] = variant
    return _format(language, _message_id(fact, model), arguments), variant


def render_fact_english(fact: OutlookFact, **kwargs: Any) -> tuple[str, int]:
    """Render one fact through the English Fluent catalogue."""
    return _render_fact(fact, language="en", **kwargs)


def render_fact_danish(fact: OutlookFact, **kwargs: Any) -> tuple[str, int]:
    """Render one fact through the Danish Fluent catalogue."""
    return _render_fact(fact, language="da", **kwargs)


def _semantic_fact_payload(fact: OutlookFact) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": fact.fact_id,
        "kind": fact.kind,
        "topic": fact.topic,
        "information_value": _info_value_of(fact),
        "subject": fact.subject,
        "values": dict(fact.values),
        "significance": float(getattr(fact, "significance", 0.5) or 0.0),
        "confidence": float(getattr(fact, "confidence", 0.8) or 0.0),
        "deviation": float(getattr(fact, "deviation", 0.0) or 0.0),
    }
    if fact.kind in {
        "negative_grid_price",
        "cheaper_grid_prices",
        "solar_surplus",
        "grid_charge",
        "battery_preserve",
        "comfort_timing",
        "grid_export",
    }:
        payload.update(start=fact.start.isoformat(), end=fact.end.isoformat())
    elif fact.kind in {
        "solar_fading",
        "battery_full",
        "target_shortfall",
        "target_reached",
        "optional_start",
    }:
        payload["start"] = fact.start.isoformat()
    return payload


def _semantic_id(
    selected: tuple[OutlookFact, ...],
    *,
    information_value: str,
    statements: tuple[Any, ...] = (),
) -> str:
    """Fingerprint shared meaning: fact payloads plus statement grouping.

    Salience/novelty/timeliness drift with wall-clock time, so they stay out
    of this hash: while the selected facts and their grouping are unchanged,
    each language renderer reuses its persisted phrase variants.
    """
    digest = hashlib.sha256(
        json.dumps(
            {
                "facts": [_semantic_fact_payload(fact) for fact in selected],
                "statements": [_statement_payload(item) for item in statements],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:10]
    return f"{selected[0].kind}_{information_value}_{digest}"


def render_plan_outlook(
    model: OutlookModel,
    *,
    now: datetime,
    language: str = "en",
    previous_outlook: dict[str, Any] | None = None,
    variation_seed: str | None = None,
) -> dict[str, Any]:
    """Assemble a rendered report; facts and selection remain language-neutral.

    Each statement's facts render through the existing per-fact Fluent
    messages (no statement-level prose exists in the catalogues); statements
    only control grouping order and the semantic fingerprint. The
    report-level information value is the max significance band across the
    selected facts; per-fact salience already determined selection order
    upstream and only breaks topic ties here.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=model.start.tzinfo or UTC)
    elif model.start.tzinfo is not None:
        now = now.astimezone(model.start.tzinfo)
    selected = model.selected_facts
    if not selected:
        return {}
    language = language if language in {"en", "da"} else "en"
    statements = tuple(getattr(model, "statements", ()) or ())
    information_value = max(
        (_info_value_of(fact) for fact in selected), key=VALUE_RANK.__getitem__
    )
    semantic_id = _semantic_id(
        selected, information_value=information_value, statements=statements
    )
    previous_variants = (
        previous_outlook.get("_render_variants", {})
        if isinstance(previous_outlook, dict)
        and previous_outlook.get("semantic_id") == semantic_id
        and previous_outlook.get("language") == language
        and isinstance(previous_outlook.get("_render_variants"), dict)
        else {}
    )
    rendered: list[str] = []
    variants: dict[str, int] = {}
    for fact in _ordered_by_statements(selected, statements):
        previous_variant = previous_variants.get(fact.fact_id)
        text, variant = _render_fact(
            fact,
            model=model,
            now=now,
            language=language,
            variation_seed=variation_seed or semantic_id,
            forced_variant=(previous_variant if isinstance(previous_variant, int) else None),
        )
        rendered.append(text)
        variants[fact.fact_id] = variant
    line_1, line_2 = rendered[0], " ".join(rendered[1:])
    salience = _salience_scores(model, now)
    topic_fact = next(
        (fact for fact in selected if fact.topic == "reliability"),
        max(
            selected,
            key=lambda fact: (
                VALUE_RANK[_info_value_of(fact)],
                float(getattr(fact, "significance", 0.0) or 0.0),
                salience.get(fact.fact_id, 0.0),
            ),
        ),
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "language": language,
                "facts": [_semantic_fact_payload(fact) for fact in selected],
                "variants": variants,
                "rendered": rendered,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:10]
    return {
        "report_id": f"{selected[0].kind}_{information_value}_{digest}",
        "semantic_id": semantic_id,
        "headline": line_1,
        "line_1": line_1,
        "line_2": line_2,
        "text": " ".join(rendered),
        "language": language,
        "information_value": information_value,
        "topic": topic_fact.topic,
        "selected_facts": [fact.fact_id for fact in selected],
        "statements": [_statement_payload(item) for item in statements],
        "fact_details": [
            {
                "id": fact.fact_id,
                "basis": fact.basis,
                "start": fact.start.isoformat(),
                "end": fact.end.isoformat(),
                "required_inputs": list(fact.required_inputs),
                "values": dict(fact.values),
                "significance": float(getattr(fact, "significance", 0.0) or 0.0),
                "confidence": float(getattr(fact, "confidence", 0.5) or 0.0),
                "deviation": float(getattr(fact, "deviation", 0.0) or 0.0),
                "salience": salience.get(fact.fact_id),
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
