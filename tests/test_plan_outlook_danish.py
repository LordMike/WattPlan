"""Danish localization tests for the pure Plan Outlook renderer."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fluent.syntax import FluentParser
from fluent.syntax.ast import Junk

from custom_components.wattplan.plan_outlook_renderer import (
    _bundle,
    _format,
    _message_id,
    _variant_counts,
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
    (
        "source_problem",
        "source_export_price",
        (("elapsed_minutes", 60), ("stale", True)),
    ),
    (
        "source_problem",
        "source_export_price",
        (("elapsed_minutes", 60), ("stale", False)),
    ),
    (
        "source_problem",
        "source_usage",
        (("elapsed_minutes", 60), ("stale", True)),
    ),
    (
        "source_problem",
        "source_usage",
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
    ("language", "renderer"),
    (("en", render_fact_english), ("da", render_fact_danish)),
    ids=("english", "danish"),
)
@pytest.mark.parametrize(("kind", "subject", "values"), FACT_VARIANT_CASES)
def test_every_supported_fragment_formats_every_declared_variant(
    language: str, renderer, kind: str, subject: str, values
) -> None:
    fact = _fact(kind, subject=subject, values=values)
    variant_count = _variant_counts(language)[_message_id(fact)]

    variants = {
        renderer(
            fact,
            model=_model(fact),
            now=NOW,
            variation_seed="test",
            forced_variant=variant,
        )[0]
        for variant in range(variant_count)
    }

    assert variant_count >= 3
    assert len(variants) == variant_count


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

    assert "et minut" in text
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

    english, _ = render_fact_english(
        fact,
        model=_model(fact),
        now=NOW,
        variation_seed="test",
        forced_variant=0,
    )
    assert "65%" in english
    assert "80%" in english


@pytest.mark.parametrize("renderer", (render_fact_english, render_fact_danish))
def test_rendering_preserves_user_defined_subject_exactly(renderer) -> None:
    subject = "Min_Egen Batteripakke 2"
    fact = _fact("grid_charge", subject=subject)

    text, _ = renderer(
        fact,
        model=_model(fact),
        now=NOW,
        variation_seed="subject",
        forced_variant=0,
    )

    assert subject in text


@pytest.mark.parametrize("language", ("en", "da"))
def test_fluent_catalogues_parse_and_load(language: str) -> None:
    _bundle.cache_clear()
    assert _bundle(language).get_message("outlook-grid-charge").value is not None


def test_catalogues_have_no_junk_and_cover_every_semantic_branch() -> None:
    facts = [
        _fact(kind, subject=subject, values=values)
        for kind, subject, values in FACT_VARIANT_CASES
    ]
    message_ids = {_message_id(fact) for fact in facts}
    message_ids.update(
        {
            _message_id(_fact("target_shortfall", values=(("expected_percent", 65),))),
            _message_id(_fact("target_reached", values=(("requested_percent", 80),))),
            _message_id(_fact("optional_start", values=(("alternative_at", NOW.isoformat()),))),
        }
    )
    root = Path(__file__).parents[1] / "custom_components" / "wattplan" / "locales"
    for language in ("en", "da"):
        parsed = FluentParser().parse((root / language / "plan_outlook.ftl").read_text())
        assert not [entry for entry in parsed.body if isinstance(entry, Junk)]
        bundle = _bundle(language)
        for message_id in message_ids:
            assert bundle.get_message(message_id).value is not None

    assert set(_variant_counts("en")) == set(_variant_counts("da"))


@pytest.mark.parametrize("language", ("en", "da"))
def test_common_recurring_messages_keep_four_variants(language: str) -> None:
    counts = _variant_counts(language)
    for message_id in {
        "outlook-grid-price-rise",
        "outlook-grid-price-fall",
        "outlook-limited-grid-use",
        "outlook-flat-grid-prices",
        "outlook-negative-grid-price",
        "outlook-cheaper-grid-prices",
        "outlook-grid-charge",
        "outlook-target-shortfall-known",
        "outlook-optional-start-alternative",
        "outlook-source-problem-pv-stale",
        "outlook-plan-refresh-failure",
        "outlook-quiet",
    }:
        assert counts[message_id] == 4


@pytest.mark.parametrize(
    ("minutes", "english", "danish"),
    (
        (1, "one minute", "et minut"),
        (59, "59 minutes", "59 minutter"),
        (60, "an hour", "en time"),
        (119, "an hour", "en time"),
        (120, "2 hours", "2 timer"),
        (121, "2 hours", "2 timer"),
    ),
)
def test_duration_rounding_and_plural_grammar(
    minutes: int, english: str, danish: str
) -> None:
    fact = _fact("source_problem", subject="source_usage", values=(("elapsed_minutes", minutes),))
    model = _model(fact)
    en_text, _ = render_fact_english(fact, model=model, now=NOW, variation_seed="duration", forced_variant=0)
    da_text, _ = render_fact_danish(fact, model=model, now=NOW, variation_seed="duration", forced_variant=0)

    assert english in en_text
    assert danish in da_text
    assert "an hour minutes" not in en_text
    assert "hour" not in da_text


@pytest.mark.parametrize(
    ("offset", "english", "danish"),
    (
        (-1, "for the rest of today", "resten af dagen"),
        (0, "today", "i dag"),
        (1, "tomorrow", "i morgen"),
        (7, "on Thursday", "på torsdag"),
    ),
)
def test_flat_price_period_uses_localized_today_tomorrow_and_weekdays(
    offset: int, english: str, danish: str
) -> None:
    fact = _fact("flat_grid_prices")
    start = NOW + timedelta(days=offset)
    if offset < 0:
        start = NOW - timedelta(minutes=30)
    fact = OutlookFact(
        **{field: getattr(fact, field) for field in fact.__dataclass_fields__} | {"start": start, "end": start + timedelta(hours=1)}
    )
    model = _model(fact)
    en_text, _ = render_fact_english(fact, model=model, now=NOW, variation_seed="period", forced_variant=0)
    da_text, _ = render_fact_danish(fact, model=model, now=NOW, variation_seed="period", forced_variant=0)

    assert english in en_text
    assert danish in da_text


@pytest.mark.parametrize(
    ("source", "stale", "expected_en", "expected_da"),
    (
        ("source_import_price", True, "last values are still being used", "seneste værdier bruges stadig"),
        ("source_import_price", False, "cannot be trusted", "kan ikke stoles på"),
        ("source_export_price", True, "earlier values remain in use", "tidligere værdier bruges stadig"),
        ("source_export_price", False, "export value is uncertain", "værdien af eksport er usikker"),
        ("source_usage", True, "last forecast remains in use", "seneste prognose bruges stadig"),
        ("source_usage", False, "demand-aware advice is limited", "forbrugsbaserede råd er begrænsede"),
        ("source_pv", True, "last forecast remains in use", "seneste prognose bruges stadig"),
        ("source_pv", False, "solar-based advice is limited", "solbaserede råd er begrænsede"),
    ),
)
def test_source_problem_preserves_source_grammar_and_consequence(
    source: str, stale: bool, expected_en: str, expected_da: str
) -> None:
    fact = _fact("source_problem", subject=source, values=(("elapsed_minutes", 60), ("stale", stale)))
    model = _model(fact)
    en_text, _ = render_fact_english(fact, model=model, now=NOW, variation_seed="source", forced_variant=0)
    da_text, _ = render_fact_danish(fact, model=model, now=NOW, variation_seed="source", forced_variant=0)

    assert expected_en in en_text
    assert expected_da in da_text
    assert source not in en_text + da_text


def test_branch_specific_messages_keep_alternatives_targets_and_swing_direction() -> None:
    alternative = _fact("optional_start", subject="Dishwasher", values=(("alternative_at", (NOW + timedelta(hours=3)).isoformat()),))
    only = _fact("optional_start", subject="Dishwasher")
    known = _fact("target_shortfall", values=(("expected_percent", 65), ("requested_percent", 80)))
    missing = _fact("target_shortfall")
    rising = _fact("grid_price_swing", values=(("direction", "ease_then_rise"),))
    easing = _fact("grid_price_swing", values=(("direction", "rise_then_ease"),))

    for language_renderer in (render_fact_english, render_fact_danish):
        texts = [
            language_renderer(fact, model=_model(fact), now=NOW, variation_seed="branch", forced_variant=0)[0]
            for fact in (alternative, only, known, missing, rising, easing)
        ]
        assert "11:00" in texts[0]
        assert "11:00" not in texts[1]
        assert "65" in texts[2]
        assert "65" not in texts[3]
        assert texts[4] != texts[5]


def test_variant_counts_follow_each_catalogue_message_and_cache_reads(monkeypatch) -> None:
    _bundle.cache_clear()
    _variant_counts.cache_clear()
    calls = 0
    original = Path.read_text

    def counted(path: Path, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    fact = _fact("optional_start")
    for _ in range(2):
        render_fact_english(fact, model=_model(fact), now=NOW, variation_seed="cache")

    assert _variant_counts("en")[_message_id(fact)] == 3
    assert calls == 2


def test_catalogue_loading_or_formatting_failure_uses_nonrecursive_fallback(monkeypatch) -> None:
    original_bundle = _bundle

    def danish_broken(language: str):
        if language == "da":
            raise ValueError("broken Danish catalogue")
        return original_bundle(language)

    monkeypatch.setattr(
        "custom_components.wattplan.plan_outlook_renderer._bundle", danish_broken
    )
    assert _format(
        "da",
        "outlook-grid-charge",
        {"subject": "Battery", "start": "09:00", "end": "10:00", "variant": 0},
    ).startswith("Battery is scheduled")

    monkeypatch.setattr(
        "custom_components.wattplan.plan_outlook_renderer._bundle", original_bundle
    )
    assert _format("da", "outlook-grid-charge", {"variant": 0}) == (
        "Plan update: grid charge."
    )

    def all_broken(language: str):
        raise ValueError("broken catalogue")

    monkeypatch.setattr(
        "custom_components.wattplan.plan_outlook_renderer._bundle", all_broken
    )
    assert _format("da", "outlook-grid-charge", {"kind": "grid charge"}) == "Plan update: grid charge."


def test_report_id_tracks_rendered_wording_but_semantic_id_does_not(monkeypatch) -> None:
    fact = _fact("grid_price_rise")
    model = _model(fact)
    first = render_plan_outlook(
        model, now=NOW, language="en", variation_seed="wording-id"
    )
    original_format = _format

    def revised_format(language: str, message_id: str, arguments: dict) -> str:
        return original_format(language, message_id, arguments) + " Revised."

    monkeypatch.setattr(
        "custom_components.wattplan.plan_outlook_renderer._format", revised_format
    )
    revised = render_plan_outlook(
        model, now=NOW, language="en", variation_seed="wording-id"
    )

    assert revised["semantic_id"] == first["semantic_id"]
    assert revised["report_id"] != first["report_id"]
