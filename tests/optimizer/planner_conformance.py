"""Common fixtures and assertions for the registry-wide planner test suites."""

import json
import math

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import (
    create_planner,
    validate_battery_result,
)
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.registry import BUILTINS, resolve_factory


def economic_params(*, prices=(0.1, 1.0), usage=(0, 1), solar=None, initial=0, target=None):
    """Small hand-checkable tariff/target case, padded to the minimum horizon."""
    extra = max(0, 4 - len(prices))
    return OptimizationParams(
        plan_start="2026-06-15T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=[*prices, *([max(prices)] * extra)],
        grid_export_price_per_kwh=[0] * (len(prices) + extra),
        usage_kwh=[*usage, *([0] * extra)],
        solar_input_kwh=[*(solar or [0] * len(prices)), *([0] * extra)],
        comfort_entities=[],
        battery_entities=[{
            "name": "house", "initial_kwh": initial, "minimum_kwh": 0,
            "capacity_kwh": 3, "charge_curve_kwh": [1],
            "discharge_curve_kwh": [1], "can_charge_from": 3, "target": target,
        }],
    )


def battery_policies(result):
    return [[slot["state"] for slot in entity["schedule"]]
            for entity in result["entities"] if entity["type"] == "battery"]


def conformance_params() -> list[OptimizationParams]:
    battery = {
        "name": "house",
        "initial_kwh": 1.0,
        "minimum_kwh": 0.2,
        "capacity_kwh": 3.0,
        "charge_curve_kwh": [1.0],
        "discharge_curve_kwh": [1.0],
        "can_charge_from": 3,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.8,
    }
    common = {
        "plan_start": "2026-06-15T00:00:00+00:00",
        "slot_minutes": 15,
        "grid_import_price_per_kwh": [-0.2, 0.1, 0.8, 0.4],
        "grid_export_price_per_kwh": [0.0, -0.1, 0.2, 0.1],
        "usage_kwh": [0.3, 0.3, 1.0, 0.4],
        "solar_input_kwh": [0.0, 1.5, 0.0, 0.2],
        "comfort_entities": [],
    }
    return [
        OptimizationParams(**common, battery_entities=[]),
        OptimizationParams(**common, battery_entities=[battery]),
        OptimizationParams(
            **common,
            battery_entities=[
                {
                    **battery,
                    "initial_kwh": 0.1,
                    "target": {"timeslot": 2, "soc_kwh": 2.0},
                },
                {**battery, "name": "pv-only", "can_charge_from": 2},
            ],
        ),
    ]


def assert_public_result(params: OptimizationParams, result: dict) -> None:
    """Validate published schedules and accounting without choosing an algorithm."""
    required = {
        "entities", "state", "projections", "diagnostics", "energy_flows",
        "optional_entity_options", "fitness", "avg_price", "execution_time",
        "suboptimal", "overconstrained", "suboptimal_reasons", "problems",
    }
    assert isinstance(result, dict) and required <= result.keys()
    json.dumps(result, allow_nan=False)
    for key in ("fitness", "avg_price", "execution_time"):
        assert type(result[key]) in (int, float) and math.isfinite(result[key])
    assert result["execution_time"] >= 0
    for key in ("suboptimal", "overconstrained"):
        assert isinstance(result[key], bool)
    for key in ("suboptimal_reasons", "problems"):
        assert isinstance(result[key], list)
        assert all(isinstance(reason, str) for reason in result[key])
    assert isinstance(result["diagnostics"], dict)
    OptimizationParams._validate_state_blob(result["state"])

    horizon = len(params.usage_kwh)
    entities = result["entities"]
    assert isinstance(entities, list)
    expected = {
        **{("battery", b.name): b for b in params.battery_entities},
        **{("comfort", c.name): c for c in params.comfort_entities},
    }
    assert len(entities) == len(expected)
    assert {(e["type"], e["name"]) for e in entities} == set(expected)
    assert [e["name"] for e in entities if e["type"] == "battery"] == [
        b.name for b in params.battery_entities
    ]
    comfort_load = [0.0] * horizon
    for entity in entities:
        source = expected[entity["type"], entity["name"]]
        assert isinstance(entity["schedule"], list)
        assert len(entity["schedule"]) == horizon
        for index, point in enumerate(entity["schedule"]):
            assert type(point["level"]) in (int, float) and math.isfinite(point["level"])
            if entity["type"] == "battery":
                assert point["state"] in {"self_consume", "preserve", "grid_charge"}
                assert point["state"] != "grid_charge" or source.can_charge_from & 1
                assert -1e-6 <= point["level"] <= source.capacity_kwh + 1e-6
            else:
                assert isinstance(point["enabled"], bool)
                comfort_load[index] += source.power_usage_kwh * point["enabled"]

    flows = result["energy_flows"]["per_slot"]
    projections = result["projections"]
    rows = projections["per_slot"]
    assert isinstance(flows, list) and isinstance(rows, list)
    assert len(flows) == len(rows) == horizon
    for index, (flow, row) in enumerate(zip(flows, rows, strict=True)):
        for key in ("load_kwh", "base_load_kwh", "comfort_load_kwh",
                    "grid_import_kwh", "grid_export_kwh", "grid_charge_kwh"):
            assert type(flow[key]) in (int, float) and flow[key] >= -1e-6
        assert flow["base_load_kwh"] == pytest.approx(params.usage_kwh[index])
        assert flow["comfort_load_kwh"] == pytest.approx(comfort_load[index])
        imported, exported = flow["grid_import_kwh"], flow["grid_export_kwh"]
        assert min(imported, exported) <= 1e-6
        assert flow["grid_charge_kwh"] <= imported + 1e-6
        assert imported - exported == pytest.approx(
            flow["load_kwh"] - params.solar_input_kwh[index], abs=1e-6
        )
        buy, sell = params.grid_import_price_per_kwh[index], params.grid_export_price_per_kwh[index]
        net = params.usage_kwh[index] - params.solar_input_kwh[index]
        assert row["baseline_cost"] == pytest.approx(max(net, 0) * buy - max(-net, 0) * sell)
        assert row["projected_cost"] == pytest.approx(imported * buy - exported * sell, abs=1e-6)
        assert row["projected_savings_cost"] == pytest.approx(row["baseline_cost"] - row["projected_cost"])
    for key in ("baseline_cost", "projected_cost", "projected_savings_cost"):
        assert projections[key] == pytest.approx(sum(row[key] for row in rows), abs=1e-6)
    options = result["optional_entity_options"]
    assert isinstance(options, list)
    assert [entity["name"] for entity in options] == [entity.name for entity in params.optional_entities]


def assert_conforms(factory: str, params: OptimizationParams, options=None) -> dict:
    """Check complete serializable output and physically executable projections.

    Targets may be infeasible; this helper does not demand a particular policy or
    impose the experimental objective on HiGHS. It compares physics for emitted
    policies independently and verifies input isolation in callers' test fixtures.
    """
    before = params.model_dump(mode="json")
    result = create_planner(factory, options)(params)
    assert params.model_dump(mode="json") == before, "Planner mutated its request"
    assert_public_result(params, result)
    schedules = validate_battery_result(result, params)
    policies = [
        [a["state"] for a in schedules[b.name]] for b in params.battery_entities
    ]
    evaluation = evaluate_schedule(params, policies)
    for step in evaluation.steps:
        assert step.grid_import >= 0
        assert step.grid_export >= 0
        for battery, level in zip(params.battery_entities, step.levels, strict=True):
            assert 0 <= level <= battery.capacity_kwh + 1e-6
    # Candidate build_result uses policy projections. HiGHS retains its original
    # continuous-control projections, so only require this from the shared builder.
    if resolve_factory(factory) != BUILTINS["highs"]:
        assert result["projections"]["projected_cost"] == pytest.approx(
            evaluation.tariff_cost
        )
        assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluation.score)
        assert result["diagnostics"]["evaluation"]["terminal_energy_credit"] == 0
        assert result["state"] is None  # Battery searches do not carry opaque state.
        for index, battery in enumerate(params.battery_entities):
            assert [slot["level"] for slot in schedules[battery.name]] == [
                step.levels[index] for step in evaluation.steps
            ]
        assert result["projections"]["final_soc_kwh"] == pytest.approx({
            battery.name: schedules[battery.name][-1]["level"]
            for battery in params.battery_entities
        })
    return result


def battery_params(*, prices=(0.1, 1.0, 0.0, 0.0), usage=(0.0, 1.0, 0.0, 0.0),
            initial=0.0, target=None, pv=None, ingress=3, batteries=1):
    return OptimizationParams(
        plan_start="2026-06-15T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=list(prices), grid_export_price_per_kwh=[0.0] * len(prices),
        usage_kwh=list(usage), solar_input_kwh=list(pv or [0.0] * len(prices)),
        comfort_entities=[], battery_entities=[dict(
            name=f"battery-{i}", initial_kwh=initial, minimum_kwh=0.0, capacity_kwh=3.0,
            charge_curve_kwh=[1.0], discharge_curve_kwh=[1.0], can_charge_from=ingress,
            target=target,
        ) for i in range(batteries)],
    )
