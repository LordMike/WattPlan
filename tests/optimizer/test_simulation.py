"""Independent physics checks and causal planner simulation regressions."""

import json
import time
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.wattplan.optimizer.simulation import (
    engine,
    worker as worker_module,
)
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
from custom_components.wattplan.optimizer.simulation.engine import (
    RUN_TIMEOUT_SECONDS,
    RunBudget,
    Variant,
    simulate,
)
from custom_components.wattplan.optimizer.simulation.physics import transition
from custom_components.wattplan.optimizer.simulation.reporting import (
    comparisons,
    write_reports,
)
from custom_components.wattplan.optimizer.simulation.scenarios import synthetic_dataset
from custom_components.wattplan.optimizer.simulation.worker import (
    PlannerTimeout,
    PlannerWorker,
)

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


def test_named_preset_overrides_variant_tuning_but_keeps_physical_request():
    data = dataset(events=[event()], actuals=[actual()])
    variant = Variant(
        "preset-highs",
        planner="highs",
        settings={
            "lookahead_slots": 6,
            "throughput_cost_per_kwh": 1.7,
            "action_deadband_kwh": 0.3,
            "mode_switch_cost": 2.1,
            "solver_time_limit_seconds": 7.5,
            "infer_battery_preserve_policy": False,
        },
        options={"lookahead_slots": 22},
        preset="highs",
    )
    planner = LocalPlanner()

    report = simulate(data, variant, planner)

    assert report["status"] == "complete"
    used = planner.calls[0][0]
    assert used["lookahead_slots"] == 22
    assert used["throughput_cost_per_kwh"] == 0.0
    assert used["action_deadband_kwh"] == 0.0
    assert used["mode_switch_cost"] == 0.0
    assert used["solver_time_limit_seconds"] == 20.0
    assert used["infer_battery_preserve_policy"] is True
    assert used["grid_import_price_per_kwh"] == [2.0] * 4
    assert used["battery_entities"][0]["capacity_kwh"] == 4.0


@pytest.mark.parametrize("slot_minutes", [15, 30, 60])
def test_run_timeout_is_two_minutes_independent_of_slot_length(slot_minutes):
    data = dataset(
        [event(slot_minutes=slot_minutes)],
        [actual(hours=slot_minutes / 60)],
    )
    planner = LocalPlanner()
    report = simulate(data, Variant("test"), planner)
    assert report["status"] == "complete"
    assert 0 < planner.calls[0][2] <= RUN_TIMEOUT_SECONDS == 120
    assert report["run_timeout_seconds"] == 120
    assert report["run_wall_seconds"] >= 0


@pytest.mark.parametrize("slots", [96, 192])
def test_same_budget_covers_all_calls_without_scaling_with_dataset_length(
    slots, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(engine, "monotonic", lambda: clock[0])

    class TimedPlanner(LocalPlanner):
        def __init__(self):
            super().__init__()
            self.timeouts = []
            self.killed = False

        def call(self, p, *, reset, timeout):
            self.timeouts.append(timeout)
            if timeout < 70:
                clock[0] += timeout
                raise PlannerTimeout(timeout, timeout)
            clock[0] += 70  # Each call individually takes less than 120 seconds.
            return super().call(p, reset=reset, timeout=timeout)

        def close(self, *, kill=False):
            self.killed = kill

    p = payload()
    p["slot_minutes"] = 15
    planner = TimedPlanner()
    report = simulate(synthetic_dataset(p, slots), Variant("timed"), planner)
    assert report["status"] == "failed"
    assert report["totals"] is None
    assert planner.timeouts == [120, 50]
    assert planner.killed
    assert report["run_timeout_seconds"] == report["run_wall_seconds"] == 120
    assert report["failure_details"]["scope"] == "run"
    assert report["failure_details"]["timeout_seconds"] == 120
    assert report["failure_details"]["completed_cycles"] == 1
    assert report["failure_details"]["completed_planning_calls"] == 1


@pytest.mark.parametrize("phase", ["validate_result", "transition"])
def test_simulation_overhead_counts_against_run_deadline(monkeypatch, phase):
    clock = [1000.0]
    monkeypatch.setattr(engine, "monotonic", lambda: clock[0])
    original = getattr(engine, phase)

    def expensive(*args, **kwargs):
        clock[0] += 121
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, phase, expensive)
    planner = LocalPlanner()
    report = simulate(dataset(), Variant("timed"), planner)
    assert report["status"] == "failed"
    assert report["totals"] is None
    assert len(planner.calls) == 1
    assert report["failure_details"]["completed_planning_calls"] == 1
    assert report["failure_details"]["completed_cycles"] == 0
    assert report["failure_details"]["elapsed_seconds"] == 121
    assert report["intervals"] == []


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


def hanging_second_factory():
    calls = 0
    baseline = self_consumption_factory()

    def plan(params):
        nonlocal calls
        calls += 1
        if calls == 2:
            time.sleep(30)
        return baseline(params)

    return plan


def _nonreading_worker(connection, factory, options):
    """A starting child that never accepts its first request."""
    time.sleep(30)


def test_worker_isolates_variant_state_and_resets_factory_at_segment_boundaries():
    with PlannerWorker("tests.optimizer.test_simulation:counter_factory") as worker:
        assert worker.call(payload(), reset=True, timeout=30)["result"]["counter"] == 1
        assert worker.call(payload(), reset=False, timeout=30)["result"]["counter"] == 2
        assert worker.call(payload(), reset=True, timeout=30)["result"]["counter"] == 1
    with PlannerWorker("tests.optimizer.test_simulation:counter_factory") as worker:
        assert worker.call(payload(), reset=True, timeout=30)["result"]["counter"] == 1


def test_worker_rechecks_deadline_after_early_wait_return(monkeypatch):
    from types import SimpleNamespace

    clock = [0.0]
    callbacks = []

    class EarlyEvent:
        ready = False
        waits = 0

        def is_set(self):
            return self.ready

        def set(self):
            self.ready = True

        def wait(self, remaining):
            self.waits += 1
            clock[0] += 0.01
            if self.waits == 1:
                return False
            callbacks[0]()
            return self.ready

    completed = EarlyEvent()

    def thread(*, target, daemon):
        callbacks.append(target)
        return SimpleNamespace(start=lambda: None, join=lambda: None)

    monkeypatch.setattr(worker_module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(worker_module, "threading", SimpleNamespace(Event=lambda: completed, Thread=thread))
    worker = object.__new__(PlannerWorker)
    worker._closed = False
    worker._exchange = None
    worker._connection = SimpleNamespace(send=lambda message: None, recv=lambda: {"result": "ready"})
    worker.close = lambda **kwargs: pytest.fail("A premature wake must not kill the worker")
    assert worker.call({}, reset=True, timeout=0.05) == {"result": "ready"}
    assert completed.waits == 2
    assert clock[0] == pytest.approx(0.02)


def test_worker_timeout_kills_process():
    with PlannerWorker("tests.optimizer.test_simulation:hanging_factory") as worker:
        with pytest.raises(PlannerTimeout) as raised:
            worker.call(payload(), reset=True, timeout=0.05)
        assert raised.value.timeout_seconds == 0.05
        assert raised.value.elapsed_seconds >= 0.05
        assert not worker._process.is_alive()
        assert not worker._exchange.is_alive()
        with pytest.raises(RuntimeError, match="closed"):
            worker.call(payload(), reset=True, timeout=1)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_worker_rejects_invalid_timeout(timeout):
    with PlannerWorker(BASELINE_PLANNER) as worker:
        with pytest.raises(ValueError, match="finite and positive"):
            worker.call(payload(), reset=True, timeout=timeout)
        assert worker._exchange is None


def test_worker_cannot_extend_whole_call_cap(monkeypatch):
    monkeypatch.setattr(worker_module, "PLANNER_TIMEOUT_SECONDS", 0.05)
    with PlannerWorker("tests.optimizer.test_simulation:hanging_factory") as worker:
        with pytest.raises(PlannerTimeout) as raised:
            worker.call(payload(), reset=True, timeout=900)
        assert raised.value.timeout_seconds == 0.05
        assert not worker._process.is_alive()
        assert not worker._exchange.is_alive()


def test_timeout_covers_blocked_request_send(monkeypatch):
    monkeypatch.setattr(worker_module, "_serve", _nonreading_worker)
    with PlannerWorker(BASELINE_PLANNER) as worker:
        # Larger than the pipe buffer; the child never drains the request.
        started = time.perf_counter()
        with pytest.raises(PlannerTimeout):
            worker.call({"large": "x" * 8_000_000}, reset=True, timeout=0.1)
        assert time.perf_counter() - started < 5
        assert not worker._process.is_alive()
        assert not worker._exchange.is_alive()


def test_timeout_covers_blocked_response_read(monkeypatch):
    with PlannerWorker(BASELINE_PLANNER) as worker:
        worker.call(payload(), reset=True, timeout=30)  # Finish child startup.
        receive = worker._connection.recv

        def blocked_receive():
            receive()  # Some response data arrived, so poll would have succeeded.
            return receive()  # The full read still blocks waiting for more bytes.

        monkeypatch.setattr(worker._connection, "recv", blocked_receive)
        started = time.perf_counter()
        with pytest.raises(PlannerTimeout):
            worker.call(payload(), reset=False, timeout=0.1)
        assert time.perf_counter() - started < 5
        assert not worker._process.is_alive()
        assert not worker._exchange.is_alive()


def test_worker_failure_cleans_up_exchange_thread():
    with PlannerWorker("tests.optimizer.test_simulation:counter_factory") as worker:
        with pytest.raises(RuntimeError, match="ValidationError"):
            worker.call({}, reset=True, timeout=30)
        assert not worker._process.is_alive()
        assert not worker._exchange.is_alive()


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
    assert summary["run_timeout_seconds"] == 120
    assert (output / "report.html").is_file()


def test_cli_timeout_invalidates_partial_score_and_continues_variants(
    tmp_path, monkeypatch
):
    from scripts import simulate_planner

    # Leave time for a cold worker startup before testing the hanging second call.
    timeout_seconds = 10
    workers = []

    class RecordingWorker(PlannerWorker):
        def __init__(self, factory, options):
            super().__init__(factory, options)
            workers.append(self)

    monkeypatch.setattr(simulate_planner, "PlannerWorker", RecordingWorker)
    monkeypatch.setattr(simulate_planner, "RunBudget", lambda: RunBudget(timeout_seconds))
    monkeypatch.setattr(simulate_planner, "RUN_TIMEOUT_SECONDS", timeout_seconds)
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_text("\n".join(json.dumps(r) for r in _records(dataset())))
    config = tmp_path / "variants.json"
    config.write_text(
        json.dumps(
            {
                "variants": [
                    {
                        "name": "timeout",
                        "planner": "tests.optimizer.test_simulation:hanging_second_factory",
                    },
                    {"name": "later", "planner": BASELINE_PLANNER},
                ]
            }
        )
    )
    output = tmp_path / "output"
    assert (
        simulate_planner.main(
            [str(inputs), "--config", str(config), "--output", str(output)]
        )
        == 1
    )
    summary = json.loads((output / "summary.json").read_text())
    failed, complete = summary["runs"]
    assert failed["status"] == "failed"
    assert failed["totals"] is None
    assert failed["run_timeout_seconds"] == timeout_seconds
    assert failed["failure_details"]["at"] == (START + timedelta(hours=1)).isoformat()
    assert failed["failure_details"]["exception_type"] == "RunTimeout"
    assert failed["failure_details"]["scope"] == "run"
    assert failed["failure_details"]["timeout_seconds"] == timeout_seconds
    assert failed["failure_details"]["elapsed_seconds"] >= timeout_seconds
    assert failed["failure_details"]["completed_cycles"] == 1
    full_failed = json.loads((output / "01-run.json").read_text())
    assert len(full_failed["cycles"]) == len(full_failed["intervals"]) == 1
    assert complete["status"] == "complete"
    assert complete["totals"]["covered_seconds"] == 7200
    assert summary["comparisons"] == []  # Partial failed costs never compare.
    assert (
        f"Whole-run wall-clock limit: {timeout_seconds} seconds"
        in (output / "report.html").read_text()
    )
    assert all(not worker._process.is_alive() for worker in workers)
    assert all(not worker._exchange.is_alive() for worker in workers)


def test_cli_counts_startup_and_gives_every_variant_repeat_a_fresh_budget(
    tmp_path, monkeypatch
):
    from scripts import simulate_planner

    clock = [1000.0]
    workers = []
    monkeypatch.setattr(engine, "monotonic", lambda: clock[0])

    class SlowStartingWorker(LocalPlanner):
        def __init__(self, factory, options):
            super().__init__()
            self.killed = False
            workers.append(self)
            clock[0] += 1 if factory == BASELINE_PLANNER else 121

        def close(self, *, kill=False):
            self.killed |= kill

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(simulate_planner, "PlannerWorker", SlowStartingWorker)
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_text("\n".join(json.dumps(r) for r in _records(dataset())))
    config = tmp_path / "variants.json"
    config.write_text(
        json.dumps(
            {
                "variants": [
                    {"name": "slow-start"},
                    {"name": "fast", "planner": BASELINE_PLANNER},
                ]
            }
        )
    )
    output = tmp_path / "output"
    assert (
        simulate_planner.main(
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
        == 1
    )
    summary = json.loads((output / "summary.json").read_text())
    assert [run["status"] for run in summary["runs"]] == [
        "failed",
        "complete",
        "complete",
        "failed",
    ]
    for run in summary["runs"]:
        assert run["run_timeout_seconds"] == 120
        if run["status"] == "failed":
            assert run["run_wall_seconds"] == 121
            assert run["completed_cycles"] == 0
            assert run["totals"] is None
        else:
            assert run["run_wall_seconds"] == 1
            assert run["completed_cycles"] == 2
    assert [worker.killed for worker in workers] == [True, False, False, True]
    assert [len(worker.calls) for worker in workers] == [0, 2, 2, 0]
    assert all(call[2] == 119 for worker in workers for call in worker.calls)


def test_glob_selection_and_no_match(tmp_path):
    content = "\n".join(json.dumps(r) for r in _records(dataset()))
    (tmp_path / "selected.jsonl").write_text(content)
    (tmp_path / "ignored.json").write_text("{}")
    loaded = load_dataset(str(tmp_path / "*.jsonl"))
    assert len(loaded.files) == 1
    assert loaded.range_info()["hours"] == 2
    with pytest.raises(ValueError, match="No JSON/JSONL"):
        load_dataset(str(tmp_path / "missing-*.jsonl"))


def test_multiple_input_sources_deduplicate_paths_but_reject_any_missing_source(tmp_path):
    from custom_components.wattplan.optimizer.simulation.dataset import input_files

    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.json"
    records = _records(dataset())
    first.write_text("\n".join(json.dumps(r) for r in records[:-1]))
    second.write_text(json.dumps(records[-1]))
    sources = [first, str(tmp_path / "*.json"), tmp_path]
    loaded = load_dataset(sources)
    loaded.validate_coverage()
    assert len(loaded.events) == 2
    assert len(loaded.actuals) == 2
    assert [item["path"] for item in loaded.files] == [str(first), str(second)]
    with pytest.raises(ValueError, match="No JSON/JSONL input files matched"):
        input_files([first, tmp_path / "missing*.json"])
    with pytest.raises(ValueError, match="No JSON/JSONL input files supplied"):
        input_files([])


def test_rest_export_preserves_date_and_incomplete_tail_metadata(tmp_path):
    exported = tmp_path / "download.json"
    exported.write_text(json.dumps({
        "changed_states": [],
        "service_response": {
            "date": START.date().isoformat(),
            "data": "\n".join(json.dumps(r) for r in _records(dataset())),
            "incomplete_tail": True,
        },
    }))
    loaded = load_dataset(exported)
    assert loaded.range_info()["hours"] == 24
    assert any("incomplete trailing record" in warning for warning in loaded.warnings)
    # A REST wrapper must not hide the missing part of an exported calendar day.
    with pytest.raises(ValueError):
        loaded.validate_coverage()


@pytest.mark.parametrize("response", [None, [], {}, {"data": 42}])
def test_rest_export_rejects_invalid_service_response(tmp_path, response):
    exported = tmp_path / "download.json"
    exported.write_text(json.dumps({"changed_states": [], "service_response": response}))
    with pytest.raises(ValueError, match="planner reproduction service response"):
        load_dataset(exported)


def test_cli_lists_and_validates_all_presets_without_running_planners(tmp_path, capsys, monkeypatch):
    from scripts import simulate_planner
    from custom_components.wattplan.optimizer.planners.presets import PRESETS

    def unexpected_worker(*args, **kwargs):
        pytest.fail("Listing and validation must not start a planner")

    monkeypatch.setattr(simulate_planner, "PlannerWorker", unexpected_worker)
    assert simulate_planner.main(["--list-planners"]) == 0
    listing = capsys.readouterr().out
    assert all(f"{name}:" in listing for name in PRESETS)
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_text("\n".join(json.dumps(r) for r in _records(dataset())))
    assert simulate_planner.main([str(inputs), "--planners", "all", "--validate-only"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configuration"] == [
        {
            "name": name,
            "planner": preset.planner,
            "settings": {},
            "options": preset.options,
            "preset": name,
        }
        for name, preset in PRESETS.items()
    ]
    assert simulate_planner.main([
        str(inputs), "--planners", "beam-search", "highs-24", "--validate-only",
    ]) == 0
    configuration = json.loads(capsys.readouterr().out)["configuration"]
    assert configuration[0] == {
        "name": "beam-search", "planner": "beam-search", "settings": {},
        "options": {}, "preset": None,
    }
    assert configuration[1]["options"] == {"lookahead_slots": 24}
    assert configuration[1]["preset"] == "highs-24"


def test_cli_runs_selected_presets_and_rounds_from_multiple_rest_exports(tmp_path):
    from scripts.simulate_planner import main

    records = _records(dataset())
    inputs = []
    for index, subset in enumerate((records[:-1], records[-1:])):
        path = tmp_path / f"export-{index}.json"
        path.write_text(json.dumps({"changed_states": [], "service_response": {
            "data": "\n".join(json.dumps(r) for r in subset),
        }}))
        inputs.append(str(path))
    output = tmp_path / "reports"
    assert main([
        *inputs, "--planners", "local-search-3", "self-consumption",
        "--rounds", "2", "--output", str(output),
    ]) == 0
    summary = json.loads((output / "summary.json").read_text())
    assert summary["run_order"] == [
        "local-search-3 / repeat 1", "self-consumption / repeat 1",
        "self-consumption / repeat 2", "local-search-3 / repeat 2",
    ]
    assert summary["configuration"][0]["options"] == {"max_evaluations": 3, "seed": 0}
    assert len(summary["files"]) == 2
    assert summary["coverage"]["covered_seconds"] == 7200
    assert all(run["status"] == "complete" for run in summary["runs"])


@pytest.mark.parametrize("args", [
    [], ["--validate-only"], ["input.json", "--planners", "unknown"],
    ["input.json", "--planners", "all", "highs", "--validate-only"],
    ["input.json", "--planners", "highs", "--config", "config.json", "--validate-only"],
    ["input.json", "--rounds", "0", "--validate-only"],
])
def test_cli_rejects_invalid_selection_arguments(args):
    from scripts.simulate_planner import main

    with pytest.raises(SystemExit) as raised:
        main(args)
    assert raised.value.code == 2


def test_cli_duplicate_planners_and_output_inside_any_input_are_rejected(tmp_path, capsys):
    from scripts.simulate_planner import main

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    path = inputs / "day.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _records(dataset())))
    assert main([str(path), "--planners", "highs", "highs", "--validate-only"]) == 2
    assert "uniquely named" in capsys.readouterr().err
    assert main([str(path), str(inputs), "--output", str(inputs / "reports")]) == 2
    assert "folders must be separate" in capsys.readouterr().err


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
