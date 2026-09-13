import numpy as np
import pytest

pytest.importorskip("highspy")

from custom_components.wattplan.optimizer import mpc_power_optimizer as optimizer
from custom_components.wattplan.optimizer.models import _parse_state_blob


def _representative_payload():
    return {
        "grid_import_price_per_kwh": [
            -0.20, 0.08, 0.15, 0.55, 0.90, 0.30,
            -0.05, 0.40, 0.75, 0.12, 0.60, 0.25,
        ],
        "grid_export_price_per_kwh": [
            0.04, -0.08, 0.02, 0.20, 0.35, 0.05,
            -0.03, 0.10, 0.25, 0.01, 0.18, 0.04,
        ],
        "solar_input_kwh": [0.0, 0.8, 1.5, 0.2, 0.0, 1.2, 0.4, 0.0, 0.0, 1.0, 0.1, 0.0],
        "usage_kwh": [0.4, 0.3, 0.5, 1.1, 1.3, 0.4, 0.8, 1.0, 1.2, 0.3, 1.0, 0.6],
        "lookahead_slots": 8,
        "infer_battery_preserve_policy": False,
        "action_deadband_kwh": 0.05,
        "throughput_cost_per_kwh": 0.003,
        "battery_entities": [
            {
                "name": "house",
                "initial_kwh": 0.7,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "target": {
                    "timeslot": 7,
                    "soc_kwh": 1.1,
                    "mode": "at_least",
                    "tolerance_kwh": 0.02,
                },
                "charge_curve_kwh": [0.9, 0.7, 0.25],
                "discharge_curve_kwh": [0.8, 0.65, 0.3],
                "charge_efficiency": 0.94,
                "discharge_efficiency": 0.91,
                "can_charge_from": 3,
            },
            {
                "name": "vehicle",
                "initial_kwh": 1.2,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.6,
                "target": {
                    "timeslot": 8,
                    "soc_kwh": 0.8,
                    "mode": "at_most",
                    "tolerance_kwh": 0.02,
                },
                "charge_curve_kwh": [0.6, 0.5, 0.2],
                "discharge_curve_kwh": [0.7, 0.55, 0.25],
                "charge_efficiency": 0.96,
                "discharge_efficiency": 0.90,
                "can_charge_from": 1,
            },
        ],
        "comfort_entities": [],
    }


def _run(payload):
    params = optimizer.OptimizationParams(**payload)
    normalized = optimizer.normalize_calculation_input(params)
    return optimizer.optimize_internal(normalized, reuse_plan_override=None)


def _assert_physical_plan(payload, result):
    state = _parse_state_blob(result["state"])
    for battery_index, battery in enumerate(payload["battery_entities"]):
        levels = state.battery_levels[battery_index]
        assert np.all(levels >= float(battery["minimum_kwh"]) - 1e-6)
        assert np.all(levels <= float(battery["capacity_kwh"]) + 1e-6)

        deadband = float(payload["action_deadband_kwh"])
        charge = state.battery_charge[battery_index]
        discharge = state.battery_discharge[battery_index]
        assert np.all((charge <= 1e-6) | (charge >= deadband - 1e-6))
        assert np.all((discharge <= 1e-6) | (discharge >= deadband - 1e-6))

    schedules = {entity["name"]: entity["schedule"] for entity in result["entities"]}
    assert schedules["house"][7]["level"] >= 1.08 - 1e-6
    assert schedules["vehicle"][8]["level"] <= 0.82 + 1e-6
    assert np.isfinite(result["projections"]["projected_cost"])


def test_full_plan_warm_starts_match_cold_optimal_cost_and_constraints(monkeypatch):
    payload = _representative_payload()
    with monkeypatch.context() as cold_patch:
        cold_patch.setattr(
            optimizer, "_use_mip_starts", lambda _entities, _lookahead: False
        )
        cold = _run(payload)

    submitted_starts = []
    original = optimizer._solve_lp

    def capture(*args, **kwargs):
        submitted_starts.append(kwargs.get("mip_start"))
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_solve_lp", capture)
    monkeypatch.setattr(
        optimizer, "_use_mip_starts", lambda _entities, _lookahead: True
    )
    warm = _run(payload)

    assert any(start is not None for start in submitted_starts)
    assert warm["successful_solves"] == cold["successful_solves"] == 12
    assert warm["suboptimal_reasons"] == cold["suboptimal_reasons"] == []
    assert warm["projections"]["projected_cost"] == pytest.approx(
        cold["projections"]["projected_cost"], abs=1e-7
    )
    _assert_physical_plan(payload, cold)
    _assert_physical_plan(payload, warm)


def test_preserve_probes_stay_cold_and_do_not_replace_primary_hint(monkeypatch):
    payload = {
        "grid_import_price_per_kwh": [0.10, 1.00, 1.00, 1.00],
        "grid_export_price_per_kwh": [0.0] * 4,
        "solar_input_kwh": [0.0] * 4,
        "usage_kwh": [1.0] * 4,
        "lookahead_slots": 4,
        "action_deadband_kwh": 0.005,
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 0,
            }
        ],
        "comfort_entities": [],
    }
    events = []
    original = optimizer._solve_mpc_step

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        events.append(
            {
                "probe": kwargs.get("forced_discharge_first") is not None,
                "input": kwargs.get("mip_start"),
                "output": None if result is None else result.get("mip_start"),
            }
        )
        return result

    monkeypatch.setattr(optimizer, "_solve_mpc_step", capture)
    monkeypatch.setattr(
        optimizer, "_use_mip_starts", lambda _entities, _lookahead: True
    )
    result = _run(payload)

    probes = [event for event in events if event["probe"]]
    primaries = [event for event in events if not event["probe"]]
    assert result["entities"][0]["schedule"][0]["state"] == "preserve"
    assert probes
    assert all(event["input"] is None for event in probes)
    assert primaries[0]["input"] is None
    for previous, current in zip(primaries, primaries[1:]):
        assert current["input"] is previous["output"]
        assert all(current["input"] is not probe["output"] for probe in probes)


def test_mip_start_extraction_only_runs_for_consuming_primary_solves(monkeypatch):
    payload = {
        "grid_import_price_per_kwh": [0.10, 1.00, 1.00, 1.00],
        "grid_export_price_per_kwh": [0.0] * 4,
        "solar_input_kwh": [0.0] * 4,
        "usage_kwh": [1.0] * 4,
        "lookahead_slots": 4,
        "action_deadband_kwh": 0.005,
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 0,
            }
        ],
        "comfort_entities": [],
    }
    extractions = 0
    original = optimizer._extract_mip_start

    def capture(*args, **kwargs):
        nonlocal extractions
        extractions += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_extract_mip_start", capture)
    with monkeypatch.context() as warm_patch:
        warm_patch.setattr(
            optimizer, "_use_mip_starts", lambda _entities, _lookahead: True
        )
        result = _run(payload)

    assert result["successful_solves"] == 4
    assert extractions == result["successful_solves"]

    payload["action_deadband_kwh"] = 0.0
    extractions = 0
    _run(payload)
    assert extractions == 0


def test_mip_start_eligibility_requires_deadband_and_long_effective_lookahead(
    monkeypatch,
):
    payload = _representative_payload()
    entities = optimizer.normalize_calculation_input(
        optimizer.OptimizationParams(**payload)
    ).battery_entities

    assert not optimizer._use_mip_starts(entities, 39)
    assert optimizer._use_mip_starts(entities, 40)

    payload["lookahead_slots"] = 40
    starts = []
    original = optimizer._solve_lp

    def capture(*args, **kwargs):
        starts.append(kwargs.get("mip_start"))
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_solve_lp", capture)
    _run(payload)
    assert not any(start is not None for start in starts)

    payload["action_deadband_kwh"] = 0.0
    entities = optimizer.normalize_calculation_input(
        optimizer.OptimizationParams(**payload)
    ).battery_entities
    assert not optimizer._use_mip_starts(entities, 48)
