"""Danish localization tests for the pure Plan Outlook renderer."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.wattplan.plan_outlook_renderer import (
    render_fact_danish,
    render_fact_english,
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


FACT_VARIANT_CASES = (
    ("grid_price_rise", "site", ()),
    ("grid_price_fall", "site", ()),
    ("limited_grid_use", "site", ()),
    ("flat_grid_prices", "site", ()),
    ("negative_grid_price", "site", ()),
    ("cheaper_grid_prices", "site", ()),
    (
        "grid_price_swing",
        "site",
        (("turn_at", (NOW + timedelta(hours=1)).isoformat()), ("direction", "ease_then_rise")),
    ),
    (
        "grid_price_swing",
        "site",
        (("turn_at", (NOW + timedelta(hours=1)).isoformat()), ("direction", "rise_then_ease")),
    ),
    ("solar_surplus", "site", (("peak_at", (NOW + timedelta(hours=2)).isoformat()),)),
    ("solar_modest", "site", (("peak_at", (NOW + timedelta(hours=2)).isoformat()),)),
    ("solar_fading", "site", ()),
    ("low_reserve", "Husbatteri_A", ()),
    ("grid_charge", "Husbatteri_A", ()),
    ("battery_preserve", "Husbatteri_A", ()),
    ("battery_self_consume", "Husbatteri_A", ()),
    ("battery_full", "Husbatteri_A", ()),
    (
        "target_shortfall",
        "Husbatteri_A",
        (("expected_percent", 65), ("requested_percent", 80)),
    ),
    ("target_shortfall", "Husbatteri_A", ()),
    ("target_reached", "Husbatteri_A", (("requested_percent", 80),)),
    ("target_reached", "Husbatteri_A", ()),
    ("comfort_timing", "eBike_Pump", ()),
    (
        "optional_start",
        "Dishwasher_A",
        (("alternative_at", (NOW + timedelta(hours=3)).isoformat()),),
    ),
    ("optional_start", "Dishwasher_A", ()),
    ("grid_export", "site", ()),
    ("heavy_grid_use", "site", ()),
    ("charging_dominates_imports", "site", ()),
    ("grid_use_increase", "site", ()),
    ("grid_use_decrease", "site", ()),
    ("source_problem", "source_pv", (("elapsed_minutes", 60), ("stale", True))),
    ("source_problem", "source_pv", (("elapsed_minutes", 60), ("stale", False))),
    (
        "source_problem",
        "source_import_price",
        (("elapsed_minutes", 60), ("stale", True)),
    ),
    (
        "source_problem",
        "source_import_price",
        (("elapsed_minutes", 60), ("stale", False)),
    ),
    ("plan_refresh_failure", "plan", (("elapsed_minutes", 60),)),
    ("plan_unavailable", "plan", ()),
    ("plan_unusable", "plan", ()),
    ("plan_expired", "plan", ()),
    ("restored_unvalidated", "plan", ()),
    ("recommendations_unavailable", "plan", ()),
    ("stored_recommendations_unvalidated", "plan", ()),
    ("quiet", "plan", ()),
)


@pytest.mark.parametrize(
    "renderer",
    (render_fact_english, render_fact_danish),
    ids=("english", "danish"),
)
@pytest.mark.parametrize(("kind", "subject", "values"), FACT_VARIANT_CASES)
def test_every_supported_fragment_has_at_least_three_variants(
    renderer, kind: str, subject: str, values
) -> None:
    fact = _fact(kind, subject=subject, values=values)

    variants = {
        renderer(
            fact,
            model=_model(fact),
            now=NOW,
            variation_seed="test",
            forced_variant=variant,
        )[0]
        for variant in range(3)
    }

    assert len(variants) == 3


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("grid_price_rise", "importpriser"),
        ("grid_price_fall", "importpriser"),
        ("limited_grid_use", "elnettet"),
    ),
)
def test_common_danish_facts_have_four_variants(
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
        for variant in range(4)
    }

    assert len(variants) == 4
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
        forced_variant=0,
    )

    assert "1 minut" in text
    assert "Soldata" in text

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
        forced_variant=0,
    )

    assert "importpriser" in text
    assert "source_import_price" not in text


def test_danish_status_and_reliability_branches_are_localized() -> None:
    for kind, expected in (
        ("plan_unavailable", "Planlægning"),
        ("plan_unusable", "Planlægningen"),
        ("plan_expired", "udløbet"),
        ("restored_unvalidated", "genstart"),
        ("recommendations_unavailable", "anbefalinger"),
        ("stored_recommendations_unvalidated", "Gemte forslag"),
        ("quiet", "ændringer"),
    ):
        fact = _fact(kind)
        text, _ = render_fact_danish(
            fact,
            model=_model(fact),
            now=NOW,
            variation_seed="test",
            forced_variant=0,
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


def test_time_ranges_are_explicit_in_both_languages() -> None:
    fact = _fact("grid_charge", subject="Husbatteri_A")
    model = _model(fact)

    english, _ = render_fact_english(
        fact,
        model=model,
        now=NOW,
        variation_seed="test",
        forced_variant=0,
    )
    danish, _ = render_fact_danish(
        fact,
        model=model,
        now=NOW,
        variation_seed="test",
        forced_variant=0,
    )

    assert "from 09:00 to 10:00" in english
    assert "fra kl. 09:00 til kl. 10:00" in danish
    assert "09:00-10:00" not in english
    assert "09:00-10:00" not in danish


def test_danish_percentages_use_danish_spacing() -> None:
    fact = _fact(
        "target_shortfall",
        subject="Husbatteri_A",
        values=(("expected_percent", 65), ("requested_percent", 80)),
    )

    text, _ = render_fact_danish(
        fact,
        model=_model(fact),
        now=NOW,
        variation_seed="test",
        forced_variant=0,
    )

    assert "65 %" in text
    assert "80 %" in text
    assert "65%" not in text
