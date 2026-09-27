"""Salience-based selection engine for Plan Outlook facts.

Pure python: no ``homeassistant`` imports. All functions are pure and
timezone-aware (naive datetimes are interpreted as UTC).

Duck-typed facts: works with both the v1 ``OutlookFact`` (int
``significance``, no confidence/deviation) and the forthcoming v2 schema
(``significance``/``confidence``/``deviation`` floats in 0..1 plus
``semantic_key()``). Anything exposing ``fact_id``, ``kind``, ``start``,
``end``, ``related`` plus optional ``topic``/``subject``/``significance``/
``confidence``/``deviation``/``semantic_key`` is accepted.

Public API (wired by the lead into ``plan_outlook.py`` later):
- ``score_candidates(facts, now, history)``
- ``select_best_set(facts, now, history)``
- ``build_statements(selected)``
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from itertools import combinations
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Tunable weights
# ---------------------------------------------------------------------------

W_SIG = 0.45
W_DEV = 0.20
W_NOV = 0.15
W_TIME = 0.15
W_REP = 0.25

CONFIDENCE_THRESHOLD = 0.5
HISTORY_WINDOW = 12
NOVELTY_DECAY = 0.7
TIMELINESS_HOURS = 12.0

DIVERSITY_WEIGHT = 0.10
COVERAGE_WEIGHT = 0.05
COHERENCE_WEIGHT = 0.12
REDUNDANCY_WEIGHT = 0.10

MAX_SET_SIZE = 3

# Fallback compat tables (authoritative copies live in plan_outlook.py and are
# preferred via lazy import so this module never creates an import cycle).
_FALLBACK_FACT_GROUPS = {
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
_FALLBACK_INCOMPATIBLE = {
    frozenset(("solar_surplus", "grid_export")),
}


@runtime_checkable
class _FactLike(Protocol):
    fact_id: str
    kind: str
    start: datetime
    end: datetime


def _compat_tables() -> tuple[dict[str, str], set[frozenset[str]]]:
    """Return (FACT_GROUPS, INCOMPATIBLE_FACT_PAIRS), preferring plan_outlook."""
    try:
        from .plan_outlook import FACT_GROUPS, INCOMPATIBLE_FACT_PAIRS

        return dict(FACT_GROUPS), set(INCOMPATIBLE_FACT_PAIRS)
    except Exception:
        return dict(_FALLBACK_FACT_GROUPS), set(_FALLBACK_INCOMPATIBLE)


def facts_are_compatible(left: Any, right: Any) -> bool:
    """Return whether two facts can share one concise report set."""
    groups, incompatible = _compat_tables()
    left_kind = getattr(left, "kind", "")
    right_kind = getattr(right, "kind", "")
    left_group = groups.get(left_kind)
    right_group = groups.get(right_kind)
    if left_group is not None and left_group == right_group:
        return False
    if frozenset((left_kind, right_kind)) in incompatible:
        return False
    if "negative_grid_price" in {left_kind, right_kind}:
        return {left_kind, right_kind} == {"negative_grid_price", "grid_charge"}
    return True


# ---------------------------------------------------------------------------
# Timezone helpers (pure)
# ---------------------------------------------------------------------------


def _ensure_aware(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None:
        ref_tz = reference.tzinfo or UTC
        return value.replace(tzinfo=ref_tz)
    return value


def _coerce_now(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


# ---------------------------------------------------------------------------
# Field normalisation (v1 int <-> v2 float tolerant)
# ---------------------------------------------------------------------------


def _unit_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number):
        return default
    return min(1.0, max(0.0, number))


def _significance01(fact: Any) -> float:
    raw = getattr(fact, "significance", 0.0)
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, int):
        # Legacy v1 int rank on the 0..3 scale (larger = more significant).
        return min(1.0, max(0.0, raw / 3.0))
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number):
        return 0.0
    return min(1.0, max(0.0, number))


def _confidence01(fact: Any) -> float:
    if hasattr(fact, "confidence"):
        return _unit_float(getattr(fact, "confidence"), 1.0)
    return 1.0


def _deviation01(fact: Any) -> float:
    return _unit_float(getattr(fact, "deviation", 0.0), 0.0)


def semantic_key_of(fact: Any) -> str:
    """Return the semantic dedup key for a fact (v2 ``semantic_key()`` aware)."""
    candidate = getattr(fact, "semantic_key", None)
    if callable(candidate):
        try:
            key = candidate()
            if isinstance(key, str) and key:
                return key
        except Exception:
            pass
    elif isinstance(candidate, str) and candidate:
        return candidate
    kind = str(getattr(fact, "kind", "unknown"))
    subject = getattr(fact, "subject", "site")
    return f"{kind}:{subject}"


def _history_keys(history: Any) -> list[str]:
    if history is None:
        return []
    if isinstance(history, (str, bytes)):
        return [str(history)]
    try:
        return [str(item) for item in history]
    except TypeError:
        return [str(history)]


# ---------------------------------------------------------------------------
# Eligibility + components
# ---------------------------------------------------------------------------


def filter_eligible(
    facts: list[Any],
    now: datetime,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> tuple[list[Any], dict[str, str]]:
    """Split facts into (eligible, {fact_id: reason}) without mutating input."""
    moment = _coerce_now(now)
    eligible: list[Any] = []
    reasons: dict[str, str] = {}
    for fact in facts:
        fact_id = str(getattr(fact, "fact_id", "?"))
        try:
            end = _ensure_aware(fact.end, moment)
        except Exception:
            reasons[fact_id] = "expired:unparseable-end"
            continue
        if end < moment:
            reasons[fact_id] = "expired:end-before-now"
            continue
        if _confidence01(fact) < confidence_threshold:
            reasons[fact_id] = "unreliable:confidence-below-threshold"
            continue
        eligible.append(fact)
    return eligible, reasons


def novelty_score(
    fact: Any,
    history: Any,
    *,
    window: int = HISTORY_WINDOW,
    decay: float = NOVELTY_DECAY,
) -> float:
    """Return 1.0 when unseen, else ``decay ** matches`` in the recent window."""
    keys = _history_keys(history)
    recent = keys[-window:] if window > 0 else []
    key = semantic_key_of(fact)
    matches = sum(1 for item in recent if item == key)
    if matches <= 0:
        # Also match bare-kind entries (v1 history stores kinds only).
        kind = str(getattr(fact, "kind", ""))
        matches = sum(1 for item in recent if item == kind)
    if matches <= 0:
        return 1.0
    return decay**matches


def repetition_score(
    fact: Any,
    history: Any,
    *,
    window: int = HISTORY_WINDOW,
) -> float:
    """Return frequency (0..1) of the fact's semantic key in the last N."""
    keys = _history_keys(history)
    recent = keys[-window:] if window > 0 else []
    if not recent:
        return 0.0
    key = semantic_key_of(fact)
    matches = sum(1 for item in recent if item == key)
    if matches <= 0:
        kind = str(getattr(fact, "kind", ""))
        matches = sum(1 for item in recent if item == kind)
    return matches / len(recent)


def timeliness_score(fact: Any, now: datetime) -> float:
    """Return urgency in 0..1: 1.0 for started/ongoing, exp decay after."""
    moment = _coerce_now(now)
    try:
        start = _ensure_aware(fact.start, moment)
    except Exception:
        return 0.0
    if start <= moment:
        return 1.0
    hours = (start - moment).total_seconds() / 3600.0
    if hours < 0:
        return 1.0
    return math.exp(-hours / TIMELINESS_HOURS)


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    fact: Any
    score: float
    reasons: dict[str, float] = field(default_factory=dict)


def salience_of(
    fact: Any,
    now: datetime,
    history: Any,
    *,
    window: int = HISTORY_WINDOW,
) -> tuple[float, dict[str, float]]:
    """Return (salience, reasons dict) for one fact."""
    significance = _significance01(fact)
    deviation = _deviation01(fact)
    novelty = novelty_score(fact, history, window=window)
    timeliness = timeliness_score(fact, now)
    repetition = repetition_score(fact, history, window=window)
    score = (
        W_SIG * significance
        + W_DEV * deviation
        + W_NOV * novelty
        + W_TIME * timeliness
        - W_REP * repetition
    )
    reasons = {
        "significance": significance,
        "deviation": deviation,
        "novelty": novelty,
        "timeliness": timeliness,
        "repetition": repetition,
    }
    return score, reasons


def score_candidates(
    facts: list[Any],
    now: datetime,
    history: Any,
    *,
    window: int = HISTORY_WINDOW,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> list[ScoredCandidate]:
    """Score eligible facts, sorted by (-score, fact_id) deterministically."""
    eligible, _ = filter_eligible(
        facts, now, confidence_threshold=confidence_threshold
    )
    scored = [
        ScoredCandidate(
            fact=fact,
            score=score,
            reasons=reasons,
        )
        for fact, (score, reasons) in (
            (fact, salience_of(fact, now, history, window=window))
            for fact in eligible
        )
    ]
    scored.sort(key=lambda item: (-item.score, str(item.fact.fact_id)))
    return scored


# ---------------------------------------------------------------------------
# Set enumeration
# ---------------------------------------------------------------------------


def _set_is_valid(members: tuple[Any, ...]) -> bool:
    if len(members) <= 1:
        return True
    kinds = [str(getattr(fact, "kind", "")) for fact in members]
    if len(set(kinds)) != len(kinds):
        return False
    for left, right in combinations(members, 2):
        if not facts_are_compatible(left, right):
            return False
    return True


def _related_set(fact: Any) -> set[str]:
    related = getattr(fact, "related", ())
    try:
        return {str(item) for item in related}
    except TypeError:
        return set()


_PRICE_KINDS = {
    "flat_grid_prices",
    "negative_grid_price",
    "cheaper_grid_prices",
    "grid_price_rise",
    "grid_price_fall",
    "grid_price_swing",
}
_ACTION_KINDS = {
    "grid_charge",
    "battery_preserve",
    "battery_self_consume",
    "optional_start",
    "comfort_timing",
}
_CAUSE_PAIRS = {
    frozenset(("low_reserve", "grid_charge")),
    frozenset(("target_shortfall", "grid_charge")),
    frozenset(("solar_surplus", "grid_export")),
    frozenset(("solar_surplus", "battery_full")),
    frozenset(("solar_fading", "grid_use_increase")),
}


def _link_weight(left_kind: str, right_kind: str) -> float:
    """Weight one related-graph link: causal/opportunity links count extra."""
    kinds = {left_kind, right_kind}
    if kinds & _PRICE_KINDS and kinds & _ACTION_KINDS:
        return 2.0
    if frozenset((left_kind, right_kind)) in _CAUSE_PAIRS:
        return 2.0
    return 1.0


def _elapsed_minutes(fact: Any) -> float:
    values = getattr(fact, "values", ())
    try:
        mapping = dict(values)
    except (TypeError, ValueError):
        return 0.0
    elapsed = mapping.get("elapsed_minutes", 0)
    return float(elapsed) if isinstance(elapsed, int | float) else 0.0


def set_score(
    members: tuple[Any, ...],
    scores: dict[str, float],
) -> tuple[float, dict[str, float]]:
    """Return (total, breakdown) for one candidate set.

    Base is the MEAN member salience (quality over quantity: do not pad
    reports to three facts), with small additive bonuses for topic/group
    coverage and related-graph coherence.
    """
    saliences = [scores.get(str(getattr(fact, "fact_id", "")), 0.0) for fact in members]
    base = sum(saliences) / len(saliences) if saliences else 0.0
    topics = {str(getattr(fact, "topic", "?")) for fact in members}
    groups, _ = _compat_tables()
    group_ids = {
        groups.get(str(getattr(fact, "kind", "")), str(getattr(fact, "kind", "")))
        for fact in members
    }
    diversity = DIVERSITY_WEIGHT * len(topics)
    coverage = COVERAGE_WEIGHT * len(group_ids)
    coherence_links = 0.0
    redundancy_hits = 0
    for left, right in combinations(members, 2):
        left_kind = str(getattr(left, "kind", ""))
        right_kind = str(getattr(right, "kind", ""))
        left_rel = _related_set(left)
        right_rel = _related_set(right)
        if left_kind in right_rel or right_kind in left_rel:
            coherence_links += _link_weight(left_kind, right_kind)
        redundancy_hits += len(left_rel & right_rel)
    coherence = COHERENCE_WEIGHT * coherence_links
    redundancy = REDUNDANCY_WEIGHT * redundancy_hits
    total = base + diversity + coverage + coherence - redundancy
    return total, {
        "base": base,
        "diversity": diversity,
        "coverage": coverage,
        "coherence": coherence,
        "redundancy": redundancy,
    }


def _reliability_pick(scored: list[ScoredCandidate]) -> list[Any]:
    best = max(
        scored,
        key=lambda item: (
            item.score,
            _significance01(item.fact),
            _confidence01(item.fact),
            _elapsed_minutes(item.fact),
        ),
    )
    # Deterministic: score desc already sorted, so first max wins; resolve full
    # ties by fact_id.
    top_key = (
        best.score,
        _significance01(best.fact),
        _confidence01(best.fact),
        _elapsed_minutes(best.fact),
    )
    tied = [
        item
        for item in scored
        if (
            item.score,
            _significance01(item.fact),
            _confidence01(item.fact),
            _elapsed_minutes(item.fact),
        )
        == top_key
    ]
    tied.sort(key=lambda item: str(item.fact.fact_id))
    return [tied[0].fact]


def select_best_set(
    facts: list[Any],
    now: datetime,
    history: Any,
    *,
    window: int = HISTORY_WINDOW,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    max_size: int = MAX_SET_SIZE,
) -> list[Any]:
    """Select the best valid 1-3 fact set (reliability short-circuit kept)."""
    scored = score_candidates(
        facts, now, history, window=window, confidence_threshold=confidence_threshold
    )
    if not scored:
        return []
    reliability = [
        item for item in scored if str(getattr(item.fact, "topic", "")) == "reliability"
    ]
    if reliability:
        return _reliability_pick(reliability)

    by_id = {str(item.fact.fact_id): item.score for item in scored}
    ordered = sorted(
        (item.fact for item in scored), key=lambda fact: str(fact.fact_id)
    )
    best: list[Any] = []
    best_key: tuple[float, tuple[str, ...]] | None = None
    for size in range(1, min(max_size, len(ordered)) + 1):
        for combo in combinations(ordered, size):
            if not _set_is_valid(combo):
                continue
            total, _ = set_score(combo, by_id)
            key_ids = tuple(sorted(str(f.fact_id) for f in combo))
            # Higher total wins; ties break by lexicographic fact_id tuple.
            key = (-total, key_ids)
            if best_key is None or key < best_key:
                best_key = key
                best = list(combo)
    if not best:
        # No valid multi-set (should be rare): fall back to top single.
        return [scored[0].fact]
    # Stable output order: salience desc, then fact_id — except that
    # optional advice always closes: it is acted on, never the headline.
    best.sort(
        key=lambda fact: (
            str(getattr(fact, "kind", "")) == "optional_start",
            -by_id[str(fact.fact_id)],
            str(fact.fact_id),
        )
    )
    return best


# ---------------------------------------------------------------------------
# Statement aggregation over the related graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FallbackOutlookStatement:
    statement_id: str
    fact_ids: tuple[str, ...]
    relation: str


def _infer_relation(members: list[Any]) -> str:
    if len(members) < 2:
        return "single"
    kinds = {str(getattr(fact, "kind", "")) for fact in members}
    topics = {str(getattr(fact, "topic", "")) for fact in members}

    price_kinds = {
        "flat_grid_prices",
        "negative_grid_price",
        "cheaper_grid_prices",
        "grid_price_rise",
        "grid_price_fall",
        "grid_price_swing",
    }
    action_kinds = {
        "grid_charge",
        "battery_preserve",
        "battery_self_consume",
        "optional_start",
        "comfort_timing",
    }
    if kinds & price_kinds and kinds & action_kinds:
        return "opportunity-action"
    if "solar_surplus" in kinds and (
        kinds & {"grid_export", "battery_full", "optional_start", "comfort_timing"}
    ):
        return "cause"
    if "solar_fading" in kinds and kinds & {"grid_use_increase", "battery_preserve"}:
        return "sequence"
    if kinds & {"grid_price_rise", "grid_use_increase"} and kinds & {
        "grid_price_fall",
        "grid_use_decrease",
    }:
        return "contrast"
    if "low_reserve" in kinds and "grid_charge" in kinds:
        return "cause"
    if "target_shortfall" in kinds and "grid_charge" in kinds:
        return "cause"
    if len(topics) == 1:
        return "sequence"
    return "cause"


def _build_statement_object(
    statement_id: str,
    fact_ids: tuple[str, ...],
    relation: str,
    headline_fact_id: str,
) -> Any:
    try:
        from .plan_outlook_types import OutlookStatement  # type: ignore
    except Exception:
        return FallbackOutlookStatement(
            statement_id=statement_id, fact_ids=fact_ids, relation=relation
        )
    try:
        available = {f.name for f in fields(OutlookStatement)}
    except Exception:
        return FallbackOutlookStatement(
            statement_id=statement_id, fact_ids=fact_ids, relation=relation
        )
    kwargs: dict[str, Any] = {}
    if "statement_id" in available:
        kwargs["statement_id"] = statement_id
    if "fact_ids" in available:
        kwargs["fact_ids"] = fact_ids
    if "relation" in available:
        kwargs["relation"] = relation
    if "headline_fact_id" in available:
        kwargs["headline_fact_id"] = headline_fact_id
    # Tolerate slightly different v2 shapes: fill facts only if required.
    try:
        return OutlookStatement(**kwargs)  # type: ignore[call-arg]
    except Exception:
        return FallbackOutlookStatement(
            statement_id=statement_id, fact_ids=fact_ids, relation=relation
        )


def build_statements(selected: list[Any]) -> list[Any]:
    """Aggregate selected facts into statements via related-graph components.

    Each fact appears in exactly one statement. Linked facts (``kind`` present
    in another member's ``related``) form one multi-fact statement whose
    relation is inferred (cause/sequence/contrast/opportunity-action);
    unlinked facts become ``single`` statements. Deterministic output ordered
    by salience-selected fact order, except that optional-only statements
    always close: advice is acted on, never the headline.
    """
    if not selected:
        return []
    order = {str(getattr(fact, "fact_id", "")): index for index, fact in enumerate(selected)}
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        root = key
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(key, key) != key:
            parent[key], key = root, parent[key]
        return root

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    for fact in selected:
        parent.setdefault(str(getattr(fact, "fact_id", "")), str(getattr(fact, "fact_id", "")))
    by_id = {str(getattr(fact, "fact_id", "")): fact for fact in selected}
    for left, right in combinations(selected, 2):
        left_id = str(getattr(left, "fact_id", ""))
        right_id = str(getattr(right, "fact_id", ""))
        left_kind = str(getattr(left, "kind", ""))
        right_kind = str(getattr(right, "kind", ""))
        if left_kind in _related_set(right) or right_kind in _related_set(left):
            union(left_id, right_id)

    components: dict[str, list[Any]] = {}
    for fact_id, fact in by_id.items():
        components.setdefault(find(fact_id), []).append(fact)
    for members in components.values():
        members.sort(key=lambda fact: order.get(str(fact.fact_id), 0))

    statements: list[Any] = []

    def _component_key(root: str) -> tuple[bool, int]:
        members = components[root]
        optional_only = bool(members) and all(
            str(getattr(fact, "kind", "")) == "optional_start"
            for fact in members
        )
        return (
            optional_only,
            min(order[str(fact.fact_id)] for fact in members),
        )

    for root in sorted(components, key=_component_key):
        members = components[root]
        fact_ids = tuple(str(fact.fact_id) for fact in members)
        relation = _infer_relation(members)
        statement_id = "stmt:" + "+".join(sorted(fact_ids))
        statements.append(
            _build_statement_object(statement_id, fact_ids, relation, fact_ids[0])
        )
    return statements
