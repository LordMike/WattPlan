"""Executable beam trajectories, objective agreement and bounded deterministic work."""

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.beam_search import factory
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from tests.optimizer.planner_conformance import assert_conforms, conformance_params

PATH = "custom_components.wattplan.optimizer.planners.beam_search:factory"


def request(*, prices=(0.1, 1.0), usage=(0.0, 1.0), initial=0.0,
            target=None, pv=None, ingress=3, batteries=1):
    padding = max(4 - len(prices), 0)
    prices = (*prices, *([0.0] * padding))
    usage = (*usage, *([0.0] * padding))
    pv = (*pv, *([0.0] * padding)) if pv is not None else None
    return OptimizationParams(
        plan_start="2026-06-15T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=list(prices),
        grid_export_price_per_kwh=[0.0] * len(prices),
        usage_kwh=list(usage), solar_input_kwh=list(pv or [0.0] * len(prices)),
        comfort_entities=[],
        battery_entities=[dict(name=f"battery-{i}", initial_kwh=initial,
                               minimum_kwh=0.0, capacity_kwh=3.0,
                               charge_curve_kwh=[1.0], discharge_curve_kwh=[1.0],
                               can_charge_from=ingress, target=target)
                          for i in range(batteries)],
    )


def policies(result):
    return [[a["state"] for a in b["schedule"]] for b in result["entities"]]


def test_forecast_guidance_keeps_early_cheap_charge_at_width_one():
    params = request()
    result = factory(beam_width=1)(params)
    assert policies(result)[0][:2] == ["grid_charge", "self_consume"]
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert factory(beam_width=1, guidance_scale=0)(params)["projections"]["projected_cost"] == pytest.approx(1.0)


def test_exhausted_tail_recovers_reserve_before_distant_target():
    params = request(
        prices=(10, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01),
        usage=(0,) * 8, ingress=1,
        target={"timeslot": 7, "soc_kwh": 1.2, "mode": "at_least"},
    )
    params.battery_entities[0].minimum_kwh = 1.0
    params.battery_entities[0].charge_curve_kwh = [0.4]
    actual = factory(max_expansions=1)(params)
    assert actual["diagnostics"]["tail_transitions"] == 7
    assert policies(actual)[0][:3] == ["grid_charge"] * 3
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]][:3] == pytest.approx([0.4, 0.8, 1.2])
    assert actual["diagnostics"]["evaluation"]["score"][0] == 0
    assert actual["diagnostics"]["evaluation"]["score"] == pytest.approx(
        evaluate_schedule(params, policies(actual)).score
    )


@pytest.mark.parametrize("mode,initial,level,expected", [
    ("at_least", 0.0, 2.0, 0.0),
    ("at_most", 2.0, 0.0, 0.0),
    ("exact", 0.0, 1.0, 0.0),
    ("exact", 0.0, 3.0, 1.0),
])
def test_deadlines_including_infeasible(mode, initial, level, expected):
    params = request(initial=initial, usage=(1.0, 1.0),
                     target=dict(timeslot=1, soc_kwh=level, mode=mode))
    result = factory()(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == pytest.approx(expected)


def test_pv_only_and_negative_prices_are_physical():
    params = request(prices=(-0.3, 1.0), pv=(1.0, 0.0), ingress=2)
    result = factory()(params)
    assert "grid_charge" not in policies(result)[0]
    assert result["projections"]["projected_cost"] == pytest.approx(0.0)
    negative = factory()(request(prices=(-0.3, 0.1), usage=(0.0, 0.0)))
    assert negative["projections"]["projected_cost"] == pytest.approx(-0.3)
    assert negative["diagnostics"]["evaluation"]["terminal_energy_credit"] == 0.0


def test_tiny_budget_completes_all_batteries_and_horizon_deterministically():
    params = request(prices=(0.1,) * 672, usage=(0.5,) * 672, batteries=12)
    plan = factory(beam_width=2, max_expansions=1, max_joint_actions=3, guidance_slots=2)
    before = params.model_dump()
    a, b = plan(params), plan(params)
    assert a["entities"] == b["entities"]
    assert a["projections"] == b["projections"]
    assert params.model_dump() == before
    assert len(a["entities"]) == 12
    assert all(len(entity["schedule"]) == 672 for entity in a["entities"])
    diag = a["diagnostics"]
    assert diag["expansions"] == 1
    assert diag["evaluations"] == 1
    assert diag["tail_transitions"] == 671
    assert diag["termination_reason"] == "evaluation_limit"
    assert diag["joint_actions_pruned_per_node"] == 3 ** 12 - 1


def test_deadline_is_cooperative_and_mandatory_tail_is_counted(monkeypatch):
    from custom_components.wattplan.optimizer.planners import budget
    monkeypatch.setattr(budget.time, "perf_counter", lambda: 1e100)
    result = factory(max_seconds=1.0)(request())
    assert result["diagnostics"]["termination_reason"] == "search_time_limit"
    assert result["diagnostics"]["expansions"] == 0
    assert result["diagnostics"]["tail_transitions"] == 4
    assert len(result["entities"][0]["schedule"]) == 4


def test_preference_accumulation_and_every_projected_level_agree():
    params = conformance_params()[2].model_copy(update={
        "throughput_cost_per_kwh": 0.3, "mode_switch_cost": 0.2,
        "action_deadband_kwh": 0.1,
    })
    result = factory()(params)
    evaluation = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluation.score)
    for index, entity in enumerate(result["entities"]):
        assert [a["level"] for a in entity["schedule"]] == pytest.approx(
            [step.levels[index] for step in evaluation.steps])


@pytest.mark.parametrize("option,value", [
    ("beam_width", 0), ("beam_width", True), ("max_expansions", -1),
    ("max_joint_actions", 0), ("guidance_slots", 1.5),
    ("guidance_scale", float("nan")), ("guidance_scale", -1),
    ("max_seconds", 0), ("max_seconds", float("inf")),
])
def test_invalid_options(option, value):
    with pytest.raises(ValueError):
        factory(**{option: value})


def test_nondischargeable_energy_has_no_household_value():
    from custom_components.wattplan.optimizer.planners.beam_search import _Node, _guidance, _rank
    params = request(prices=(0.1, 1.0), usage=(0.0, 1.0))
    params.battery_entities[0].discharge_curve_kwh = [0.0]
    guidance = _guidance(params, 24)
    empty = _rank(params, _Node((0.0,), (0,)), 0, guidance, 1.0)
    full = _rank(params, _Node((3.0,), (0,)), 0, guidance, 1.0)
    assert empty == full
    assert "grid_charge" not in policies(factory(beam_width=1)(params))[0]


def test_action_preparation_is_bounded_by_effective_work_budget():
    params = request(batteries=20)
    result = factory(max_expansions=1, max_joint_actions=10**9)(params)
    assert result["diagnostics"]["joint_actions_considered"] == 1
    assert result["diagnostics"]["tail_transitions"] == 3


def test_soc_curves_below_reserve_and_tolerance():
    params = conformance_params()[2]
    params.battery_entities[0].charge_curve_kwh = [1.0, 0.8, 0.2]
    params.battery_entities[0].discharge_curve_kwh = [0.2, 0.6, 1.0]
    params.battery_entities[0].target.mode = "exact"
    params.battery_entities[0].target.tolerance_kwh = 0.2
    result = assert_conforms(PATH, params)
    evaluation = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluation.score)
    assert result["diagnostics"]["evaluation"]["reserve_shortfalls"][0]["initial_shortfall_kwh"] == pytest.approx(0.1)


@pytest.mark.parametrize("expansions", [1, 30_000])
def test_reserve_recovery_starts_immediately_even_with_one_expansion(expansions):
    params = request(prices=(1.0, 0.0, 0.0, 0.0), usage=(0.0,) * 4)
    params.battery_entities[0].minimum_kwh = 2.0
    result = factory(max_expansions=expansions)(params)
    assert policies(result)[0][:2] == ["grid_charge", "grid_charge"]
    assert result["entities"][0]["schedule"][0]["level"] == 1.0
    assert result["diagnostics"]["evaluation"]["reserve_recovery_penalty"] == 5000.0
    assert result["suboptimal_reasons"] == ["battery_min_unmet"]
    # A real first-slot shortfall is reported, without claiming infeasibility.
    assert result["diagnostics"]["evaluation"]["constraint_status"] == "observed_plan_violations_not_infeasibility_proof"


def test_pv_only_reserve_recovery_never_invents_grid_ingress():
    params = request(ingress=2, prices=(1.0,) * 4, usage=(0.0,) * 4,
                     pv=(0.0, 1.0, 0.0, 0.0))
    params.battery_entities[0].minimum_kwh = 1.0
    result = factory(max_expansions=1)(params)
    assert "grid_charge" not in policies(result)[0]
    assert [slot["level"] for slot in result["entities"][0]["schedule"]] == [0.0, 1.0, 1.0, 1.0]
    assert result["projections"]["projected_cost"] == 0.0
    assert result["suboptimal_reasons"] == ["battery_min_unmet"]


def test_arbitrary_prefix_scoring_matches_shared_evaluator_with_reserve_deficit():
    from custom_components.wattplan.optimizer.planners.beam_search import _Node, _advance, _objective

    params = request(initial=0.0, prices=(0.3,) * 4, usage=(0.2,) * 4)
    params.battery_entities[0].minimum_kwh = 1.5
    params.battery_entities[0].charge_efficiency = 0.8
    params.mode_switch_cost = 0.2
    params.throughput_cost_per_kwh = 0.1
    schedule = ["preserve", "grid_charge", "grid_charge", "self_consume"]
    node = _Node((0.0,), (0,))
    for slot, mode in enumerate(schedule):
        node = _advance(params, node, (mode,), slot)
        prefix = params.model_copy(update={
            name: getattr(params, name)[:slot + 1]
            for name in ("usage_kwh", "solar_input_kwh", "grid_import_price_per_kwh", "grid_export_price_per_kwh")
        })
        evaluation = evaluate_schedule(prefix, [schedule[:slot + 1]])
        assert _objective(params, node) == pytest.approx(evaluation.objective)
        assert node.levels == pytest.approx(evaluation.steps[-1].levels)


def test_target_miss_is_visible_in_ha_health():
    params = request(target=dict(timeslot=0, soc_kwh=3.0, mode="at_least"))
    result = factory()(params)
    assert result["suboptimal"]
    assert "battery_target_unmet" in result["problems"]
