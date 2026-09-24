"""Danish localization tests for the pure Plan Outlook renderer."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.wattplan.plan_outlook_renderer import (
    render_fact_danish,
    render_plan_outlook,
)
from custom_components.wattplan.plan_outlook_types import OutlookFact, OutlookModel

NOW = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)


def _fact(
    kind: str,
    *,
    subject: str = "Husbatteri",
    values: tuple[tuple[str, str | int | float | bool | None], ...] = (),
) -> OutlookFact:
    return OutlookFact(
        fact_id=f"{kind}:test",
        kind=kind,
        topic="energy",
        information_value="high",
        basis="forecast",
        start=NOW + timedelta(hours=1),
        end=NOW + timedelta(hours=2),
        subject=subject,
        values=values,
    )


def _model(*facts: OutlookFact) -> OutlookModel:
    return OutlookModel(
        facts=facts,
        selected_fact_ids=tuple(fact.fact_id for fact in facts),
        selection_history=(),
        start=NOW,
        horizon_end=NOW + timedelta(hours=12),
        plan_created_at=NOW,
        seed_prefix="danish-test",
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("grid_price_rise", "elpriser"),
        ("grid_price_fall", "elpriser"),
        ("limited_grid_use", "netforbrug"),
    ),
)
def test_danish_variable_grid_facts_have_three_variants(
    kind: str, expected: str
) -> None:
    fact = _fact(kind)
    model = _model(fact)

    variants = {
        render_fact_danish(
            fact,
            model=model,
            now=NOW,
            variation_seed="test",
            forced_variant=variant,
        )[0]
        for variant in range(3)
    }

    assert len(variants) == 3
    assert all(expected in text.lower() for text in variants)


def test_danish_preserves_subject_and_uses_danish_duration_forms() -> None:
    fact = _fact(
        "source_problem",
        subject="source_pv",
        values=(("elapsed_minutes", 1), ("stale", True)),
    )

    text, _ = render_fact_danish(
        fact,
        model=_model(fact),
        now=NOW,
        variation_seed="test",
    )

    assert "1 minut" in text
    assert "Solopdateringer" in text

    target = _fact(
        "target_reached",
        subject="Min Egen Batteripakke",
        values=(("requested_percent", 80),),
    )
    target_text, _ = render_fact_danish(
        target,
        model=_model(target),
        now=NOW,
        variation_seed="test",
    )

    assert "Min Egen Batteripakke" in target_text


def test_danish_translates_internal_source_names() -> None:
    fact = _fact(
        "source_problem",
        subject="source_import_price",
        values=(("elapsed_minutes", 45),),
    )

    text, _ = render_fact_danish(
        fact,
        model=_model(fact),
        now=NOW,
        variation_seed="test",
    )

    assert "importprisen" in text
    assert "source_import_price" not in text


def test_danish_status_and_reliability_branches_are_localized() -> None:
    for kind, expected in (
        ("plan_unavailable", "Planlægning"),
        ("plan_unusable", "Planlægningen"),
        ("plan_expired", "udløbet"),
        ("restored_unvalidated", "genstart"),
        ("recommendations_unavailable", "anbefalinger"),
        ("stored_recommendations_unvalidated", "Gemte forslag"),
        ("quiet", "planændring"),
    ):
        fact = _fact(kind)
        text, _ = render_fact_danish(
            fact,
            model=_model(fact),
            now=NOW,
            variation_seed="test",
        )
        assert expected in text


def test_language_changes_report_id_but_not_semantic_id_or_variant_reuse() -> None:
    fact = _fact("grid_price_rise")
    model = _model(fact)
    english = render_plan_outlook(
        model,
        now=NOW,
        language="en",
        variation_seed="language-test",
    )
    danish = render_plan_outlook(
        model,
        now=NOW,
        language="da",
        previous_outlook=english,
        variation_seed="language-test",
    )
    danish_reused = render_plan_outlook(
        model,
        now=NOW + timedelta(hours=6),
        language="da",
        previous_outlook=danish,
        variation_seed="other-seed",
    )

    assert english["semantic_id"] == danish["semantic_id"]
    assert english["report_id"] != danish["report_id"]
    assert danish["language"] == "da"
    assert danish_reused["_render_variants"] == danish["_render_variants"]
    assert danish_reused["report_id"] == danish["report_id"]


def test_unknown_language_falls_back_to_english() -> None:
    fact = _fact("flat_grid_prices")
    model = _model(fact)

    fallback = render_plan_outlook(model, now=NOW, language="fr")
    english = render_plan_outlook(model, now=NOW, language="en")

    assert fallback["language"] == "en"
    assert fallback["report_id"] == english["report_id"]
