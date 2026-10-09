"""Independent physics checks and causal planner simulation regressions."""

import json
import time
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.wattplan.optimizer.models import (
    BatteryEntityParams,
    OptimizationParams,
    encode_state_blob,
)
from custom_components.wattplan.optimizer.simulation.adapters import (
    BASELINE_PLANNER,
    self_consumption_factory,
)
from custom_components.wattplan.optimizer.simulation.dataset import (
    Actual,
    Dataset,
    Event,
    load_dataset,
)
from custom_components.wattplan.optimizer.simulation.engine import Variant, simulate
from custom_components.wattplan.optimizer.simulation.physics import transition
from custom_components.wattplan.optimizer.simulation.reporting import (
    comparisons,
    write_reports,
)
from custom_components.wattplan.optimizer.simulation.scenarios import synthetic_dataset
from custom_components.wattplan.optimizer.simulation.worker import PlannerWorker

START = datetime(2026, 9, 20, tzinfo=UTC)


def payload():
    return {
        "plan_start": START.isoformat(),
        "slot_minutes": 60,
        "grid_import_price_per_kwh": [2.0] * 4,
        "grid_export_price_per_kwh": [0.5] * 4,
        "usage_kwh": [2.0] * 4,
        "solar_input_kwh": [0.0] * 4,
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 2.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [2.0],
                "can_charge_from": 3,
            }
        ],
        "comfort_entities": [],
    }


def actual(start=START, *, usage=0.5, pv=0.0, hours=1, **changes):
    end = start + timedelta(hours=hours)
    return Actual(
        start,
        end,
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "usage_kwh": usage,
            "pv_kwh": pv,
            "import_price": 2.0,
            "export_price": 0.5,
            "quality": [],
            **changes,
        },
    )


def event(at=START, *, captured=None, **changes):
    p = {**payload(), **changes, "plan_start": at.isoformat()}
    return Event(
        captured or at, at, OptimizationParams(**p).model_dump(mode="json"), False
    )


def dataset(events=None, actuals=None):
    return Dataset(
        "home",
        "DKK",
        "UTC",
        events or [event(), event(START + timedelta(hours=1))],
        actuals or [actual(), actual(START + timedelta(hours=1))],
        [],
        [],
    )


class LocalPlanner:
    """Test adapter exercises the same params/result API without subprocess cost."""

    def __init__(self, mode="self_consume"):
        self.calls = []
        self.mode = mode

    def call(self, p, *, reset, timeout):
        self.calls.append((deepcopy(p), reset, timeout))
        return {
            "result": {
                "entities": [
                    {
                        "type": "battery",
                        "name": b["name"],
                        "schedule": [{"state": self.mode, "level": 4.0}]
                        * len(p["usage_kwh"]),
                    }
                    for b in p["battery_entities"]
                ],
                "state": encode_state_blob({"v": 1, "call": len(self.calls)}),
            },
            "wall_seconds": 0.1,
            "cpu_seconds": 0.05,
        }


def test_causal_forecasts_and_simulated_soc_replace_only_runtime_state():
    next_at = START + timedelta(hours=1)
    archived_state = encode_state_blob({"v": 1, "real_world": True})
    data = dataset(
        events=[event(state=archived_state), event(next_at, state=archived_state)]
    )
    planner = LocalPlanner()
    report = simulate(data, Variant("test"), planner)
    assert report["status"] == "complete"
    assert planner.calls[0][0]["state"] is None
    second = planner.calls[1][0]
    assert second["usage_kwh"] == [2.0] * 4  # actuals are .5, not 2
    assert second["battery_entities"][0]["initial_kwh"] == 1.5
    assert second["state"] == encode_state_blob({"v": 1, "call": 1})
    assert second["state"] != archived_state
    assert report["segments"][0]["final_soc_kwh"] == {"battery": 1.0}
    assert report["totals"]["grid_import_kwh"] == 0
    assert (
        data.events[1].params["battery_entities"][0]["initial_kwh"] == 2
    )  # no mutation


def test_efficiencies_and_energy_conservation_are_hand_calculated():
    b = BatteryEntityParams(
        **{
            **payload()["battery_entities"][0],
            "initial_kwh": 0,
            "charge_efficiency": 0.8,
            "discharge_efficiency": 0.5,
        }
    )
    charged = transition([b], [0], ["preserve"], usage=1, pv=3)
    assert charged.levels == pytest.approx([1.6])
    assert charged.loss == pytest.approx(0.4)
    assert charged.grid_export == 0
    discharged = transition([b], charged.levels, ["self_consume"], usage=1, pv=0)
    assert discharged.levels == pytest.approx([0])
    assert discharged.grid_import == pytest.approx(0.2)
    assert discharged.loss == pytest.approx(0.8)
    # AC input + stored decrease = served load + exports + conversion losses.
    assert 3 + 0.2 == pytest.approx(2 + charged.loss + discharged.loss)


def test_allocation_ingress_and_below_reserve_do_not_create_energy():
    b = BatteryEntityParams(**{**payload()["battery_entities"][0], "minimum_kwh": 1})
    step = transition([b], [0.5], ["self_consume"], usage=2, pv=0)
    assert step.levels == [0.5]
    assert step.grid_import == 2
    first = b.model_copy(update={"name": "first", "minimum_kwh": 0})
    second = first.model_copy(update={"name": "second", "discharge_curve_kwh": [1.0]})
    step = transition([first, second], [2, 2], ["self_consume"] * 2, usage=1.5, pv=0)
    assert step.discharge == pytest.approx([1, 0.5])
    sequential = transition(
        [first, second],
        [2, 2],
        ["self_consume"] * 2,
        usage=1.5,
        pv=0,
        allocation="sequential",
    )
    assert sequential.discharge == pytest.approx([1.5, 0])
    step = transition([first, second], [0, 0], ["preserve"] * 2, usage=0, pv=3)
    assert step.pv_charge == [2, 1]
    no_grid = first.model_copy(update={"can_charge_from": 2})
    assert transition([no_grid], [0], ["grid_charge"], usage=0, pv=0).charge == 0


def test_fractional_action_latches_full_slot_headroom_and_signed_tariffs():
    p = payload()
    p["battery_entities"][0].update(initial_kwh=3, charge_curve_kwh=[2])
    a = [
        actual(START + timedelta(minutes=15 * i), usage=0, hours=0.25, import_price=-2)
        for i in range(4)
    ]
    report = simulate(
        dataset([event(**p)], a), Variant("charge"), LocalPlanner("grid_charge")
    )
    assert report["status"] == "complete"
    assert (
        report["totals"]["charge_kwh"] == 1
    )  # .5 + .5; a rechecked headroom would stop at .5
    assert report["totals"]["net_cost"] == -2
    assert report["segments"][0]["final_soc_kwh"] == {"battery": 4}
    p["battery_entities"][0]["initial_kwh"] = 3.1
    report = simulate(
        dataset([event(**p)], a), Variant("charge"), LocalPlanner("grid_charge")
    )
    assert report["totals"]["charge_kwh"] == 0  # .9 headroom < half a full 2kWh slot


def test_replanning_inside_an_actual_interval_is_causal_and_scales_energy():
    half = START + timedelta(minutes=30)
    planner = LocalPlanner("preserve")
    report = simulate(
        dataset([event(), event(captured=half), event(START + timedelta(hours=1))]),
        Variant("preserve"),
        planner,
    )
    assert report["status"] == "complete"
    assert report["totals"]["usage_kwh"] == 1
    assert [row["usage_kwh"] for row in report["intervals"]] == [0.25, 0.25, 0.5]
    assert planner.calls[1][0]["battery_entities"][0]["initial_kwh"] == 2


def test_gaps_and_equipment_changes_reset_all_variants_from_observed_soc():
    times = [START + timedelta(hours=i) for i in range(4)]
    gap = actual(times[1], usage=None, quality=["missing_usage_kwh"])
    hardware = deepcopy(payload()["battery_entities"])
    hardware[0]["capacity_kwh"] = 5
    data = dataset(
        [
            event(times[0]),
            event(times[1]),
            event(times[2]),
            event(times[3], battery_entities=hardware),
        ],
        [actual(times[0]), gap, actual(times[2]), actual(times[3])],
    )
    planner = LocalPlanner()
    with pytest.raises(ValueError, match="coverage gaps"):
        simulate(data, Variant("test"), planner)
    assert not planner.calls


def test_expired_schedule_breaks_coverage_and_target_checks_use_actual_soc():
    p = payload()
    p["battery_entities"][0]["target"] = {"timeslot": 0, "soc_kwh": 3}
    data = dataset(
        [event(**p), event(START + timedelta(hours=5))],
        [actual(START + timedelta(hours=i)) for i in range(6)],
    )
    with pytest.raises(ValueError, match="Missing planning cycles"):
        simulate(data, Variant("test"), LocalPlanner())


@pytest.mark.parametrize("mode", ["invalid", None])
def test_invalid_planner_results_fail_whole_run_without_partial_score(mode):
    report = simulate(dataset(), Variant("bad"), LocalPlanner(mode))
    assert report["status"] == "failed"
    assert report["totals"] is None


def _records(data):
    return [
        {
            "schema_version": 2,
            "setup_id": "home",
            "currency": "DKK",
            "timezone": "UTC",
            "created_at": e.captured.isoformat(),
            "input_captured_at": e.captured.isoformat(),
            "request": {"optimizer_params": e.params},
            "result": {},
        }
        for e in data.events
    ] + [
        {
            "schema_version": 2,
            "setup_id": "home",
            "record_type": "actuals",
            "actuals": [a.values for a in data.actuals],
        }
    ]


def test_loads_service_responses_deduplicates_and_rejects_conflicting_actuals(tmp_path):
    records = _records(dataset())
    content = "".join(json.dumps(r) + "\n" for r in records)
    (tmp_path / "day.jsonl").write_text(content)
    (tmp_path / "export.json").write_text(json.dumps({"data": content}))
    loaded = load_dataset(tmp_path)
    assert len(loaded.events) == 2
    assert len(loaded.actuals) == 2
    assert len(loaded.files) == 2
    records[-1]["actuals"][0]["usage_kwh"] = 0.9
    (tmp_path / "export.json").write_text(
        json.dumps({"data": "\n".join(json.dumps(r) for r in records)})
    )
    with pytest.raises(ValueError, match="Conflicting actuals"):
        load_dataset(tmp_path)


def test_legacy_decision_time_and_missing_pv_are_explicit(tmp_path):
    records = _records(dataset())
    records[0].pop("input_captured_at")
    records[0]["schema_version"] = 1
    records[0]["created_at"] = (START + timedelta(minutes=5)).isoformat()
    records[-1]["actuals"][0]["pv_kwh"] = None
    (tmp_path / "day.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + '\n{"broken":'
    )
    loaded = load_dataset(tmp_path)
    assert not loaded.actuals[0].usable
    assert len(loaded.warnings) == 2
    assert loaded.events[0].at == START + timedelta(minutes=5)


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda r: r.append({**r[0], "setup_id": "other"}), "Multiple setups"),
        (
            lambda r: r[-1]["actuals"][0].update(
                end=(START + timedelta(hours=2)).isoformat()
            ),
            "overlap",
        ),
        (lambda r: r[-1]["actuals"][0].update(pv_kwh=float("nan")), "Invalid actual"),
    ],
)
def test_loader_fails_closed_on_ambiguous_or_invalid_inputs(tmp_path, change, error):
    records = _records(dataset())
    change(records)
    (tmp_path / "day.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    with pytest.raises(ValueError, match=error):
        load_dataset(tmp_path)


def test_variant_cannot_change_physical_world():
    with pytest.raises(ValueError, match="physical"):
        Variant("bad", settings={"battery_entities": []})


def counter_factory():
    calls = 0
    baseline = self_consumption_factory()

    def plan(params):
        nonlocal calls
        calls += 1
        result = baseline(params)
        result["counter"] = calls
        return result

    return plan


def hanging_factory():
    def plan(params):
        time.sleep(30)

    return plan


def test_worker_isolates_variant_state_and_resets_factory_at_segment_boundaries():
    with PlannerWorker("tests.optimizer.test_simulation:counter_factory") as worker:
        assert worker.call(payload(), reset=True, timeout=30)["result"]["counter"] == 1
        assert worker.call(payload(), reset=False, timeout=30)["result"]["counter"] == 2
        assert worker.call(payload(), reset=True, timeout=30)["result"]["counter"] == 1
    with PlannerWorker("tests.optimizer.test_simulation:counter_factory") as worker:
        assert worker.call(payload(), reset=True, timeout=30)["result"]["counter"] == 1


def test_worker_timeout_kills_process():
    with PlannerWorker("tests.optimizer.test_simulation:hanging_factory") as worker:
        with pytest.raises(TimeoutError):
            worker.call(payload(), reset=True, timeout=0.05)
        assert not worker._process.is_alive()


def test_real_planner_can_run_synthetic_cycles_with_shared_physics():
    from custom_components.wattplan.optimizer import optimize

    class Adapter:
        def call(self, p, **kwargs):
            return {
                "result": optimize(OptimizationParams(**p)),
                "wall_seconds": 0.0,
                "cpu_seconds": 0.0,
            }

    report = simulate(synthetic_dataset(payload(), 2), Variant("highs"), Adapter())
    assert report["status"] == "complete"
    assert report["totals"]["usage_kwh"] == 4
    assert report["totals"]["loss_kwh"] == 0
    assert report["totals"]["grid_import_kwh"] >= 2


def test_reports_keep_terminal_reserves_with_cost_comparisons(tmp_path):
    first = simulate(dataset(), Variant("preserve"), LocalPlanner("preserve"))
    second = simulate(dataset(), Variant("consume"), LocalPlanner())
    first["run"], second["run"] = "preserve", "consume"
    delta = comparisons([first, second])[0]
    assert delta["net_cost_delta"] == -2
    assert not delta["equal_terminal_reserves"]
    assert delta["segments"][0]["final_soc_delta_kwh"] == {"battery": -1}
    write_reports(tmp_path, {"warnings": []}, [first, second])
    assert (tmp_path / "01-intervals.csv").is_file()
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert "intervals" not in summary["runs"][0]
    assert "Stored energy" in (tmp_path / "report.html").read_text()


def test_cli_validation_and_isolated_repeated_reports(tmp_path):
    from scripts.simulate_planner import main

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "day.jsonl").write_text(
        "\n".join(json.dumps(r) for r in _records(dataset()))
    )
    config = tmp_path / "variants.json"
    config.write_text(
        json.dumps(
            {
                "variants": [
                    {"name": "first"},
                    {"name": "second", "planner": BASELINE_PLANNER},
                ]
            }
        )
    )
    assert main([str(inputs), "--validate-only"]) == 0
    output = tmp_path / "output"
    assert (
        main(
            [
                str(inputs),
                "--config",
                str(config),
                "--output",
                str(output),
                "--repeats",
                "2",
            ]
        )
        == 0
    )
    summary = json.loads((output / "summary.json").read_text())
    assert summary["run_order"] == [
        "first / repeat 1",
        "second / repeat 1",
        "second / repeat 2",
        "first / repeat 2",
    ]
    assert summary["coverage"]["covered_seconds"] == 7200
    assert all(run["status"] == "complete" for run in summary["runs"])
    assert (output / "report.html").is_file()


def test_glob_selection_and_no_match(tmp_path):
    content = "\n".join(json.dumps(r) for r in _records(dataset()))
    (tmp_path / "selected.jsonl").write_text(content)
    (tmp_path / "ignored.json").write_text("{}")
    loaded = load_dataset(str(tmp_path / "*.jsonl"))
    assert len(loaded.files) == 1
    assert loaded.range_info()["hours"] == 2
    with pytest.raises(ValueError, match="No JSON/JSONL"):
        load_dataset(str(tmp_path / "missing-*.jsonl"))


@pytest.mark.parametrize("direction", ["previous", "next"])
@pytest.mark.parametrize("missing", ["actual", "plan", "both"])
def test_explicit_one_slot_repair_keeps_provenance(direction, missing):
    times = [START + timedelta(hours=i) for i in range(3)]
    data = dataset([event(t) for t in times], [actual(t) for t in times])
    if missing in {"actual", "both"}:
        data.actuals.pop(1)
    if missing in {"plan", "both"}:
        data.events.pop(1)
    with pytest.raises(ValueError, match="coverage gaps|Missing planning cycles"):
        data.windows()
    data.validate_coverage(direction)
    windows, _ = data.windows()
    assert sum((w.end - w.start).total_seconds() for w in windows) == 10800
    assert len(data.patches) == (2 if missing == "both" else 1)
    assert all(p["direction"] == direction for p in data.patches)
    assert all(p["start"] == times[1].isoformat() for p in data.patches)
    if missing in {"actual", "both"}:
        assert (
            data.actuals[1].values["simulation_patch"]["source_start"]
            == times[0 if direction == "previous" else 2].isoformat()
        )
        assert data.actuals[1].values["grid_import_kwh"] is None


def test_two_gaps_or_distinct_actual_and_plan_gaps_cannot_be_repaired():
    times = [START + timedelta(hours=i) for i in range(4)]
    data = dataset([event(t) for t in times], [actual(times[0]), actual(times[3])])
    with pytest.raises(ValueError, match="coverage gaps"):
        data.validate_coverage("previous")
    data = dataset(
        [event(t) for t in (times[0], times[1], times[3])],
        [actual(t) for t in (times[0], times[2], times[3])],
    )
    with pytest.raises(ValueError, match="Missing planning cycles"):
        data.validate_coverage("previous")


def test_mixed_cadence_and_partial_boundary_fail_closed():
    data = dataset([event(), event(START + timedelta(hours=1), slot_minutes=15)])
    with pytest.raises(ValueError, match="Mixed planner slot"):
        data.windows()
    data = dataset(
        actuals=[
            actual(START + timedelta(seconds=5), hours=3595 / 3600),
            actual(START + timedelta(hours=1)),
        ]
    )
    with pytest.raises(ValueError, match="coverage gaps"):
        data.validate_coverage("next")


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_local_day_range_is_dst_aware(day, hours):
    data = dataset()
    data.timezone = "Europe/Copenhagen"
    data.dates = [day]
    info = data.range_info()
    assert info["hours"] == hours
    assert info["slots"] == hours
    assert info["basis"] == "local_calendar_days"


def test_empty_dated_file_and_export_date_preserve_missing_boundaries(tmp_path):
    content = "\n".join(json.dumps(r) for r in _records(dataset()))
    (tmp_path / "2026-09-19.jsonl").write_text("")
    (tmp_path / "export.json").write_text(
        json.dumps({"date": "2026-09-20", "data": content})
    )
    loaded = load_dataset(tmp_path)
    assert loaded.range_info()["start"] == "2026-09-19T00:00:00+00:00"
    assert loaded.range_info()["hours"] == 48
    with pytest.raises(ValueError, match="coverage gaps"):
        loaded.windows()


def test_capture_offset_is_disclosed_without_shrinking_range():
    data = dataset(
        [
            event(captured=START + timedelta(seconds=2)),
            event(
                START + timedelta(hours=1),
                captured=START + timedelta(hours=1, seconds=2),
            ),
        ]
    )
    windows, _ = data.windows()
    assert data.range_info()["hours"] == 2
    assert sum((w.end - w.start).total_seconds() for w in windows) == 7198


def test_planner_repair_preserves_absolute_deadline_and_no_chain():
    times = [START + timedelta(hours=i) for i in range(3)]
    p = payload()
    p["battery_entities"][0]["target"] = {"timeslot": 2, "soc_kwh": 3}
    data = dataset([event(**p), event(times[2])], [actual(t) for t in times])
    data.validate_coverage("previous")
    assert data.events[1].params["battery_entities"][0]["target"]["timeslot"] == 1
    data.events.pop(2)
    with pytest.raises(ValueError, match="Missing planning cycles|synthetic neighbor"):
        data.validate_coverage("previous")


def test_out_of_range_context_does_not_seed_state_or_execute():
    times = [START + timedelta(hours=i) for i in range(24)]
    prior = [event(START - timedelta(hours=i)) for i in (2, 1)]
    for e in prior:
        e.params["battery_entities"][0]["initial_kwh"] = 0
    data = dataset(
        prior + [event(t, captured=t + timedelta(seconds=2)) for t in times],
        [actual(t) for t in times],
    )
    data.dates = [START.date()]
    windows, _ = data.windows()
    assert len(windows) == 24
    assert windows[0].start == START + timedelta(seconds=2)
    assert windows[0].event.params["battery_entities"][0]["initial_kwh"] == 2
    assert windows[0].reset_reason == "initial_or_coverage_gap"


@pytest.mark.parametrize("capture_hour", [0.5, 2 + 1 / 3])
def test_out_of_slot_capture_does_not_override_repaired_cycle(capture_hour):
    times = [START + timedelta(hours=i) for i in range(3)]
    late = event(times[1], captured=START + timedelta(hours=capture_hour))
    data = dataset([event(), event(times[2]), late], [actual(t) for t in times])
    data.validate_coverage("previous")
    windows, skipped = data.windows()
    assert len(windows) == 3
    assert late not in [w.event for w in windows]
    assert skipped == [
        {"at": late.at.isoformat(), "reason": "capture_outside_plan_slot"}
    ]


def test_next_neighbor_repair_rejects_deadline_beyond_copied_horizon():
    times = [START + timedelta(hours=i) for i in range(3)]
    p = payload()
    p["battery_entities"][0]["target"] = {"timeslot": 3, "soc_kwh": 3}
    data = dataset([event(), event(times[2], **p)], [actual(t) for t in times])
    with pytest.raises(ValueError, match="rebased deadlines/windows must fit"):
        data.validate_coverage("next")
