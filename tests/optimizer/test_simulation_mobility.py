"""Boundary-once mobility events and physical state carry across replanning."""

from datetime import timedelta
import json

import pytest

from custom_components.wattplan.optimizer.simulation.engine import Variant, simulate
from custom_components.wattplan.optimizer.simulation.scenarios import shift_request
from custom_components.wattplan.optimizer.simulation.reporting import comparisons, write_reports
from tests.optimizer.test_simulation import START, LocalPlanner, actual, dataset, event, payload


def mobility_payload():
    p = payload()
    p["usage_kwh"] = [0.0] * 4
    p["battery_entities"][0].update(connected_slots=[False, True, True, True],
        external_energy_kwh=[1.0, 0.0, 0.0, 0.0],
        departure_requirements=[dict(timeslot=0, minimum_kwh=2.0)])
    return p


def test_half_hour_actual_boundaries_apply_one_full_dc_draw():
    p = mobility_payload()
    data = dataset(events=[event(**{k: v for k, v in p.items() if k != "plan_start"})],
        actuals=[actual(usage=0, hours=0.5), actual(START + timedelta(minutes=30), usage=0, hours=0.5)])
    report = simulate(data, Variant("mobility"), LocalPlanner("preserve"))
    assert report["status"] == "complete"
    assert [row["external_energy_kwh"] for row in report["intervals"]] == [0.0, 1.0]
    assert report["intervals"][-1]["soc_kwh"]["battery"] == 1.0
    assert report["totals"]["service_feasible"] is True
    assert report["segments"][0]["departure_checks"][0]["soc_kwh"] == 2.0
    assert "synthetic" in report["mobility_assumption"]


def test_changing_schedule_does_not_reset_hardware_or_return_inventory():
    p = mobility_payload()
    first = event(**{k: v for k, v in p.items() if k != "plan_start"})
    next_p = shift_request(p, 1)
    next_p["battery_entities"][0]["initial_kwh"] = 4.0
    second = event(START + timedelta(hours=1), **{k: v for k, v in next_p.items() if k != "plan_start"})
    assert first.hardware == second.hardware
    planner = LocalPlanner("preserve")
    report = simulate(dataset(events=[first, second], actuals=[actual(usage=0),
        actual(START + timedelta(hours=1), usage=0)]), Variant("carry"), planner)
    assert report["status"] == "complete"
    assert len(report["segments"]) == 1
    assert planner.calls[1][0]["battery_entities"][0]["initial_kwh"] == 1.0
    assert report["totals"]["external_energy_kwh"] == 1.0
    assert report["intervals"][-1]["soc_kwh"]["battery"] == 1.0


def test_unserved_mobility_is_completed_but_service_infeasible():
    p = mobility_payload()
    p["battery_entities"][0]["external_energy_kwh"][0] = 3.0
    p["battery_entities"][0]["departure_requirements"][0]["minimum_kwh"] = 3.0
    report = simulate(dataset(events=[event(**{k: v for k, v in p.items() if k != "plan_start"})],
        actuals=[actual(usage=0)]), Variant("infeasible"), LocalPlanner("preserve"))
    assert report["status"] == "complete"
    assert report["totals"]["external_unserved_kwh"] == 1.0
    assert report["totals"]["departure_misses"] == 1
    assert report["totals"]["service_failures"] == 2
    assert report["totals"]["service_feasible"] is False
    assert report["intervals"][-1]["soc_kwh"]["battery"] == 0.0


def test_fixture_shifts_absolute_mobility_without_periodic_trips():
    p = mobility_payload()
    shifted = shift_request(p, 1)
    battery = shifted["battery_entities"][0]
    assert battery["connected_slots"] == [True, True, True, False]
    assert battery["external_energy_kwh"] == [0.0] * 4
    assert battery["departure_requirements"] == []
    exhausted = shift_request(p, 8)["battery_entities"][0]
    assert exhausted["connected_slots"] == [False] * 4
    assert exhausted["external_energy_kwh"] == [0.0] * 4


def test_completed_infeasible_trajectory_is_not_a_savings_winner(tmp_path):
    def run(name, cost, feasible):
        return dict(run=name, status="complete", totals=dict(net_cost=cost,
            service_feasible=feasible, service_failures=0 if feasible else 1),
            cycles=[], intervals=[], segments=[dict(segment=1, start="start", end="end",
                initial_soc_kwh={"ev": 24.0}, final_soc_kwh={"ev": 24.0})])
    baseline, failed, alternative = run("baseline", 10, True), run("cheap-invalid", 0, False), run("valid", 8, True)
    result = comparisons([baseline, failed, alternative])
    assert len(result) == 1
    assert result[0]["run"] == "valid"
    assert result[0]["reference"] == "baseline"
    assert result[0]["net_cost_delta"] == -2
    write_reports(tmp_path, {}, [baseline, failed, alternative])
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert len(summary["runs"]) == 3
    assert summary["comparison_exclusions"] == {"cheap-invalid": "mobility_service_infeasible"}
    assert "Mobility service failed" in (tmp_path / "report.html").read_text()
    # An invalid original control cannot be replaced silently by an alternative.
    assert comparisons([failed, baseline, alternative]) == []
    write_reports(tmp_path, {}, [failed, baseline, alternative])
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["comparison_omission_reason"] == "reference_mobility_service_infeasible"


@pytest.mark.parametrize("mode,usage,pv", [
    ("grid_charge", 0.0, 0.4),
    ("self_consume", 1.0, 0.0),
])
def test_active_fleet_flows_allow_independent_fractional_energy_audit(mode, usage, pv):
    p = mobility_payload()
    home = p["battery_entities"][0]
    home.update(connected_slots=None, external_energy_kwh=None,
        departure_requirements=[], charge_efficiency=0.8, discharge_efficiency=0.5)
    ev = dict(home, name="ev", connected_slots=[False, True, True, True],
        external_energy_kwh=[0.25, 0.0, 0.0, 0.0])
    p["battery_entities"] = [home, ev]
    following = shift_request(p, 1)
    report = simulate(dataset(
        events=[event(**{k: v for k, v in p.items() if k != "plan_start"}),
            event(START + timedelta(hours=1), **{k: v for k, v in following.items() if k != "plan_start"})],
        actuals=[actual(START + timedelta(minutes=30 * i), usage=usage, pv=pv, hours=0.5)
            for i in range(4)]), Variant("fleet-audit"), LocalPlanner(mode))
    assert report["status"] == "complete"
    previous = {"battery": 2.0, "ev": 2.0}
    for index, row in enumerate(report["intervals"]):
        flows = row["battery_flows"]
        assert [flow["name"] for flow in flows] == ["battery", "ev"]
        assert sum(flow["charge_kwh"] for flow in flows) == pytest.approx(row["charge_kwh"])
        assert sum(flow["discharge_kwh"] for flow in flows) == pytest.approx(row["discharge_kwh"])
        for flow in flows:
            name = flow["name"]
            assert flow["level_before_kwh"] == pytest.approx(previous[name])
            # AC charge/discharge and DC traction are accounted independently.
            before_draw = previous[name] + flow["charge_kwh"] * 0.8 - flow["discharge_kwh"] / 0.5
            assert flow["level_before_external_kwh"] == pytest.approx(before_draw)
            assert flow["level_kwh"] == pytest.approx(before_draw - row["external_energy_by_battery"][name])
            assert flow["level_kwh"] == row["soc_kwh"][name]
            assert 0 <= flow["charge_kwh"] <= 1.0
            assert 0 <= flow["discharge_kwh"] <= 1.0
            if name == "ev" and index < 2:
                assert flow["charge_kwh"] == flow["discharge_kwh"] == 0.0
            previous[name] = flow["level_kwh"]
    assert sum(row["external_energy_by_battery"]["ev"] for row in report["intervals"]) == 0.25
    flow_kind = "charge_kwh" if mode == "grid_charge" else "discharge_kwh"
    assert report["intervals"][0]["battery_flows"][0][flow_kind] > 0
    assert report["intervals"][2]["battery_flows"][1][flow_kind] > 0


def test_inactive_simulation_does_not_add_battery_flow_fields():
    report = simulate(dataset(), Variant("legacy"), LocalPlanner())
    assert report["status"] == "complete"
    assert all("battery_flows" not in row for row in report["intervals"])


def test_mobility_run_includes_real_flows_for_inert_connected_windows():
    p = payload()
    p["battery_entities"][0]["capacity_kwh"] = 20.0
    ev = dict(p["battery_entities"][0], name="ev", connected_slots=[True] * 4,
        external_energy_kwh=[0.0] * 4, departure_requirements=[])
    p["battery_entities"].append(ev)
    inert = {k: v for k, v in p.items() if k != "plan_start"}
    active = dict(inert, battery_entities=[p["battery_entities"][0],
        dict(ev, connected_slots=[False, True, True, True], external_energy_kwh=[1.0, 0.0, 0.0, 0.0])])
    report = simulate(dataset(events=[event(**inert),
        event(START + timedelta(hours=1), **active), event(START + timedelta(hours=2), **inert)],
        actuals=[actual(START + timedelta(hours=i), usage=0) for i in range(3)]),
        Variant("mixed-mobility"), LocalPlanner("grid_charge"))
    assert report["status"] == "complete"
    ev_flows = [row["battery_flows"][1] for row in report["intervals"]]
    assert [flow["charge_kwh"] for flow in ev_flows] == [2.0, 0.0, 2.0]
    assert [flow["level_kwh"] for flow in ev_flows] == [4.0, 3.0, 5.0]
    # The same explicitly inert request alone retains the old output shape.
    standalone = simulate(dataset(events=[event(**inert)], actuals=[actual(usage=0)]),
        Variant("inert"), LocalPlanner("grid_charge"))
    assert "battery_flows" not in standalone["intervals"][0]
