"""Language-neutral models for Plan Outlook selection and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

type OutlookValue = str | int | float | bool | None

SCHEMA_VERSION = 2

_INFO_VALUE_TO_SIGNIFICANCE: dict[str, float] = {
    "high": 0.9,
    "medium": 0.5,
    "low": 0.2,
}


def _clamp01(value: float) -> float:
    """Clamp a value into the 0..1 range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return min(1.0, max(0.0, number))


def _significance_from_payload(payload: dict[str, Any]) -> float:
    """Resolve v2 significance float from v2 or v1 payloads."""
    raw = payload.get("significance", None)
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, float):
        return _clamp01(raw)
    if isinstance(raw, int):
        return _clamp01(max(0, min(3, raw)) / 3.0)
    info = payload.get("information_value", None)
    if isinstance(info, str):
        mapped = _INFO_VALUE_TO_SIGNIFICANCE.get(info.strip().lower())
        if mapped is not None:
            return mapped
    return 0.5


def _float_from_payload(
    payload: dict[str, Any], key: str, default: float
) -> float:
    """Read an optional 0..1 float field with a neutral default."""
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int | float):
        return _clamp01(raw)
    return default


@dataclass(frozen=True, slots=True)
class OutlookFact:
    """One supported, selectable outlook statement without rendered wording."""

    fact_id: str
    kind: str
    topic: str
    basis: str
    start: datetime
    end: datetime
    subject: str = "site"
    related: tuple[str, ...] = ()
    required_inputs: tuple[str, ...] = ()
    values: tuple[tuple[str, OutlookValue], ...] = ()
    significance: float = 0.0
    confidence: float = 0.5
    deviation: float = 0.0
    # v1 compat loading only; never used in selection.
    information_value: str | None = None

    def semantic_key(self) -> str:
        """Return the ``kind.magnitude.period`` history key for this fact."""
        return semantic_key(
            self.kind,
            self.significance,
            self.deviation,
            self.start,
            self.start.tzinfo,
        )


@dataclass(frozen=True, slots=True)
class OutlookStatement:
    """One grouped outlook statement linking related facts."""

    statement_id: str
    fact_ids: tuple[str, ...] = ()
    relation: str = "single"
    headline_fact_id: str = ""


@dataclass(frozen=True, slots=True)
class OutlookModel:
    """Selected language-neutral outlook content ready for rendering."""

    facts: tuple[OutlookFact, ...]
    selected_fact_ids: tuple[str, ...]
    selection_history: tuple[str, ...]
    start: datetime
    horizon_end: datetime
    plan_created_at: datetime | None
    seed_prefix: str
    statements: tuple[OutlookStatement, ...] = ()

    @property
    def selected_facts(self) -> tuple[OutlookFact, ...]:
        """Return selected facts in their recorded order."""
        by_id = {fact.fact_id: fact for fact in self.facts}
        return tuple(
            by_id[fact_id] for fact_id in self.selected_fact_ids if fact_id in by_id
        )


def _period_bucket(hour: int) -> str:
    """Map a local hour to a coarse day-period bucket."""
    if 0 <= hour <= 4:
        return "overnight"
    if 5 <= hour <= 10:
        return "morning"
    if 11 <= hour <= 13:
        return "midday"
    if 14 <= hour <= 17:
        return "afternoon"
    return "evening"


def _magnitude_bucket(significance: float) -> str:
    """Map a 0..1 significance float to a high/med/low tertile label."""
    value = _clamp01(significance)
    if value >= 2.0 / 3.0:
        return "high"
    if value >= 1.0 / 3.0:
        return "med"
    return "low"


def semantic_key(
    kind: str,
    significance: float,
    deviation: float,
    start: datetime,
    display_tz: Any | None = None,
) -> str:
    """Return a ``kind.magnitude.period`` semantic key for history tracking.

    ``deviation`` is accepted for forward compatibility but does not alter
    the key. ``display_tz`` may be a tzinfo or an IANA timezone name; when
    given, ``start`` is converted before bucketing its hour.
    """
    _ = deviation
    moment = start
    if display_tz is not None:
        try:
            tzinfo = (
                ZoneInfo(str(display_tz)) if isinstance(display_tz, str) else display_tz
            )
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=tzinfo)
            else:
                moment = moment.astimezone(tzinfo)
        except Exception:
            moment = start
    try:
        hour = int(moment.hour)
    except (AttributeError, TypeError, ValueError):
        hour = 0
    hour = max(0, min(23, hour))
    return f"{kind}.{_magnitude_bucket(significance)}.{_period_bucket(hour)}"


def fact_to_dict(fact: OutlookFact) -> dict[str, Any]:
    """Return a bounded JSON-serializable v2 fact payload."""
    return {
        "fact_id": fact.fact_id,
        "kind": fact.kind,
        "topic": fact.topic,
        "basis": fact.basis,
        "start": fact.start.isoformat(),
        "end": fact.end.isoformat(),
        "subject": fact.subject,
        "related": list(fact.related),
        "required_inputs": list(fact.required_inputs),
        "values": [list(item) for item in fact.values],
        "significance": _clamp01(fact.significance),
        "confidence": _clamp01(fact.confidence),
        "deviation": _clamp01(fact.deviation),
    }


def fact_from_dict(payload: dict[str, Any]) -> OutlookFact | None:
    """Restore one fact from a persisted v1 or v2 outlook model."""
    try:
        raw_info = payload.get("information_value")
        information_value = str(raw_info) if raw_info is not None else None
        return OutlookFact(
            fact_id=str(payload["fact_id"]),
            kind=str(payload["kind"]),
            topic=str(payload["topic"]),
            basis=str(payload["basis"]),
            start=datetime.fromisoformat(str(payload["start"])),
            end=datetime.fromisoformat(str(payload["end"])),
            subject=str(payload.get("subject", "site")),
            related=tuple(str(value) for value in payload.get("related", [])),
            required_inputs=tuple(
                str(value) for value in payload.get("required_inputs", [])
            ),
            values=tuple(
                (str(item[0]), item[1])
                for item in payload.get("values", [])
                if isinstance(item, list | tuple) and len(item) == 2
            ),
            significance=_significance_from_payload(payload),
            confidence=_float_from_payload(payload, "confidence", 0.5),
            deviation=_float_from_payload(payload, "deviation", 0.0),
            information_value=information_value,
        )
    except KeyError, TypeError, ValueError:
        return None


def statement_to_dict(statement: OutlookStatement) -> dict[str, Any]:
    """Return a JSON-serializable v2 statement payload."""
    return {
        "statement_id": statement.statement_id,
        "fact_ids": list(statement.fact_ids),
        "relation": statement.relation,
        "headline_fact_id": statement.headline_fact_id,
    }


def statement_from_dict(payload: dict[str, Any]) -> OutlookStatement | None:
    """Restore one statement from a persisted v2 outlook model."""
    try:
        return OutlookStatement(
            statement_id=str(payload["statement_id"]),
            fact_ids=tuple(str(value) for value in payload.get("fact_ids", [])),
            relation=str(payload.get("relation", "single")),
            headline_fact_id=str(payload.get("headline_fact_id", "")),
        )
    except KeyError, TypeError, ValueError:
        return None


def model_to_dict(model: OutlookModel) -> dict[str, Any]:
    """Return the persisted v2 representation of an outlook model."""
    return {
        "schema_version": SCHEMA_VERSION,
        "facts": [fact_to_dict(fact) for fact in model.facts],
        "selected_fact_ids": list(model.selected_fact_ids),
        "selection_history": list(model.selection_history),
        "start": model.start.isoformat(),
        "horizon_end": model.horizon_end.isoformat(),
        "plan_created_at": (
            model.plan_created_at.isoformat()
            if model.plan_created_at is not None
            else None
        ),
        "seed_prefix": model.seed_prefix,
        "statements": [statement_to_dict(item) for item in model.statements],
    }


def model_from_dict(payload: dict[str, Any]) -> OutlookModel | None:
    """Restore an outlook model from a persisted v1 or v2 payload."""
    try:
        facts = tuple(
            fact
            for item in payload.get("facts", [])
            if isinstance(item, dict)
            if (fact := fact_from_dict(item)) is not None
        )
        statements = tuple(
            statement
            for item in payload.get("statements", [])
            if isinstance(item, dict)
            if (statement := statement_from_dict(item)) is not None
        )
        plan_created_at_raw = payload.get("plan_created_at")
        plan_created_at = (
            datetime.fromisoformat(str(plan_created_at_raw))
            if plan_created_at_raw is not None
            else None
        )
        return OutlookModel(
            facts=facts,
            selected_fact_ids=tuple(
                str(value) for value in payload.get("selected_fact_ids", [])
            ),
            selection_history=tuple(
                str(value) for value in payload.get("selection_history", [])
            ),
            start=datetime.fromisoformat(str(payload["start"])),
            horizon_end=datetime.fromisoformat(str(payload["horizon_end"])),
            plan_created_at=plan_created_at,
            seed_prefix=str(payload.get("seed_prefix", "")),
            statements=statements,
        )
    except KeyError, TypeError, ValueError:
        return None
