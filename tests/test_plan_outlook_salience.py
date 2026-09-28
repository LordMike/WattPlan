"""Salience-engine semantic tests: novelty, timeliness, set-optimality, v1 load."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.wattplan.plan_outlook import build_plan_outlook
from custom_components.wattplan.plan_outlook_renderer import render_plan_outlook
from custom_components.wattplan.plan_outlook_selection import (
    COHERENCE_WEIGHT,
    TIMELINESS_HOURS,
    build_statements,
    score_candidates,
    select_best_set,
    set_score,
)
from custom_components.wattplan.plan_outlook_types import (
    OutlookFact,
    OutlookModel,
    fact_from_dict,
    model_from_dict,
    semantic_key,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _fact(
    kind: str,
    *,
    subject: str = "site",
    topic: str = "opportunity",
    significance: float = 0.5,
    confidence: float = 0.8,
    deviation: float = 0.4,
    start: datetime | None = None,
    related: tuple[str, ...] = (),
) -> OutlookFact:
    moment = start or NOW
    return OutlookFact(
        fact_id=f"{kind}:{subject}",
        kind=kind,
        topic=topic,
        basis="forecast",
        start=moment,
        end=moment + timedelta(hours=1),
        subject=subject,
        related=related,
        significance=significance,
        confidence=confidence,
        deviation=deviation,
    )


def _history_for(fact: OutlookFact, repeats: int = 1) -> list[str]:
    return [
        semantic_key(
            fact.kind, fact.significance, fact.deviation, fact.start, "UTC"
        )
    ] * repeats


def test_noon_repeat_is_suppressed_in_favour_of_fresh_news() -> None:
    """A story already told three times is demoted below equally fresh news.

    Bare-kind history entries (persisted v1 selection_history) still drive
    the novelty decay; see the v2-key matching note in the final report.
    """
    repeated = _fact(
        "grid_price_rise", significance=0.9, start=NOW.replace(hour=12)
    )
    assert _history_for(repeated)[0] == "grid_price_rise.high.midday"
    fresh = _fact(
        "optional_start",
        subject="dishwasher",
        significance=0.9,
        start=NOW.replace(hour=12),
    )
    plain = select_best_set([repeated, fresh], NOW, [])
    assert [fact.kind for fact in plain] == ["grid_price_rise", "optional_start"]

    stale = select_best_set([repeated, fresh], NOW, ["grid_price_rise"] * 3)
    assert [fact.kind for fact in stale] == ["optional_start"]


def test_extreme_event_resurfaces_despite_repetition() -> None:
    """Material consequences are never starved by the repetition penalty."""
    extreme = _fact(
        "negative_grid_price",
        significance=0.9,
        deviation=0.9,
        related=("grid_charge",),
    )
    routine = _fact(
        "flat_grid_prices", significance=0.2, deviation=0.1, topic="routine"
    )
    selected = select_best_set(
        [extreme, routine], NOW, _history_for(extreme, repeats=2)
    )

    assert selected and selected[0].kind == "negative_grid_price"


def test_timeliness_prefers_the_imminent_action() -> None:
    """Two otherwise identical charge windows: the sooner one wins."""
    soon = _fact(
        "grid_charge", subject="BattA", topic="battery", start=NOW + timedelta(hours=1)
    )
    later = _fact(
        "grid_charge", subject="BattB", topic="battery", start=NOW + timedelta(hours=6)
    )
    selected = select_best_set([later, soon], NOW, [])

    assert [fact.fact_id for fact in selected] == ["grid_charge:BattA"]


def test_coherent_pair_beats_padded_triple() -> None:
    """Mean-based set scoring refuses to pad a strong pair with filler."""
    negative = _fact(
        "grid_price_rise",
        significance=0.9,
        deviation=0.6,
        related=("grid_charge",),
    )
    charge = _fact(
        "grid_charge",
        subject="battery",
        topic="battery",
        significance=0.5,
        deviation=0.3,
        related=("grid_price_rise",),
    )
    filler = _fact(
        "optional_start", subject="dishwasher", significance=0.2, deviation=0.2
    )
    pool = [negative, charge, filler]

    selected = select_best_set(pool, NOW, [])

    assert [fact.kind for fact in selected] == [
        "grid_price_rise",
        "grid_charge",
    ]

    scores = {item.fact.fact_id: item.score for item in score_candidates(pool, NOW, [])}
    pair_total, _ = set_score((negative, charge), scores)
    triple_total, _ = set_score((negative, charge, filler), scores)
    assert pair_total > triple_total


def test_v1_persisted_payload_loads_with_mapped_significance() -> None:
    """A schema-v1 snapshot restores facts and renders without new fields."""
    legacy = fact_from_dict(
        {
            "fact_id": "grid_price_rise:site",
            "kind": "grid_price_rise",
            "topic": "opportunity",
            "information_value": "high",
            "basis": "forecast",
            "start": NOW.isoformat(),
            "end": (NOW + timedelta(hours=2)).isoformat(),
            "subject": "site",
            "related": ["grid_charge"],
            "required_inputs": ["import_price"],
            "values": [],
            "significance": 2,
        }
    )
    assert legacy is not None
    assert legacy.significance == 2 / 3
    assert legacy.confidence == 0.5
    assert legacy.deviation == 0.0

    inferred = fact_from_dict(
        {
            "fact_id": "flat_grid_prices:site",
            "kind": "flat_grid_prices",
            "topic": "routine",
            "information_value": "medium",
            "basis": "forecast",
            "start": NOW.isoformat(),
            "end": (NOW + timedelta(hours=2)).isoformat(),
        }
    )
    assert inferred is not None
    assert inferred.significance == 0.5

    model = model_from_dict(
        {
            "schema_version": 1,
            "facts": [
                {
                    "fact_id": "grid_price_rise:site",
                    "kind": "grid_price_rise",
                    "topic": "opportunity",
                    "information_value": "high",
                    "basis": "forecast",
                    "start": NOW.isoformat(),
                    "end": (NOW + timedelta(hours=2)).isoformat(),
                    "subject": "site",
                    "related": [],
                    "required_inputs": ["import_price"],
                    "values": [],
                    "significance": 2,
                }
            ],
            "selected_fact_ids": ["grid_price_rise:site"],
            "selection_history": ["grid_price_rise"],
            "start": NOW.isoformat(),
            "horizon_end": (NOW + timedelta(hours=2)).isoformat(),
            "plan_created_at": NOW.isoformat(),
            "seed_prefix": "v1-site",
        }
    )
    assert model is not None
    assert model.statements == ()

    rendered = render_plan_outlook(model, now=NOW, language="en")
    assert rendered["information_value"] == "high"
    assert rendered["semantic_id"].startswith("grid_price_rise_high_")


def test_selection_history_uses_semantic_keys() -> None:
    """History records kind.magnitude.period keys; medium renders as 'med'."""
    assert (
        semantic_key("grid_price_rise", 0.9, 0.6, NOW.replace(hour=12), "UTC")
        == "grid_price_rise.high.midday"
    )
    # Magnitude tertiles are high/med/low ("med", not "medium") by design.
    assert (
        semantic_key("optional_start", 0.5, 0.4, NOW.replace(hour=9), "UTC")
        == "optional_start.med.morning"
    )

    from types import SimpleNamespace

    request = {
        "entry_id": "site-a",
        "slot_minutes": 15,
        "window": SimpleNamespace(start_at=datetime(2026, 9, 24, 0, tzinfo=UTC)),
        "local_timezone": "UTC",
        "source_provenance": {
            "import_price": {"configured": True},
            "usage": {"configured": False},
            "pv": {"configured": False},
        },
        "optimizer_params": {
            "grid_import_price_per_kwh": [0.1, 0.1, 0.8, 0.8],
            "usage_kwh": [0.4] * 4,
            "solar_input_kwh": [0.0] * 4,
            "battery_entities": [],
        },
    }
    outlook = build_plan_outlook(
        request=request,
        result={"entities": [], "optional_entity_options": [], "energy_flows": {}},
    )

    assert outlook["_model"]["selection_history"] == [
        "grid_price_rise.high.overnight"
    ]


def test_v2_semantic_keys_drive_novelty_end_to_end() -> None:
    """Facts expose semantic_key() so v2 history actually suppresses repeats."""
    from custom_components.wattplan.plan_outlook_selection import (
        novelty_score,
        repetition_score,
    )

    repeated = _fact(
        "grid_price_rise", significance=0.9, start=NOW.replace(hour=12)
    )
    assert repeated.semantic_key() == "grid_price_rise.high.midday"
    fresh = _fact(
        "optional_start",
        subject="dishwasher",
        significance=0.9,
        start=NOW.replace(hour=12),
    )

    history = [repeated.semantic_key()] * 3
    assert novelty_score(repeated, history) < 1.0
    assert repetition_score(repeated, history) == 1.0
    assert novelty_score(fresh, history) == 1.0

    stale = select_best_set([repeated, fresh], NOW, history)
    assert [fact.kind for fact in stale] == ["optional_start"]


def test_renderer_groups_statements_and_tracks_grouping_in_semantic_id() -> None:
    """Statement grouping orders prose and fingerprints meaning, not wording."""
    first = _fact(
        "grid_price_rise",
        significance=0.9,
        deviation=0.6,
        related=("grid_charge",),
    )
    second = _fact(
        "grid_charge",
        subject="battery",
        topic="battery",
        significance=0.5,
        deviation=0.3,
        related=("grid_price_rise",),
    )
    base = OutlookModel(
        facts=(first, second),
        selected_fact_ids=(first.fact_id, second.fact_id),
        selection_history=(),
        start=NOW,
        horizon_end=NOW + timedelta(hours=2),
        plan_created_at=NOW,
        seed_prefix="stmt-test",
    )
    plain = render_plan_outlook(base, now=NOW, language="en")

    from custom_components.wattplan.plan_outlook_types import OutlookStatement

    grouped = OutlookModel(
        facts=base.facts,
        selected_fact_ids=base.selected_fact_ids,
        selection_history=(),
        start=base.start,
        horizon_end=base.horizon_end,
        plan_created_at=base.plan_created_at,
        seed_prefix=base.seed_prefix,
        statements=(
            OutlookStatement(
                statement_id="stmt:linked",
                fact_ids=(first.fact_id, second.fact_id),
                relation="opportunity-action",
                headline_fact_id=first.fact_id,
            ),
        ),
    )
    rendered = render_plan_outlook(grouped, now=NOW, language="en")

    assert rendered["statements"] == [
        {
            "statement_id": "stmt:linked",
            "fact_ids": [first.fact_id, second.fact_id],
            "relation": "opportunity-action",
            "headline_fact_id": first.fact_id,
        }
    ]
    assert rendered["semantic_id"] != plain["semantic_id"]
    assert rendered["semantic_id"].startswith("grid_price_rise_high_")
    assert rendered["information_value"] == "high"
    details = {item["id"]: item for item in rendered["fact_details"]}
    assert details[first.fact_id]["significance"] == 0.9
    assert details[first.fact_id]["confidence"] == 0.8
    assert details[first.fact_id]["deviation"] == 0.6
    assert details[first.fact_id]["salience"] is not None


def test_optional_advice_closes_never_opens() -> None:
    """Optional recommendations sort last even at top salience."""
    optional = _fact(
        "optional_start",
        subject="dishwasher",
        significance=0.9,
        deviation=0.8,
        start=NOW + timedelta(hours=1),
        related=(),
    )
    main = _fact("solar_surplus", significance=0.9, deviation=0.8, related=())

    selected = select_best_set([optional, main], NOW, [])

    assert [fact.kind for fact in selected][-1] == "optional_start"

    model = OutlookModel(
        facts=(main, optional),
        selected_fact_ids=tuple(fact.fact_id for fact in selected),
        selection_history=(),
        start=NOW,
        horizon_end=NOW + timedelta(hours=2),
        plan_created_at=NOW,
        seed_prefix="optional-last",
    )
    rendered = render_plan_outlook(model, now=NOW, language="en")

    assert "dishwasher" not in rendered["line_1"]
    assert "dishwasher" in rendered["text"]


def test_statements_preserve_selected_order_except_optional_advice() -> None:
    """Unrelated statements retain salience order while advice still closes."""
    main = _fact("solar_surplus", subject="z-main", significance=0.9)
    secondary = _fact("grid_price_rise", subject="a-secondary", significance=0.8)
    optional = _fact("optional_start", subject="dishwasher", significance=0.9)

    statements = build_statements([main, optional, secondary])

    assert [statement.fact_ids for statement in statements] == [
        (main.fact_id,),
        (secondary.fact_id,),
        (optional.fact_id,),
    ]
    assert [statement.headline_fact_id for statement in statements] == [
        main.fact_id,
        secondary.fact_id,
        optional.fact_id,
    ]


def test_rendered_headline_and_report_id_use_first_statement_headline() -> None:
    """The report identifier describes the fact rendered in the headline."""
    main = _fact("solar_surplus", subject="z-main", significance=0.9)
    secondary = _fact("grid_price_rise", subject="a-secondary", significance=0.8)
    optional = _fact("optional_start", subject="dishwasher", significance=0.9)
    selected = (main, optional, secondary)
    model = OutlookModel(
        facts=selected,
        selected_fact_ids=tuple(fact.fact_id for fact in selected),
        selection_history=(),
        start=NOW,
        horizon_end=NOW + timedelta(hours=2),
        plan_created_at=NOW,
        seed_prefix="statement-order",
        statements=tuple(build_statements(list(selected))),
    )

    rendered = render_plan_outlook(model, now=NOW, language="en")

    assert "solar" in rendered["headline"].lower()
    assert rendered["line_1"] == rendered["headline"]
    assert rendered["report_id"].startswith("solar_surplus_high_")


def test_far_routine_fact_cannot_pad_near_term_story() -> None:
    """A routine battery action 22h out must not pad a near-term story.

    This mirrors the observed price-swing + low-grid-use + next-day preserve
    outlook. The far fact adds a topic, group, and price->action link, but
    those set bonuses must not outweigh its low timeliness.
    """
    price = _fact(
        "grid_price_swing",
        significance=0.9,
        deviation=0.8,
        related=("battery_preserve",),
    )
    balance = _fact(
        "limited_grid_use",
        topic="energy_balance",
        significance=0.5,
        deviation=0.4,
    )
    far_routine = _fact(
        "battery_preserve",
        subject="Battery",
        topic="battery",
        significance=0.5,
        deviation=0.2,
        start=NOW + timedelta(hours=22),
        related=("grid_price_swing",),
    )
    pool = [price, balance, far_routine]

    selected = select_best_set(pool, NOW, [])
    assert [fact.kind for fact in selected] == [
        "grid_price_swing",
        "limited_grid_use",
    ]

    scores = {item.fact.fact_id: item.score for item in score_candidates(pool, NOW, [])}
    near_total, _ = set_score((balance, price), scores, now=NOW)
    padded_total, breakdown = set_score(
        (balance, price, far_routine), scores, now=NOW
    )
    assert near_total > padded_total
    # The price-action link counts only at the far fact's timeliness.
    assert breakdown["coherence"] == pytest.approx(
        COHERENCE_WEIGHT * 2.0 * math.exp(-22 / TIMELINESS_HOURS) / 2
    )


def test_significant_next_day_event_survives_without_hard_cutoff() -> None:
    """Timeliness decays smoothly: a significant event 22h out still wins."""
    far_significant = _fact(
        "negative_grid_price",
        significance=0.9,
        deviation=0.9,
        start=NOW + timedelta(hours=22),
        related=("grid_charge",),
    )
    routine_near = _fact(
        "flat_grid_prices", significance=0.2, deviation=0.1, topic="routine"
    )

    selected = select_best_set([far_significant, routine_near], NOW, [])

    assert [fact.kind for fact in selected] == ["negative_grid_price"]
