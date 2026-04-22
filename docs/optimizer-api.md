# WattPlan Optimizer API

This document describes the direct Python API for the optimizer packaged inside this repository.

- **Package:** `custom_components.wattplan.optimizer`
- **Primary Module:** `custom_components.wattplan.optimizer.mpc_power_optimizer`
- **Primary Function:** `optimize(params: OptimizationParams) -> dict`

The optimizer is model-predictive-control (MPC) based.

If you are using WattPlan through the Home Assistant integration, see [optimizer-profiles.md](optimizer-profiles.md) for the user-facing `Aggressive`, `Balanced`, and `Conservative` presets. Those profiles are integration-level presets that map onto the numeric optimizer fields documented here.

## Time Resolution (Timeslots)
All time-indexed fields use **timeslots**.
- A timeslot is one fixed slice of time at your chosen resolution (for example, 15 minutes).
- Every array index corresponds to one timeslot.
- The API does not enforce a specific minutes-per-timeslot value; the caller is responsible for consistent input resolution.

## Conceptual Model
The solve combines three kinds of entities:
- **Battery Entities:** Controllable storage with modeled charge/discharge flows and serialized policy states for inverter control.
- **Comfort Entities:** Postponable-but-required comfort loads, such as heating or hot water that can be shifted, but should not violate minimum comfort.
- **Optional Entities:** User suggestions for "might run" appliances, such as dishwashers or dryers. They are advisory only and do **not** affect the main optimized schedule. The output is a list of best candidate start timeslots.

## Core Usage
```python
from custom_components.wattplan.optimizer import OptimizationParams, optimize

params = OptimizationParams(**payload)
result = optimize(params)
```

## Request Model (`OptimizationParams`)
| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `grid_import_price_per_kwh` | `list[float]` | Yes | - | Length `4..672`, finite values | Horizon driver (timeslot count). |
| `grid_export_price_per_kwh` | `list[float]` | No | `[]` -> all zeros | Empty or must match `len(grid_import_price_per_kwh)`, finite values | Per-timeslot grid export price. Zero means exported surplus has no monetary value. |
| `solar_input_kwh` | `list[float]` | Yes* | `[]` | Must match `len(grid_import_price_per_kwh)`, finite, `>= 0` | Per-timeslot PV forecast (kWh per timeslot). |
| `usage_kwh` | `list[float]` | Yes* | `[]` | Must match `len(grid_import_price_per_kwh)`, finite, `>= 0` | Per-timeslot base load forecast (kWh per timeslot). |
| `rolling_window_slots` | `int` | No | `24` | `>= 1` | Slot count used for comfort rolling-window ON accounting. |
| `throughput_cost_per_kwh` | `float` | No | `0.0` | Finite, `>= 0` | Extra cost on charge/discharge throughput to reduce cycling. |
| `action_deadband_kwh` | `float` | No | `0.0` | Finite, `>= 0` | Modeled flow commands smaller than this are treated as neutral flow. |
| `mode_switch_cost` | `float` | No | `0.0` | Finite, `>= 0` | Extra cost on changing battery behavior between slots. |
| `battery_entities` | `list[BatteryEntityParams]` | Yes | - | May be empty | Main controllable storage entities. |
| `comfort_entities` | `list[ComfortEntityParams]` | Yes | - | May be empty | Required-but-shiftable comfort entities. |
| `optional_entities` | `list[OptionalEntityParams]` | No | `[]` | Fully validated for feasibility | Advisory start-time options only. |
| `state` | `str \| None` | No | `None` | Valid base64 JSON object, version `v=1` | Opaque carry-over state from previous call. |

\* For direct optimizer API use, `solar_input_kwh` and `usage_kwh` must still match the length of `grid_import_price_per_kwh` when supplied. The Home Assistant integration can synthesize or omit these sources before calling the optimizer.

**Additional Global Constraints:**
- Unknown fields are rejected (`extra="forbid"`).
- Entity names must be unique across battery + comfort + optional groups (case-insensitive).

## Battery Entity Model (`BatteryEntityParams`)
| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `name` | `str` | Yes | - | Non-empty | Unique globally. |
| `initial_kwh` | `float` | Yes | - | Finite, `0..capacity_kwh` | Initial state of charge (kWh). |
| `target` | `BatteryTargetParams \| None` | No | `null` | If set: `timeslot < horizon` | Optional deadline target constraint. |
| `minimum_kwh` | `float` | Yes | - | Finite, `0..capacity_kwh` | Minimum desired state (kWh). |
| `capacity_kwh` | `float` | Yes | - | Finite, `> 0` | Storage capacity (kWh). |
| `charge_curve_kwh` | `list[float]` | Yes | - | Non-empty, finite, `>= 0` | Chargeable energy per slot by SoC curve (kWh per slot). |
| `discharge_curve_kwh` | `list[float]` | Yes | - | Non-empty, finite, `>= 0` | Dischargeable energy per slot by SoC curve (kWh per slot). |
| `charge_efficiency` | `float` | No | `1.0` | Finite, `(0, 1]` | Fraction of charged energy that increases SoC. |
| `discharge_efficiency` | `float` | No | `1.0` | Finite, `(0, 1]` | Fraction of discharged SoC energy delivered to load. |
| `prefer_pv_surplus_charging` | `bool` | No | `false` | - | Internal/deferred hint for routing PV surplus into this battery. This is not exposed as a battery action state and should not be used as a user-facing control contract. |
| `can_charge_from` | `int` | No | `2` | `0`, `1`, `2`, `3` | Allowed charging-ingress flags (`1=GRID`, `2=PV`, `3=GRID|PV`; `0` means charging disabled). |

**Curve Unit Note:**
- `charge_curve_kwh` and `discharge_curve_kwh` are **kWh per slot**, not kW.
- Example: a `2 kW` unit can move at most `0.5 kWh` in a `15-minute` slot (`2 * 0.25 = 0.5`).
- **Efficiency Semantics:**
  - Charging: `SoC gain = charged_energy * charge_efficiency`
  - Discharging: `SoC drop = delivered_energy / discharge_efficiency`

### Battery Target Model (`BatteryTargetParams`)
| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `timeslot` | `int` | Yes | - | `>= 0`, `< horizon` | Deadline timeslot, interpreted as **by end of timeslot**. |
| `soc_kwh` | `float` | Yes | - | `0..capacity_kwh` | Desired SoC level at deadline (kWh). |
| `mode` | `str` | No | `"at_least"` | `at_least`, `at_most`, `exact` | `at_least`: charge (if needed) to be at/above target by deadline. `at_most`: discharge (if needed) to be at/below target. `exact`: charge and discharge as needed to land on target (within tolerance). |
| `tolerance_kwh` | `float` | No | `0.0` | `>= 0` | Allowed kWh tolerance around the target level. |

## Comfort Entity Model (`ComfortEntityParams`)
| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `name` | `str` | Yes | - | Non-empty | Unique globally. |
| `target_on_slots_per_rolling_window` | `int` | Yes | - | `>= 1`, `<= rolling_window_slots` | Required ON slots over rolling window. |
| `min_consecutive_on_slots` | `int` | No | `1` | `>= 1`, `< solve horizon` | Minimum ON lock once enabled. |
| `min_consecutive_off_slots` | `int` | No | `1` | `>= 1`, `< solve horizon` | Minimum OFF lock once disabled. |
| `max_consecutive_off_slots` | `int` | Yes | - | `>= 1`, `>= min_consecutive_off_slots` | Max OFF streak before force-ON. |
| `power_usage_kwh` | `float` | Yes | - | Finite, `> 0` | Energy draw when ON (kWh per slot). |
| `is_on_now` | `bool` | Yes | - | - | Current ON/OFF runtime state. |
| `on_slots_last_rolling_window` | `int` | Yes | - | `>= 0`, `<= rolling_window_slots` | Observed ON slots in prior rolling window. |
| `off_streak_slots_now` | `int` | Yes | - | `>= 0` | Current OFF streak (slots). |
| `measured_power_source` | `str \| null` | No | `null` | - | Optional source of observed power telemetry. |
| `recent_avg_on_power_kw` | `float \| null` | No | `null` | Finite, `> 0` | Optional observed ON power average. |

## Optional Entity Model (`OptionalEntityParams`)
Optional entities provide advisory start-time suggestions and do not change the optimized battery/comfort schedule.
| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `name` | `str` | Yes | - | Non-empty | Unique globally. |
| `duration_timeslots` | `int` | Yes | - | Duration of this optional run. |
| `start_after_timeslot` | `int` | No | `0` | `>= 0`, `< start_before_timeslot` | Earliest allowed start (inclusive). |
| `start_before_timeslot` | `int` | Yes | - | Latest boundary (exclusive). |
| `energy_kwh` | `float \| list[float]` | Yes | - | Finite, `>= 0`; list non-empty and `len <= duration_timeslots` | Scalar is spread uniformly. List is spread step-wise to full duration. |
| `options` | `int` | No | `3` | `> 0`, feasible within window/gap rules | Exact number of options returned. |
| `min_option_gap_timeslots` | `int` | No | `0` | `>= 0` | Minimum spacing between suggested starts. |
| `allow_overlapping_options` | `bool` | No | `false` | - | If false, effective spacing is at least `duration_timeslots`. |

**Feasibility is validated up front.** If requested `options` cannot fit the search window under spacing rules, validation fails.

## Opaque State Contract
`state` is an opaque base64 blob returned by one solve and accepted in the next.
- You should store and pass it back as-is.
- Do not parse or mutate it in client code.
- The optimizer may reuse overlap from prior solve data when it is compatible.

## Full Request Example (Small)
```jsonc
{
  // 8 timeslots (for docs brevity). Real integrations usually use more.
  "grid_import_price_per_kwh": [0.34, 0.31, 0.28, 0.22, 0.18, 0.21, 0.30, 0.42],
  "grid_export_price_per_kwh": [0.00, 0.00, 0.05, 0.08, 0.10, 0.08, 0.02, 0.00],
  "solar_input_kwh":       [0.0,  0.1,  0.5,  1.0,  0.8,  0.3,  0.0,  0.0],
  "usage_kwh":             [1.2,  1.1,  1.0,  0.9,  1.0,  1.2,  1.3,  1.4],
  "rolling_window_slots": 96,

  // Controllable storage: the model tracks grid/PV charge and discharge flows.
  "battery_entities": [
    {
      "name": "home_battery",
      "initial_kwh": 4.0,
      "minimum_kwh": 1.0,
      "capacity_kwh": 10.0,
      "target": {
        "timeslot": 5,
        "soc_kwh": 8.0,
        "mode": "at_least",
        "tolerance_kwh": 0.1
      },
      "charge_curve_kwh": [2.5],
      "discharge_curve_kwh": [2.5],
      "charge_efficiency": 0.95,
      "discharge_efficiency": 0.95,
      "can_charge_from": 2
    }
  ],

  // Postponable but required comfort load (must be maintained above minimum).
  "comfort_entities": [
    {
      "name": "house_heat",
      "target_on_slots_per_rolling_window": 8,
      "min_consecutive_on_slots": 4,
      "min_consecutive_off_slots": 4,
      "max_consecutive_off_slots": 5,
      "power_usage_kwh": 1.1,
      "is_on_now": false,
      "on_slots_last_rolling_window": 3,
      "off_streak_slots_now": 1,
      "measured_power_source": null,
      "recent_avg_on_power_kw": null
    }
  ],

  // Entirely optional appliances: advisory starts only, no effect on base solve.
  "optional_entities": [
    {
      "name": "dishwasher",
      "duration_timeslots": 2,
      "start_after_timeslot": 0,
      "start_before_timeslot": 8,
      "energy_kwh": 1.8,
      "options": 2,
      "min_option_gap_timeslots": 2,
      "allow_overlapping_options": false
    },
    {
      "name": "dryer",
      "duration_timeslots": 4,
      "start_after_timeslot": 0,
      "start_before_timeslot": 8,
      "energy_kwh": [0.4, 1.2],
      "options": 1,
      "min_option_gap_timeslots": 0,
      "allow_overlapping_options": true
    }
  ],

  // Opaque state from previous optimize() response (optional).
  "state": null
}
```

## Response Shape (Summary)
`optimize(...)` returns a dict with these top-level fields:
| Field | Type | Meaning |
|---|---|---|
| `execution_time` | `float` | Solve wall time in seconds. |
| `generations` | `int` | Number of solved timeslots (same as horizon length). |
| `fitness` | `float` | Objective score for final schedule. |
| `avg_price` | `float` | Average effective import price. |
| `projections` | `dict` | Projected cost/savings metrics for this schedule. |
| `overconstrained` | `bool` | Whether soft constraint violations were detected. |
| `suboptimal` | `bool` | `true` when one or more soft targets/limits were unmet. |
| `suboptimal_reasons` | `list[str]` | Machine-readable reason keys for suboptimal output. |
| `problems` | `list[str]` | Human-readable issue tags (if any). |
| `entities` | `list[dict]` | Battery/comfort schedules. |
| `optional_entity_options` | `list[dict]` | Advisory start options per optional entity. |
| `state` | `str` | Opaque base64 state for next call. |

### Battery Policy States
Battery schedule `state` values are inverter-control policies derived from the plan, not raw measured or forecast battery flows:

| State | Meaning |
| --- | --- |
| `preserve` | Prevent this battery from discharging. This saves stored energy when the optimizer shows that spending it now would make the plan worse or violate modeled constraints. PV charging may still be allowed by the user's inverter setup. |
| `self_consume` | Normal battery operation. Allow this battery to cover real load. Do not request grid charging. This is also the default when the model has no positive reason to preserve or grid-charge. |
| `grid_charge` | Request or allow grid charging for this battery and prevent the battery from being spent while doing so. |

PV surplus charging is implicit/normal battery behavior, not a primary action state. PV export is site-level and multi-battery-sensitive, so a dedicated PV export policy is deferred to a future site-level design.

`grid_charge` is emitted when modeled grid charging for that battery is above the action deadband. `preserve` is emitted from a model-backed counterfactual check: when the optimizer chooses not to discharge a battery, WattPlan asks the same model whether forcing a small discharge from that battery for marginal unexpected load would be infeasible or make the objective worse than preserving the battery and importing that marginal energy. Modeled PV surplus is consumed first in that counterfactual, so PV export is not turned into a battery action state. If the forced-discharge alternative is worse, the slot is marked `preserve`; otherwise WattPlan emits `self_consume`. Forecast zero battery flow is not a preserve reason by itself.

### Notes on `entities` and `optional_entity_options`
- `entities` is the actual optimized schedule.
- Battery schedule points encode policy directly in `state`: `preserve`, `self_consume`, or `grid_charge`.
- `optional_entity_options` is advisory and computed on top of that baseline.
- Optional entities do not affect each other and do not modify `entities`.

### `projections` Fields
- `baseline_cost`: Baseline net energy cost across the horizon, including export revenue when `grid_export_price_per_kwh` is provided.
- `projected_cost`: Projected net cost for the optimized schedule (`grid imports - grid export revenue`).
- `projected_savings_cost`: `baseline_cost - projected_cost`.
- `projected_savings_pct`: `(1 - projected_cost / baseline_cost) * 100`, which is equivalent to `(projected_savings_cost / baseline_cost) * 100` when `baseline_cost > 0`. The optimizer still emits the raw numeric result; Home Assistant sensors may choose not to expose extreme values as entity state.
- `per_slot`: List with one object per timeslot (same index/order as input arrays), each containing:
  - `baseline_cost`
  - `projected_cost`
  - `projected_savings_cost`
  - `projected_savings_pct`

### `suboptimal_reasons` Keys
Current machine-readable keys include:
- `battery_min_unmet`: At least one battery dropped below its configured `minimum_kwh` in the solved schedule.
- `battery_target_unmet`: A battery `target` constraint (`at_least`/`at_most`/`exact`) was not met at its target timeslot.
- `comfort_target_unmet`: A comfort entity did not achieve its required ON slots within the rolling window.
- `comfort_max_off_unmet`: A comfort entity exceeded its configured `max_consecutive_off_slots`.

## Validation Behavior
Validation happens before optimization starts.
- Invalid input raises an exception immediately (Pydantic validation error).
- Error messages identify the offending field and why it failed.
- Unknown fields are rejected.

This pre-validation/normalization design ensures the calculation phase operates on strictly shaped, trusted input.

## Terminology
- `grid import`: Energy bought from the grid.
- `grid export`: Energy sent back to the grid. Also commonly called `feed-in`, `export`, or `export to grid`.
- `grid_import_price_per_kwh` / `grid_export_price_per_kwh` are the optimizer terms because they are symmetric and match the physical energy flow direction.
