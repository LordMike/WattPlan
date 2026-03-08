# WattPlan Optimizer API

This document describes the direct Python API for the optimizer packaged inside this repository.

- Package: `custom_components.wattplan.optimizer`
- Primary module: `custom_components.wattplan.optimizer.mpc_power_optimizer`
- Primary function: `optimize(params: OptimizationParams) -> dict`

The optimizer is model-predictive-control (MPC) based.

## Time resolution (timeslots)

All time-indexed fields use **timeslots**.

- A timeslot is one fixed slice of time at your chosen resolution (for example, 15 minutes).
- Every array index corresponds to one timeslot.
- The API does not enforce a specific minutes-per-timeslot value; the caller is responsible for consistent input resolution.

## Conceptual model

The solve combines three kinds of entities:

- `battery_entities`: controllable storage.
  - Can charge, discharge, or hold.
  - Can absorb PV surplus when economically/physically allowed.
- `comfort_entities`: postponable-but-required comfort loads.
  - Think: heating/hot water that can be shifted, but should not violate minimum comfort.
  - They are modeled with gain/loss dynamics and minimum comfort constraints.
- `optional_entities`: user suggestions for "might run" appliances.
  - Think: dishwasher, dryer, washer.
  - They are advisory only and do **not** affect the main optimized schedule.
  - Output is a list of best candidate start timeslots.

## Core usage

```python
from custom_components.wattplan.optimizer import OptimizationParams, optimize

params = OptimizationParams(**payload)
result = optimize(params)
```

## Request model (`OptimizationParams`)

| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `price_per_kwh` | `list[float]` | Yes | - | Length `4..672`, finite values | Horizon driver (timeslot count). |
| `solar_input_kwh` | `list[float]` | Yes* | `[]` | Must match `len(price_per_kwh)`, finite, `>= 0` | Per-timeslot PV forecast (kWh per timeslot). |
| `usage_kwh` | `list[float]` | Yes* | `[]` | Must match `len(price_per_kwh)`, finite, `>= 0` | Per-timeslot base load forecast (kWh per timeslot). |
| `rolling_window_slots` | `int` | No | `24` | `>= 1` | Slot count used for comfort rolling-window ON accounting. |
| `battery_entities` | `list[BatteryEntityParams]` | Yes | - | May be empty | Main controllable storage entities. |
| `comfort_entities` | `list[ComfortEntityParams]` | Yes | - | May be empty | Required-but-shiftable comfort entities. |
| `optional_entities` | `list[OptionalEntityParams]` | No | `[]` | Fully validated for feasibility | Advisory start-time options only. |
| `state` | `str \| None` | No | `None` | Valid base64 JSON object, version `v=1` | Opaque carry-over state from previous call. |

\* `solar_input_kwh` and `usage_kwh` have defaults in the model definition, but are effectively required because validation enforces exact length match to `price_per_kwh`.

Additional global constraints:

- Unknown fields are rejected (`extra="forbid"`).
- Entity names must be unique across battery + comfort + optional groups (case-insensitive).

## Battery entity model (`BatteryEntityParams`)

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
| `can_charge_from` | `int` | No | `2` | `0`, `1`, `2`, `3` | Charge-source flags (`1=GRID`, `2=PV`, `3=GRID|PV`; `0` means charging disabled). |

Curve unit note:

- `charge_curve_kwh` and `discharge_curve_kwh` are **kWh per slot**, not kW.
- Example: a `2 kW` unit can move at most `0.5 kWh` in a `15-minute` slot (`2 * 0.25 = 0.5`).
- Efficiency semantics:
  - charging: `SoC gain = charged_energy * charge_efficiency`
  - discharging: `SoC drop = delivered_energy / discharge_efficiency`

### Battery target model (`BatteryTargetParams`)

| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `timeslot` | `int` | Yes | - | `>= 0`, `< horizon` | Deadline timeslot, interpreted as **by end of timeslot**. |
| `soc_kwh` | `float` | Yes | - | `0..capacity_kwh` | Desired SoC level at deadline (kWh). |
| `mode` | `str` | No | `"at_least"` | `at_least`, `at_most`, `exact` | `at_least`: charge (if needed) to be at/above target by deadline. `at_most`: discharge (if needed) to be at/below target. `exact`: charge and discharge as needed to land on target (within tolerance). |
| `tolerance_kwh` | `float` | No | `0.0` | `>= 0` | Allowed kWh tolerance around the target level. |

## Comfort entity model (`ComfortEntityParams`)

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

## Optional entity model (`OptionalEntityParams`)

Optional entities provide advisory start-time suggestions and do not change the optimized battery/comfort schedule.

| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `name` | `str` | Yes | - | Non-empty | Unique globally. |
| `duration_timeslots` | `int` | Yes | - | `> 0`, `<= horizon` | Duration of this optional run. |
| `start_after_timeslot` | `int` | No | `0` | `>= 0`, `< start_before_timeslot` | Earliest allowed start (inclusive). |
| `start_before_timeslot` | `int` | Yes | - | `>= 1`, `<= horizon` | Latest boundary (exclusive). |
| `energy_kwh` | `float \| list[float]` | Yes | - | finite, `>= 0`; list non-empty and `len <= duration_timeslots` | Scalar is spread uniformly. List is spread step-wise to full duration. |
| `options` | `int` | No | `3` | `> 0`, feasible within window/gap rules | Exact number of options returned. |
| `min_option_gap_timeslots` | `int` | No | `0` | `>= 0` | Minimum spacing between suggested starts. |
| `allow_overlapping_options` | `bool` | No | `false` | - | If false, effective spacing is at least `duration_timeslots`. |

Feasibility is validated up front. If requested `options` cannot fit the search window under spacing rules, validation fails.

## `energy_kwh` normalization behavior

Before optimization starts, optional entity energy is normalized:

- Scalar: `energy_kwh = 4.0` and `duration_timeslots = 8` becomes `[0.5, ..., 0.5]` (8 timeslots).
- List shorter than duration: values are spread over the duration in contiguous segments.
  - Example: `[1, 2]` over `10` timeslots -> first 5 timeslots use `1`, next 5 use `2`.

This means calculation code always receives a full per-timeslot profile.

## Opaque state contract

`state` is an opaque base64 blob returned by one solve and accepted in the next.

- You should store and pass it back as-is.
- Do not parse or mutate it in client code.
- The optimizer may reuse overlap from prior solve data when it is compatible.

## Full request example (small)

```jsonc
{
  // 8 timeslots (for docs brevity). Real integrations usually use more.
  "price_per_kwh":      [0.34, 0.31, 0.28, 0.22, 0.18, 0.21, 0.30, 0.42],
  "solar_input_kwh": [0.0,  0.1,  0.5,  1.0,  0.8,  0.3,  0.0,  0.0],
  "usage_kwh":       [1.2,  1.1,  1.0,  0.9,  1.0,  1.2,  1.3,  1.4],
  "rolling_window_slots": 96,

  // Controllable storage: can charge/discharge/hold and absorb PV surplus.
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

## Response shape (summary)

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

### Notes on `entities` and `optional_entity_options`

- `entities` is the actual optimized schedule.
- Battery schedule points include `charge_source` flags (`0=none`, `1=GRID`, `2=PV`, `3=GRID|PV`) alongside `state` and `level`.
- `optional_entity_options` is advisory and computed on top of that baseline.
- Optional entities do not affect each other and do not modify `entities`.

### `projections` fields

- `baseline_cost`: `sum(price_per_kwh[t] * usage_kwh[t])` across the horizon.
- `projected_cost`: projected import cost for the optimized schedule.
- `projected_savings_cost`: `baseline_cost - projected_cost`.
- `projected_savings_pct`: `(projected_savings_cost / baseline_cost) * 100`, or `0` when baseline is `0`.
- `per_slot`: list with one object per timeslot (same index/order as input arrays), each containing:
  - `baseline_cost`
  - `projected_cost`
  - `projected_savings_cost`
  - `projected_savings_pct`

### `suboptimal_reasons` keys

Current machine-readable keys include:

- `battery_min_unmet`: at least one battery dropped below its configured `minimum_kwh` in the solved schedule.
- `battery_target_unmet`: a battery `target` constraint (`at_least`/`at_most`/`exact`) was not met at its target timeslot.
- `comfort_target_unmet`: a comfort entity did not achieve its required ON slots within the rolling window.
- `comfort_max_off_unmet`: a comfort entity exceeded its configured `max_consecutive_off_slots`.

## Validation behavior

Validation happens before optimization starts.

- Invalid input raises an exception immediately (Pydantic validation error).
- Error messages identify the offending field and why it failed.
- Unknown fields are rejected.

This pre-validation/normalization design ensures the calculation phase operates on strictly shaped, trusted input.
