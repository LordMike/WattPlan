"""Greedy forecast planning economics, target behavior and shared contract."""

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.greedy import factory
from tests.optimizer.planner_conformance import assert_conforms

PATH = "custom_components.wattplan.optimizer.planners.greedy:factory"


def request(
    prices,
    *,
    usage=None,
    pv=None,
    level=0,
    efficiency=1,
    target=None,
    ingress=3,
    export=None,
    capacity=3,
    curve=None,
):
    usage = usage if usage is not None else [1] * len(prices)
    pv = pv if pv is not None else [0] * len(prices)
    export = export if export is not None else [0] * len(prices)
    padding = max(0, 4 - len(prices))
    prices, usage, pv, export = [
        series + [0] * padding for series in (prices, usage, pv, export)
    ]
    battery = dict(
        name="house",
        initial_kwh=level,
        minimum_kwh=0,
        capacity_kwh=capacity,
        charge_curve_kwh=curve or [1],
        discharge_curve_kwh=[1],
        charge_efficiency=efficiency,
        discharge_efficiency=efficiency,
        can_charge_from=ingress,
    )
    if target is not None:
        battery["target"] = target
    return OptimizationParams(
        plan_start="2026-06-15T00:00:00+00:00",
        slot_minutes=15,
        grid_import_price_per_kwh=prices,
        grid_export_price_per_kwh=export or [0] * len(prices),
        usage_kwh=usage or [1] * len(prices),
        solar_input_kwh=pv or [0] * len(prices),
        battery_entities=[battery],
        comfort_entities=[],
    )


@pytest.mark.parametrize(
    "options",
    [
        {"max_evaluations": 0},
        {"max_evaluations": True},
        {"max_evaluations": 1.5},
        {"trade_window_slots": 0},
        {"trade_window_slots": 673},
        {"trade_window_slots": True},
    ],
)
def test_invalid_options(options):
    with pytest.raises(ValueError):
        factory(**options)


def test_tariff_trade_and_efficiency():
    params = request([0.1, 1], usage=[0, 1])
    result = assert_conforms(PATH, params)
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert [a["state"] for a in result["entities"][0]["schedule"]][:2] == [
        "grid_charge",
        "self_consume",
    ]
    inefficient = assert_conforms(
        PATH, request([0.5, 0.6], usage=[0, 1], efficiency=0.5)
    )
    assert inefficient["projections"]["projected_cost"] == pytest.approx(0.6)


def test_preserves_existing_energy_for_expensive_load():
    result = assert_conforms(PATH, request([0.1, 1], level=1))
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)


def test_negative_import_gets_paid_to_charge_even_at_horizon_end():
    result = assert_conforms(PATH, request([-0.5], usage=[0]))
    assert result["projections"]["projected_cost"] == pytest.approx(-0.5)
    assert result["projections"]["final_soc_kwh"]["house"] == pytest.approx(1)


def test_pv_only_negative_export_and_taper():
    result = assert_conforms(
        PATH,
        request(
            [0.1, 1], usage=[0, 1], pv=[2, 0], ingress=2, export=[-0.5, 0], curve=[1, 0]
        ),
    )
    assert all(a["state"] != "grid_charge" for a in result["entities"][0]["schedule"])
    assert result["projections"]["projected_cost"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "mode,level,wanted",
    [("at_least", 0, 2), ("exact", 0, 2), ("at_most", 2, 1), ("exact", 2, 1)],
)
def test_deadline_modes(mode, level, wanted):
    result = assert_conforms(
        PATH,
        request(
            [0.1, 1],
            level=level,
            target={"timeslot": 1, "soc_kwh": wanted, "mode": mode},
        ),
    )
    assert result["diagnostics"]["target_miss_kwh"] == pytest.approx(0)


def test_impossible_exact_is_reported_without_solver_fallback():
    result = assert_conforms(
        PATH,
        request(
            [1], usage=[0], target={"timeslot": 0, "soc_kwh": 0.4, "mode": "exact"}
        ),
    )
    assert result["diagnostics"]["target_miss_kwh"] == pytest.approx(0.4)
    assert result["diagnostics"]["evaluation"]["terminal_energy_credit"] == 0


def test_budget_and_determinism_over_full_horizon():
    params = request([0.1, 1] * 336)
    first = assert_conforms(PATH, params, {"max_evaluations": 5})
    second = assert_conforms(PATH, params, {"max_evaluations": 5})
    assert first["entities"] == second["entities"]
    assert first["diagnostics"]["evaluations"] == 5
    assert first["diagnostics"]["lookahead_slots_used"] is False
    assert len(first["entities"][0]["schedule"]) == 672


def test_target_priority_beats_high_import_cost_and_tolerance():
    params = request(
        [10, 12, 0, 0],
        usage=[0, 0, 0, 0],
        target={"timeslot": 1, "soc_kwh": 1.1, "tolerance_kwh": 0.1},
    )
    result = assert_conforms(PATH, params)
    assert result["diagnostics"]["target_miss_kwh"] == 0
    assert result["projections"]["projected_cost"] == pytest.approx(10)


def test_at_most_without_load_is_impossible_and_reported():
    result = assert_conforms(
        PATH,
        request(
            [1] * 4,
            usage=[0] * 4,
            level=2,
            target={"timeslot": 3, "soc_kwh": 1, "mode": "at_most"},
        ),
    )
    assert result["diagnostics"]["target_miss_kwh"] == pytest.approx(1)


def test_throughput_preference_can_prevent_paid_grid_charge():
    params = request([-0.1] * 4, usage=[0] * 4)
    params = params.model_copy(update={"throughput_cost_per_kwh": 1.0})
    result = assert_conforms(PATH, params)
    assert result["projections"]["final_soc_kwh"]["house"] == 0


def test_many_batteries_with_long_horizon_and_tight_budget():
    params = request([0.1, 1] * 336)
    battery = params.battery_entities[0]
    params = params.model_copy(
        update={
            "battery_entities": [
                battery.model_copy(update={"name": f"battery-{index}"})
                for index in range(12)
            ]
        }
    )
    result = assert_conforms(PATH, params, {"max_evaluations": 3})
    assert len(result["entities"]) == 12
    assert result["diagnostics"]["evaluations"] == 3


def test_trade_release_uses_load_instead_of_high_price_pv_slot():
    result = assert_conforms(
        PATH, request([0.1, 0.5, 10, 0], usage=[0, 1, 1, 0], pv=[0, 0, 1, 0])
    )
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert result["entities"][0]["schedule"][1]["state"] == "self_consume"


def test_two_cheap_charges_supply_two_peak_slots():
    result = assert_conforms(
        PATH, request([0.1, 0.2, 0.8, 0.9], usage=[0, 0, 1, 1], capacity=2)
    )
    assert result["projections"]["projected_cost"] == pytest.approx(0.3)


def test_pv_opportunity_releases_energy_before_existing_supplied_peak():
    result = assert_conforms(
        PATH,
        request(
            [0.1, 0.1, 0.5, 0.8],
            usage=[0, 0, 1, 1],
            pv=[0, 2, 0, 0],
            export=[0.1] * 4,
            capacity=2,
        ),
    )
    assert result["projections"]["projected_cost"] == pytest.approx(0)


def test_plain_charge_preserves_at_most_deadline_discharge():
    result = assert_conforms(
        PATH,
        request(
            [-0.2, -0.1, 0.3, 0.5],
            usage=[0, 0.1, 0.8, 0.8],
            capacity=2,
            target={"timeslot": 2, "soc_kwh": 0.3, "mode": "at_most"},
        ),
    )
    assert result["diagnostics"]["target_miss_kwh"] == 0
    assert result["projections"]["projected_cost"] == pytest.approx(0.09)


@pytest.mark.parametrize("budget", [1, 8, 256])
def test_first_candidate_recovers_reserve_before_cheaper_future_power(budget):
    params = request([10, 1, 0, 0], usage=[0] * 4)
    params.battery_entities[0].minimum_kwh = 2.0
    result = factory(max_evaluations=budget)(params)
    assert [slot["state"] for slot in result["entities"][0]["schedule"]][:2] == ["grid_charge", "grid_charge"]
    assert result["diagnostics"]["evaluations"] <= budget
    assert result["diagnostics"]["evaluation"]["reserve_recovery_penalty"] == 5000.0
    assert result["suboptimal_reasons"] == ["battery_min_unmet"]


def test_pv_only_recovery_remains_physical_with_single_evaluation():
    params = request([10, 1, 0, 0], usage=[0] * 4, pv=[0, 1, 0, 0], ingress=2)
    params.battery_entities[0].minimum_kwh = 1.0
    result = factory(max_evaluations=1)(params)
    schedule = result["entities"][0]["schedule"]
    assert all(slot["state"] != "grid_charge" for slot in schedule)
    assert [slot["level"] for slot in schedule] == [0.0, 1.0, 1.0, 1.0]
    assert result["projections"]["projected_cost"] == 0.0
    assert result["diagnostics"]["evaluations"] == 1


@pytest.mark.parametrize("mode", ["at_least", "exact"])
def test_small_budget_honors_early_target_despite_later_cheaper_charge(mode):
    params = request([10, 1, 0, 0], usage=[0] * 4,
                     target={"timeslot": 0, "soc_kwh": 1.0, "mode": mode})
    result = factory(max_evaluations=8)(params)
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert result["diagnostics"]["target_miss_kwh"] == 0.0
    assert not result["suboptimal"]


def test_small_budget_prioritizes_three_charge_target_over_tariff_passes():
    params = request([10, 9, 8, 0], usage=[0] * 4,
                     target={"timeslot": 2, "soc_kwh": 3.0, "mode": "exact"})
    result = factory(max_evaluations=8)(params)
    assert result["entities"][0]["schedule"][2]["level"] == 3.0
    assert result["diagnostics"]["target_miss_kwh"] == 0.0
    assert result["diagnostics"]["evaluations"] <= 8


def test_small_budget_honors_upper_target_even_at_negative_price():
    params = request([-10, -1, 0, 0], usage=[1, 0, 0, 0], level=2,
                     target={"timeslot": 0, "soc_kwh": 1.0, "mode": "at_most"})
    result = factory(max_evaluations=8)(params)
    assert result["entities"][0]["schedule"][0]["state"] == "self_consume"
    assert result["diagnostics"]["target_miss_kwh"] == 0.0


def test_exact_discrete_miss_reports_health_without_infeasibility_claim():
    params = request([1] * 4, usage=[0] * 4,
                     target={"timeslot": 0, "soc_kwh": 0.4, "mode": "exact"})
    result = factory(max_evaluations=8)(params)
    assert result["suboptimal_reasons"] == ["battery_target_unmet"]
    assert result["diagnostics"]["evaluation"]["constraint_status"] == "observed_plan_violations_not_infeasibility_proof"
