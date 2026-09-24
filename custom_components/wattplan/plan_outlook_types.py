"""Language-neutral models for Plan Outlook selection and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

type OutlookValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class OutlookFact:
    """One supported, selectable outlook statement without rendered wording."""

    fact_id: str
    kind: str
    topic: str
    information_value: str
    basis: str
    start: datetime
    end: datetime
    subject: str = "site"
    related: tuple[str, ...] = ()
    required_inputs: tuple[str, ...] = ()
    values: tuple[tuple[str, OutlookValue], ...] = ()
    significance: int = 0


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

    @property
    def selected_facts(self) -> tuple[OutlookFact, ...]:
        """Return selected facts in their recorded order."""
        by_id = {fact.fact_id: fact for fact in self.facts}
        return tuple(
            by_id[fact_id] for fact_id in self.selected_fact_ids if fact_id in by_id
        )


def fact_to_dict(fact: OutlookFact) -> dict[str, Any]:
    """Return a bounded JSON-serializable fact payload."""
    return {
        "fact_id": fact.fact_id,
        "kind": fact.kind,
        "topic": fact.topic,
        "information_value": fact.information_value,
        "basis": fact.basis,
        "start": fact.start.isoformat(),
        "end": fact.end.isoformat(),
        "subject": fact.subject,
        "related": list(fact.related),
        "required_inputs": list(fact.required_inputs),
        "values": [list(item) for item in fact.values],
        "significance": fact.significance,
    }


def fact_from_dict(payload: dict[str, Any]) -> OutlookFact | None:
    """Restore one fact from a persisted outlook model."""
    try:
        return OutlookFact(
            fact_id=str(payload["fact_id"]),
            kind=str(payload["kind"]),
            topic=str(payload["topic"]),
            information_value=str(payload["information_value"]),
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
            significance=int(payload.get("significance", 0)),
        )
    except KeyError, TypeError, ValueError:
        return None


def model_to_dict(model: OutlookModel) -> dict[str, Any]:
    """Return the persisted representation of an outlook model."""
    return {
        "schema_version": 1,
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
    }


def model_from_dict(payload: dict[str, Any]) -> OutlookModel | None:
    """Restore an outlook model from a persisted payload."""
    try:
        facts = tuple(
            fact
            for item in payload.get("facts", [])
            if isinstance(item, dict)
            if (fact := fact_from_dict(item)) is not None
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
        )
    except KeyError, TypeError, ValueError:
        return None
