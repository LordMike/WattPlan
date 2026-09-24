"""Pure Plan Outlook extraction, selection, and wording tests."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.wattplan.plan_outlook import (
    _select_facts,
    build_plan_outlook,
    build_plan_outlook_model,
    build_status_plan_outlook,
    render_stored_plan_outlook,
)
from custom_components.wattplan.plan_outlook_renderer import render_plan_outlook
from custom_components.wattplan.plan_outlook_types import OutlookFact


def _request(
    *,
    start: datetime | None = None,
    prices: list[float] | None = None,
    usage: list[float] | None = None,
    solar: list[float] | None = None,
    batteries: list[dict] | None = None,
    usage_configured: bool = True,
    pv_configured: bool = True,
) -> dict:
    start = start or datetime(2026, 9, 24, 0, tzinfo=UTC)
    prices = prices or [0.4, 0.3, -0.1, -0.1, 0.5, 0.7, 0.8, 0.9]
    usage = usage or [0.4] * len(prices)
    solar = solar or [0.0] * len(prices)
    return {
        "entry_id": "site-a",
        "slot_minutes": 15,
        "window": SimpleNamespace(start_at=start),
        "local_timezone": getattr(start.tzinfo, "key", "UTC"),
        "source_provenance": {
            "import_price": {"configured": True},
            "usage": {"configured": usage_configured},
            "pv": {"configured": pv_configured},
        },
        "optimizer_params": {
            "grid_import_price_per_kwh": prices,
            "usage_kwh": usage,
            "solar_input_kwh": solar,
            "battery_entities": batteries or [],
        },
    }


def _result(
    *,
    entities: list[dict] | None = None,
    optionals: list[dict] | None = None,
    flows: list[dict] | None = None,
) -> dict:
    return {
        "entities": entities or [],
        "optional_entity_options": optionals or [],
        "energy_flows": {"per_slot": flows or []},
    }


def _battery_schedule(states: list[str], levels: list[float]) -> dict:
    return {
        "name": "House battery",
        "type": "battery",
        "schedule": [
            {"state": state, "level": level}
            for state, level in zip(states, levels, strict=True)
        ],
    }


def test_model_is_language_neutral_and_renderer_is_independent() -> None:
    request = _request(prices=[0.1, 0.1, 0.8, 0.8])
    model = build_plan_outlook_model(request=request, result=_result())

    assert model is not None
    assert model.selected_facts
    assert all(not hasattr(fact, "text") for fact in model.facts)
    assert any(fact.kind == "grid_price_rise" for fact in model.facts)

    rendered = render_plan_outlook(model, now=request["window"].start_at)

    assert rendered["text"]
    assert rendered["language"] == "en"
    assert rendered["report_id"].startswith("grid_price_rise_high_")
    assert rendered["semantic_id"].startswith("grid_price_rise_high_")
    assert model == build_plan_outlook_model(request=request, result=_result())


def test_report_id_changes_with_displayed_semantic_values() -> None:
    request = _request(
        prices=[0.3] * 4,
        batteries=[
            {
                "name": "House battery",
                "capacity_kwh": 10,
                "initial_kwh": 3,
                "minimum_kwh": 1,
                "target": {"timeslot": 2, "soc_kwh": 8},
            }
        ],
    )
    first = build_plan_outlook(
        request=request,
        result=_result(
            entities=[_battery_schedule(["grid_charge"] * 4, [4, 5, 6.5, 7])]
        ),
    )
    second = build_plan_outlook(
        request=request,
        result=_result(
            entities=[_battery_schedule(["grid_charge"] * 4, [4, 5, 6.6, 7])]
        ),
    )

    assert first["report_id"] != second["report_id"]
    assert first["semantic_id"] != second["semantic_id"]
    assert first["report_id"].startswith("target_shortfall_high_")
    assert second["report_id"].startswith("target_shortfall_high_")


def test_status_report_id_is_stable_while_status_is_unchanged() -> None:
    first = build_status_plan_outlook(
        "plan_unavailable", now=datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    )
    second = build_status_plan_outlook(
        "plan_unavailable", now=datetime(2026, 9, 24, 12, 5, tzinfo=UTC)
    )

    assert first["report_id"] == second["report_id"]
    assert first["semantic_id"] == second["semantic_id"]


def test_negative_price_and_grid_charge_form_a_coherent_story() -> None:
    battery = _battery_schedule(
        ["self_consume", "self_consume", "grid_charge", "grid_charge", "self_consume"],
        [2, 2, 3, 4, 4],
    )
    request = _request(batteries=[{"name": "House battery", "capacity_kwh": 10}])
    outlook = build_plan_outlook(request=request, result=_result(entities=[battery]))

    assert outlook["information_value"] == "high"
    assert any(
        fact.startswith("negative_grid_price:") for fact in outlook["selected_facts"]
    )
    assert any(fact.startswith("grid_charge:") for fact in outlook["selected_facts"])
    assert "House battery" in outlook["text"]
    assert "from 00:30 to 01:00" in outlook["text"]
    assert len(outlook["text"].split()) <= 70


def test_nonconsecutive_charge_slots_are_not_reported_as_one_interval() -> None:
    battery = _battery_schedule(
        ["grid_charge", "self_consume", "self_consume", "grid_charge"],
        [2, 2, 2, 3],
    )
    request = _request(
        prices=[0.2] * 4,
        batteries=[{"name": "House battery", "capacity_kwh": 10}],
    )
    outlook = build_plan_outlook(request=request, result=_result(entities=[battery]))

    assert "from 00:00 to 01:00" not in outlook["text"]
    assert "from 00:00 to 00:15" in outlook["text"] or "from 00:45 to 01:00" in outlook["text"]


def test_target_shortfall_uses_end_of_slot_level_and_timestamp() -> None:
    request = _request(
        prices=[0.3] * 4,
        batteries=[
            {
                "name": "House battery",
                "capacity_kwh": 10,
                "initial_kwh": 3,
                "minimum_kwh": 1,
                "target": {"timeslot": 2, "soc_kwh": 8},
            }
        ],
    )
    battery = _battery_schedule(["grid_charge"] * 4, [4, 5, 6.5, 7])
    outlook = build_plan_outlook(request=request, result=_result(entities=[battery]))

    assert any(
        fact.startswith("target_shortfall:") for fact in outlook["selected_facts"]
    )
    assert "65%" in outlook["text"]
    assert "00:45" in outlook["text"]


def test_energy_balance_counts_grid_charging_as_import() -> None:
    flows = [
        {
            "base_load_kwh": 0.2,
            "comfort_load_kwh": 0.0,
            "grid_import_kwh": 0.7,
            "grid_export_kwh": 0.0,
            "grid_charge_kwh": 0.5,
        }
        for _ in range(8)
    ]
    request = _request(prices=[0.2] * 8, solar=[0.1] * 8)
    outlook = build_plan_outlook(request=request, result=_result(flows=flows))

    assert any(
        fact.startswith("charging_dominates_imports:")
        for fact in outlook["selected_facts"]
    )
    assert not any(
        fact.startswith("limited_grid_use:") for fact in outlook["selected_facts"]
    )


def test_untrusted_or_unconfigured_sources_suppress_balance_and_solar_claims() -> None:
    flows = [
        {
            "grid_import_kwh": 0,
            "grid_export_kwh": 1,
            "base_load_kwh": 0.1,
            "comfort_load_kwh": 0,
            "grid_charge_kwh": 0,
        }
        for _ in range(8)
    ]
    request = _request(prices=[0.2] * 8, solar=[1.0] * 8, pv_configured=False)
    outlook = build_plan_outlook(request=request, result=_result(flows=flows))

    assert "Solar" not in outlook["text"]
    assert "grid use" not in outlook["text"]
    assert "feed the grid" not in outlook["text"]

    request = _request(prices=[0.2] * 8, solar=[1.0] * 8)
    outlook = build_plan_outlook(
        request=request,
        result=_result(flows=flows),
        source_health={"source_pv": {"status": "degraded", "failure_slot_count": 1}},
    )
    assert "Solar builds" not in outlook["text"]
    assert "feed the grid" not in outlook["text"]


def test_expired_optional_suggestion_is_removed() -> None:
    request = _request(prices=[0.2] * 8)
    optional = {
        "name": "dishwasher",
        "options": [{"start_timeslot": 1}, {"start_timeslot": 6}],
    }
    now = request["window"].start_at + timedelta(hours=1)
    outlook = build_plan_outlook(
        request=request,
        result=_result(optionals=[optional]),
        now=now,
    )

    assert "00:15" not in outlook["text"]
    assert "01:30" in outlook["text"]


def test_persistent_source_problem_is_debounced_and_suppresses_precise_advice() -> None:
    request = _request(prices=[0.2, 0.2, 0.6, 0.8], solar=[0.0] * 4)
    transient = build_plan_outlook(
        request=request,
        result=_result(),
        source_health={
            "source_pv": {
                "status": "degraded",
                "failure_slot_count": 3,
                "configured": True,
            }
        },
    )
    assert "Solar updates" not in transient["text"]

    persistent = build_plan_outlook(
        request=request,
        result=_result(),
        source_health={
            "source_pv": {
                "status": "degraded",
                "is_stale": True,
                "failure_slot_count": 4,
                "failure_elapsed_minutes": 60,
                "configured": True,
            }
        },
    )
    assert persistent["selected_facts"] == ["source_problem:source_pv"]
    assert "solar" in persistent["text"].lower()
    assert "an hour" in persistent["text"]
    assert persistent["topic"] == "reliability"


def test_restored_plan_never_publishes_action_advice() -> None:
    request = _request()
    outlook = build_plan_outlook(
        request=request,
        result=_result(
            entities=[_battery_schedule(["grid_charge"] * 8, list(range(8)))]
        ),
        plan_validated=False,
    )

    assert outlook["topic"] == "reliability"
    assert "restart" in outlook["text"]
    assert "charge from the grid" not in outlook["text"]


def test_selection_is_stable_and_history_changes_only_on_selection_change() -> None:
    request = _request(prices=[0.1, 0.1, 0.8, 0.8])
    first = build_plan_outlook(request=request, result=_result())
    second = build_plan_outlook(
        request=_request(prices=[0.1001, 0.1, 0.8001, 0.8]),
        result=_result(),
        previous_outlook=first,
    )

    assert second["selected_facts"] == first["selected_facts"]
    assert second["_model"]["selection_history"] == first["_model"]["selection_history"]


def test_wording_varies_predictably_across_reporting_periods() -> None:
    prices = [0.1] * 48 + [0.8] * 48
    request = _request(
        prices=prices,
        usage=[0.2] * 96,
        solar=[0.0] * 96,
        pv_configured=False,
    )
    start = request["window"].start_at
    model = build_plan_outlook_model(request=request, result=_result(), now=start)

    assert model is not None
    reports = [
        render_plan_outlook(
            model,
            now=start + timedelta(hours=hour),
            variation_seed="fixed-test-seed",
        )
        for hour in (8, 14, 20)
    ]

    assert len({report["line_1"] for report in reports}) == 3
    assert len({report["report_id"] for report in reports}) == 3
    assert len({report["semantic_id"] for report in reports}) == 1
    assert all(
        any(fact.startswith("grid_price_rise:") for fact in report["selected_facts"])
        for report in reports
    )


def test_unchanged_semantic_report_reuses_its_existing_wording() -> None:
    request = _request(prices=[0.1] * 48 + [0.8] * 48)
    model = build_plan_outlook_model(request=request, result=_result())

    assert model is not None
    morning = render_plan_outlook(
        model,
        now=request["window"].start_at + timedelta(hours=8),
        variation_seed="fixed-test-seed",
    )
    afternoon = render_plan_outlook(
        model,
        now=request["window"].start_at + timedelta(hours=14),
        previous_outlook=morning,
        variation_seed="different-test-seed",
    )

    assert afternoon["semantic_id"] == morning["semantic_id"]
    assert afternoon["_render_variants"] == morning["_render_variants"]
    assert afternoon["line_1"] == morning["line_1"]
    assert afternoon["report_id"] == morning["report_id"]


def test_midnight_and_dst_coverage_remain_timezone_aware() -> None:
    zone = ZoneInfo("Europe/Copenhagen")
    start = datetime(2026, 10, 25, 1, 30, tzinfo=zone)
    request = _request(start=start, prices=[0.2] * 8)
    outlook = build_plan_outlook(request=request, result=_result(), now=start)

    assert outlook["covered_start"].endswith("+02:00")
    assert datetime.fromisoformat(outlook["covered_end"]).tzinfo is not None


def test_retained_outlook_ages_out_observations_and_action_advice() -> None:
    request = _request(
        prices=[0.2] * 12,
        batteries=[
            {
                "name": "House battery",
                "capacity_kwh": 10,
                "initial_kwh": 1.1,
                "minimum_kwh": 1.0,
            }
        ],
    )
    battery = _battery_schedule(
        ["grid_charge", "grid_charge", *(["self_consume"] * 10)],
        [2.0, 3.0, *([3.0] * 10)],
    )
    optional = {"name": "dishwasher", "options": [{"start_timeslot": 3}]}
    initial = build_plan_outlook(
        request=request,
        result=_result(entities=[battery], optionals=[optional]),
        now=request["window"].start_at,
    )

    retained = render_stored_plan_outlook(
        initial,
        now=request["window"].start_at + timedelta(hours=2),
    )

    assert not any(
        fact.startswith(("low_reserve:", "grid_charge:"))
        for fact in retained["selected_facts"]
    )
    assert "dishwasher" not in retained["text"]
    assert (
        retained["covered_start"]
        == (request["window"].start_at + timedelta(hours=2)).isoformat()
    )


def test_retained_outlook_updates_day_label_after_midnight() -> None:
    start = datetime(2026, 9, 24, 23, 30, tzinfo=UTC)
    request = _request(
        start=start,
        prices=[0.2] * 12,
        usage=[0.2] * 12,
        solar=[0.0] * 12,
        pv_configured=False,
    )
    initial = build_plan_outlook(request=request, result=_result(), now=start)
    retained = render_stored_plan_outlook(
        initial,
        now=start + timedelta(hours=1),
    )

    assert "today" in retained["text"]
    assert "tomorrow" not in retained["text"]


def test_retained_outlook_adds_current_source_health() -> None:
    request = _request(prices=[0.2, 0.2, 0.6, 0.8])
    initial = build_plan_outlook(request=request, result=_result())
    retained = render_stored_plan_outlook(
        initial,
        now=request["window"].start_at + timedelta(hours=1),
        source_health={
            "source_pv": {
                "status": "degraded",
                "configured": True,
                "is_stale": True,
                "failure_slot_count": 4,
                "failure_started_at": (
                    request["window"].start_at - timedelta(hours=1)
                ).isoformat(),
            }
        },
    )

    assert "solar" in retained["text"].lower()
    assert "2 hours" in retained["text"]
    assert retained["topic"] == "reliability"


def test_retained_outlook_models_plan_refresh_failure_before_rendering() -> None:
    request = _request(prices=[0.2, 0.2, 0.6, 0.8])
    initial = build_plan_outlook(request=request, result=_result())
    started_at = request["window"].start_at - timedelta(hours=1)

    retained = render_stored_plan_outlook(
        initial,
        now=request["window"].start_at,
        plan_failure_status={"slot_count": 4, "started_at": started_at},
    )

    assert "an hour" in retained["text"]
    assert "plan" in retained["text"].lower()
    assert "plan_refresh_failure:plan" in retained["selected_facts"]
    assert retained["topic"] == "reliability"
    assert retained["information_value"] == "high"


def test_multiple_source_problems_produce_one_prioritized_warning() -> None:
    request = _request(prices=[0.2, 0.2, 0.6, 0.8])
    outlook = build_plan_outlook(
        request=request,
        result=_result(),
        source_health={
            "source_import_price": {
                "status": "unavailable",
                "failure_slot_count": 4,
                "failure_elapsed_minutes": 60,
                "configured": True,
            },
            "source_usage": {
                "status": "unavailable",
                "failure_slot_count": 4,
                "failure_elapsed_minutes": 120,
                "configured": True,
            },
        },
    )

    assert outlook["selected_facts"] == ["source_problem:source_usage"]
    assert outlook["topic"] == "reliability"


def test_negative_price_only_combines_with_matching_grid_charge() -> None:
    start = datetime(2026, 9, 24, 0, tzinfo=UTC)

    def fact(
        kind: str,
        *,
        value: str = "medium",
        related: tuple[str, ...] = (),
        significance: int = 1,
    ) -> OutlookFact:
        return OutlookFact(
            fact_id=f"{kind}:test",
            kind=kind,
            topic="opportunity",
            information_value=value,
            basis="forecast",
            start=start,
            end=start + timedelta(hours=1),
            related=related,
            significance=significance,
        )

    selected = _select_facts(
        [
            fact(
                "negative_grid_price",
                value="high",
                related=("grid_charge",),
                significance=3,
            ),
            fact("grid_price_rise", value="high", significance=2),
            fact("grid_charge", related=("negative_grid_price",)),
            fact("optional_start"),
        ],
        previous=None,
    )

    assert [item.kind for item in selected] == ["negative_grid_price", "grid_charge"]


def test_selection_avoids_redundant_fact_groups() -> None:
    start = datetime(2026, 9, 24, 0, tzinfo=UTC)

    def fact(kind: str, topic: str, value: str, significance: int) -> OutlookFact:
        return OutlookFact(
            fact_id=f"{kind}:test",
            kind=kind,
            topic=topic,
            information_value=value,
            basis="forecast",
            start=start,
            end=start + timedelta(hours=1),
            significance=significance,
        )

    selected = _select_facts(
        [
            fact("heavy_grid_use", "energy_balance", "high", 3),
            fact("grid_use_decrease", "energy_balance", "high", 2),
            fact("solar_surplus", "energy_balance", "medium", 2),
            fact("grid_export", "energy_balance", "medium", 1),
            fact("optional_start", "opportunity", "medium", 1),
        ],
        previous=None,
    )
    kinds = {item.kind for item in selected}

    assert len(kinds & {"heavy_grid_use", "grid_use_decrease", "grid_export"}) == 1
    assert not {"solar_surplus", "grid_export"} <= kinds


def test_comfort_and_optional_names_are_preserved_exactly() -> None:
    request = _request(
        prices=[0.2] * 4,
        usage=[0.1] * 4,
        solar=[0.5] * 4,
    )
    model = build_plan_outlook_model(
        request=request,
        result=_result(
            entities=[
                {
                    "name": "eBike_Pump",
                    "type": "comfort",
                    "schedule": [{"enabled": True}] * 4,
                }
            ],
            optionals=[
                {
                    "name": "Dishwasher_A",
                    "options": [{"start_timeslot": 1}],
                }
            ],
        ),
    )

    assert model is not None
    subjects = {fact.kind: fact.subject for fact in model.facts}
    assert subjects["comfort_timing"] == "eBike_Pump"
    assert subjects["optional_start"] == "Dishwasher_A"
