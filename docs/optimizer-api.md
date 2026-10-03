# WattPlan Optimizer API

This document describes the direct Python API for the optimizer packaged inside this repository.

- **Package:** `custom_components.wattplan.optimizer`
- **Primary Module:** `custom_components.wattplan.optimizer.mpc_power_optimizer`
- **Primary Function:** `optimize(params: OptimizationParams) -> dict`

The optimizer is model-predictive-control (MPC) based.

Import and export tariffs retain their supplied signed values in the battery solve. Negative import prices can therefore make feasible grid charging economically beneficial. Comfort timing starts from a constraint-driven schedule and may be improved by bounded whole-schedule placement outside the battery MILP. Direction constraints prevent a slot from importing and exporting simultaneously, prevent a battery from charging and discharging simultaneously, limit grid-sourced charging to actual grid import, and reserve PV charging/export for physical PV surplus after household and scheduled comfort demand.

The model does not export battery energy, add an export-first battery mode, curtail forecast PV, or assign a terminal value to energy remaining beyond the supplied horizon. A `grid_charge` schedule state is an on/off inverter policy, not a throttled power setpoint: an enabled slot uses the configured full charging rate, with PV supplying available surplus first and the battery's remaining capacity limiting the final input. The optimizer prices this executable charge before choosing the policy. A capacity-limited top-up is only considered when at least half the configured slot energy can fit (with a small numeric feasibility tolerance); smaller top-ups are OFF. Charging may stop early at capacity without commanding a partial rate.

The continuous model can still prefer exporting PV over storing it when stored energy has no modeled future value, even though an empty PV-capable battery receiving the coarse `self_consume` policy would normally charge from surplus first. PV charging under `self_consume` is ordinary inverter behavior, not a separate controllable action in this policy model.

If you are using WattPlan through the Home Assistant integration, see [optimizer-profiles.md](optimizer-profiles.md) for the user-facing `Aggressive`, `Balanced`, and `Conservative` presets. Those profiles are integration-level presets that map onto the numeric optimizer fields documented here. New Home Assistant setups use a 12-hour economic lookahead. Existing setups are migrated to the historical 22-slot lookahead and may edit the displayed duration in General settings.

## Time Resolution (Timeslots)
All time-indexed fields use **timeslots**.
- A timeslot is one fixed slice of time at your chosen resolution (for example, 15 minutes).
- Every array index corresponds to one timeslot.
- The API does not enforce a specific minutes-per-timeslot value; the caller is responsible for consistent input resolution.
- `lookahead_slots` is also expressed in this same resolution. The Home Assistant integration converts its hour-based economic lookahead setting to slots (`hours * 60 / slot_minutes`).

## Conceptual Model
The solve combines three kinds of entities:
- **Battery Entities:** Controllable storage with modeled charge/discharge flows and serialized policy states for inverter control.
- **Comfort Entities:** Required comfort loads, such as heating or hot water. A deterministic feasible baseline is followed by bounded cost-aware relocation of existing ON runs.
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
| `solar_input_kwh` | `list[float]` | Yes | - | Must match `len(grid_import_price_per_kwh)`, finite, `>= 0` | Per-timeslot PV forecast (kWh per timeslot). |
| `usage_kwh` | `list[float]` | Yes | - | Must match `len(grid_import_price_per_kwh)`, finite, `>= 0` | Per-timeslot base load forecast (kWh per timeslot). |
| `rolling_window_slots` | `int` | No | `24` | `>= 1` | Slot count used for comfort rolling-window ON accounting. |
| `lookahead_slots` | `int` | No | `22` | `2..672` | Maximum future slots considered when selecting each current action. Direct API callers that omit it retain the historical 22-slot behavior. The full supplied horizon is still returned. |
| `throughput_cost_per_kwh` | `float` | No | `0.0` | Finite, `>= 0` | Heuristic objective weight per kWh of charge/discharge throughput. It discourages cycling but is not a monetary battery-wear estimate. |
| `action_deadband_kwh` | `float` | No | `0.0` | Finite, `>= 0` | Battery flow is constrained to zero or at least this inclusive threshold. |
| `mode_switch_cost` | `float` | No | `0.0` | Finite, `>= 0` | Heuristic objective weight that discourages changing modeled battery behavior. It is not a monetary switching or wear estimate. |
| `infer_battery_preserve_policy` | `bool` | No | `true` | - | Enables the model-backed counterfactual used to emit `preserve` battery policy states. When disabled, all `battery_preserve` booleans are `false` and non-grid-charging battery slots fall back to `self_consume`. |
| `solver_time_limit_seconds` | `float` | No | `20` | `> 0`, `<= 3600` | Wall-clock limit for each MILP solve. A solve stopped by the limit is used when HiGHS holds a feasible solution (logged as a warning) and fails otherwise. The integration additionally bounds the whole optimizer call by the slot length (at least 60 seconds) and reports a timeout as a `planner_execution` error. |
| `battery_entities` | `list[BatteryEntityParams]` | Yes | - | May be empty | Main controllable storage entities. |
| `comfort_entities` | `list[ComfortEntityParams]` | Yes | - | May be empty | Required comfort entities with bounded cost-aware placement. |
| `optional_entities` | `list[OptionalEntityParams]` | No | `[]` | Fully validated for feasibility | Advisory start-time options only. |
| `state` | `str \| None` | No | `None` | Valid base64 JSON object, version `v=1` | Opaque carry-over state from previous call. |
| `plan_start` | timezone-aware `datetime` or ISO datetime string | No | `None` | Must include a timezone; normalized to UTC | Start of forecast slot zero. Enables clock-aligned prefix refresh. The integration supplies its aligned source-window start. |
| `slot_minutes` | `int` | No | `15` | `1..1440` | Slot duration used to align timed requests and carry commitments. |

`solar_input_kwh` and `usage_kwh` are required in practice: the model defaults them to empty lists, but validation rejects any length other than `len(grid_import_price_per_kwh)`, so omitting them fails validation. Direct callers pass an all-zero list to model "no PV" or "no base load". The Home Assistant integration synthesizes these series from its configured sources before calling the optimizer.

**Additional Global Constraints:**
- Unknown fields are rejected (`extra="forbid"`).
- Entity names must be unique across battery + comfort + optional groups (case-insensitive).
- Comfort minimum ON/OFF durations must each be shorter than `min(lookahead_slots, horizon)`.

## Battery Entity Model (`BatteryEntityParams`)
| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `name` | `str` | Yes | - | Non-empty | Unique globally. |
| `initial_kwh` | `float` | Yes | - | Finite, `0..capacity_kwh` | Initial state of charge (kWh). |
| `target` | `BatteryEntityParams.TargetParams \| None` | No | `null` | If set: `timeslot < horizon` | Optional deadline target constraint. |
| `minimum_kwh` | `float` | Yes | - | Finite, `0..capacity_kwh` | Minimum desired state (kWh). Soft floor: the solve penalizes, rather than forbids, ending a slot below it. A battery that starts below it is planned to recover as fast as its limits allow, and the plan reports `battery_min_unmet` for as long as the planned SoC stays below the floor. |
| `capacity_kwh` | `float` | Yes | - | Finite, `> 0` | Storage capacity (kWh). |
| `charge_curve_kwh` | `list[float]` | Yes | - | Non-empty, finite, `>= 0` | Chargeable energy per slot by SoC curve (kWh per slot). |
| `discharge_curve_kwh` | `list[float]` | Yes | - | Non-empty, finite, `>= 0` | Dischargeable energy per slot by SoC curve (kWh per slot). |
| `charge_efficiency` | `float` | No | `1.0` | Finite, `(0, 1]` | Fraction of charged energy that increases SoC. |
| `discharge_efficiency` | `float` | No | `1.0` | Finite, `(0, 1]` | Fraction of discharged SoC energy delivered to load. |
| `prefer_pv_surplus_charging` | `bool` | No | `false` | - | Internal/deferred hint for routing PV surplus into this battery. This is not exposed as a battery action state and should not be used as a user-facing control contract. |
| `can_charge_from` | `int` | No | `2` | `0`, `1`, `2`, `3` | Allowed charging-ingress flags (`1=GRID`, `2=PV`, `3`=both; `0` means charging disabled). |

**Curve Unit Note:**
- `charge_curve_kwh` and `discharge_curve_kwh` are **kWh per slot**, not kW.
- Example: a `2 kW` unit can move at most `0.5 kWh` in a `15-minute` slot (`2 * 0.25 = 0.5`).
- **Efficiency Semantics:**
  - Charging: `SoC gain = charged_energy * charge_efficiency`
  - Discharging: `SoC drop = delivered_energy / discharge_efficiency`
- **Known limitations of the power curves and targets:**
  - Each MPC solve reads the charge and discharge limits once, at the battery's SoC when that solve starts, and applies them to every slot in its lookahead. A curve that tapers with SoC is therefore exact for the slot being decided but only an approximation for later slots in the window (a battery that will charge into the taper is planned as if it kept its current rate). Each following solve starts from the real SoC and corrects this, and applied controls are always clipped to the true curve.
  - A `target` becomes a constraint only once its deadline falls inside the lookahead window. A target further away is invisible to the solve until it enters the window, and by then a large or slow-to-reach SoC may no longer be reachable; the plan then reports `battery_target_unmet`. Use a `lookahead_slots` large enough to cover the time needed to reach the target, or set the target earlier.
  - Energy left in a battery at the end of the lookahead (or the horizon) has no terminal value, so the plan does not hold charge for value beyond what it can see.

### Battery Target Model (`BatteryEntityParams.TargetParams`)
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
| `on_slots_last_rolling_window` | `int` | No | `0` | `>= 0`, `<= rolling_window_slots` | Deprecated aggregate retained for compatibility when ordered history is unavailable. |
| `on_history` | `list[bool] \| null` | No | `null` | Exactly `rolling_window_slots - 1` values when present | Ordered observed states, oldest first, immediately before forecast slot 0. Used by the deterministic scheduler to enforce rolling demand. |
| `off_streak_slots_now` | `int` | Yes | - | `>= 0` | Current OFF streak (slots). |
| `measured_power_source` | `str \| null` | No | `null` | - | Optional source of observed power telemetry. |
| `recent_avg_on_power_kw` | `float \| null` | No | `null` | Finite, `> 0` | Optional observed ON power average. |

## Optional Entity Model (`OptionalEntityParams`)
Optional entities provide advisory start-time suggestions and do not change the published battery/comfort schedule. Each candidate is valued by replaying the supplied forecast horizon from the original battery state under the unchanged published battery modes and comfort schedule. The replay applies the candidate load to physical PV, battery, import, and export flows, including later tariff cost caused by changed battery state, and compares it with the same fixed-policy replay without the load. Signed import and export tariffs are used in both replays. Heuristic throughput and mode-switch weights are not added to replay costs or used to rank optional-load starts.
| Field | Type | Required | Default | Constraints | Notes |
|---|---|---:|---|---|---|
| `name` | `str` | Yes | - | Non-empty | Unique globally. |
| `duration_timeslots` | `int` | Yes | - | `> 0`, `<= horizon` | Duration of this optional run. |
| `start_after_timeslot` | `int` | No | `0` | `>= 0`, `< start_before_timeslot` | Earliest allowed start (inclusive). |
| `start_before_timeslot` | `int` | Yes | - | `>= 1`, `> start_after_timeslot` | Latest boundary (exclusive). The latest start is `start_before_timeslot - duration_timeslots`. |
| `energy_kwh` | `float \| list[float]` | Yes | - | Finite, `>= 0`; list non-empty and `len <= duration_timeslots` | A scalar is the **total** energy of the run, spread uniformly over `duration_timeslots`. A list is a **per-slot** energy profile, not a total: if shorter than the duration it is stretched step-wise (each entry covers a proportional block of slots and keeps its per-slot value), so the run's total energy is not preserved. For example `[0.4, 1.2]` over four slots draws `0.4, 0.4, 1.2, 1.2` kWh (3.2 kWh in all). |
| `options` | `int` | No | `3` | `> 0`, feasible within window/gap rules | Exact number of options returned. |
| `min_option_gap_timeslots` | `int` | No | `0` | `>= 0` | Minimum spacing between suggested starts. |
| `allow_overlapping_options` | `bool` | No | `false` | - | If false, effective spacing is at least `duration_timeslots`. |

**Feasibility is validated up front.** If requested `options` cannot fit the search window under spacing rules, validation fails.

## Opaque State Contract
`state` is an opaque base64 blob returned by one solve and accepted in the next.
- You should store and pass it back as-is.
- Do not parse or mutate it in client code. Internally, trajectory data that can be recomputed (the per-slot comfort history) is stored only as its starting row and rebuilt on decode, which keeps the blob small; blobs from earlier versions that stored the full history still decode.
- With `plan_start`, the optimizer normally computes a full plan, then refreshes the first eight battery actions on each of the next three consecutive slot advances. The fourth advance computes a new full plan. At 15-minute resolution this is full at 00:00, prefix refresh at 00:15/00:30/00:45, and full at 01:00. The configured economic lookahead is unchanged for every fresh decision; an eight-slot refresh does not mean an eight-slot lookahead.
- Every response still covers the complete forecast. The reused battery tail is applied to current forecast load/PV and newly calculated SOC. Comfort is regenerated from current rolling history and minimum-on/off commitments, then bounded placement may improve its timing before it is fixed as demand. Battery policies are retained even when physical limits produce zero flow.
- A same-slot refresh uses a zero shift and does not advance the cadence clock. Skipped, reversed, unaligned, or differently sized windows, changed resolution/configuration, and incompatible state cause a full plan. Optional recommendation changes do not invalidate the battery/comfort cadence. Target compatibility uses the absolute end-of-slot deadline.
- First-slot comfort commitments use expiry times on the planning timeline, checked against observed modes and compatible durations. Ordered observed history can also establish an ongoing commitment, including after a skipped refresh. Predicted future transitions are not assumed to have been actuated.
- The deterministic comfort scheduler recalculates the entire baseline comfort schedule on every call. Bounded placement preserves runtime, rolling windows, minimum runs, maximum OFF time, and current locks. Invalid baselines are reported explicitly and are neither priced nor moved. If placement changes demand, battery control arrays are not reused; at most one additional battery planning pass is allowed. A rebuilt plan is accepted only when actual tariff cost does not worsen and hard constraints remain valid.
- Saved state from earlier comfort scheduling implementations is invalidated for control reuse. The first request after the bounded-placement scheduler change fully replans, while retaining compatible observed/first-slot comfort commitments.
- Short horizons (nine slots or fewer) are fully planned. Requests without `plan_start` retain the legacy full-calculation/reuse path; the optimizer does not infer a new tick from call count. A timed state handed to an untimed request is fully replanned. Older opaque state remains accepted but cannot enable clock-based partial refresh.

Prefix refresh changes how often future battery decisions are optimized, not the forecast resolution. Comfort placement is bounded on full and prefix calls and may shift feasible existing runs toward lower whole-site tariff cost. Optional recommendations are recalculated against the final accepted comfort schedule and published battery policies; they are not a guarantee of equivalence to a newly optimized full policy horizon. Full calls and native solver outliers are still possible; the cadence is not a hard execution-time limit.

## Full Request Example (Small)
```jsonc
{
  // 8 timeslots (for docs brevity). Real integrations usually use more.
  "grid_import_price_per_kwh": [0.34, 0.31, 0.28, 0.22, 0.18, 0.21, 0.30, 0.42],
  "grid_export_price_per_kwh": [0.00, 0.00, 0.05, 0.08, 0.10, 0.08, 0.02, 0.00],
  "solar_input_kwh":       [0.0,  0.1,  0.5,  1.0,  0.8,  0.3,  0.0,  0.0],
  "usage_kwh":             [1.2,  1.1,  1.0,  0.9,  1.0,  1.2,  1.3,  1.4],
  "rolling_window_slots": 8,
  "lookahead_slots": 48,

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
      "on_history": [false, false, true, true, true, false, false],
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
| `fitness` | `float` | Internal objective score of the final schedule: net tariff cost plus constraint-violation and switching penalties. Lower is better; intended for diagnostics and comparison between plans of the same request, not as a monetary figure. |
| `avg_price` | `float` | Net tariff cost of the schedule (import cost minus export revenue) divided by grid-imported kWh. Export revenue lowers it, so it can be negative. A sentinel of `1000.0` is returned when the schedule imports no energy. |
| `projections` | `dict` | Projected cost/savings metrics for this schedule. |
| `overconstrained` | `bool` | Whether soft constraint violations were detected. |
| `suboptimal` | `bool` | `true` when one or more soft targets/limits were unmet. |
| `suboptimal_reasons` | `list[str]` | Machine-readable reason keys for suboptimal output. |
| `problems` | `list[str]` | Human-readable issue tags (if any). |
| `entities` | `list[dict]` | Battery/comfort schedules. |
| `optional_entity_options` | `list[dict]` | Advisory start options per optional entity. |
| `state` | `str` | Opaque base64 state for next call. |
| `cadence` | `dict`, timed requests only | Full/repair/fallback mode, reason, phase, optimized/replayed battery-slot counts, and provisional tail count. |
| `comfort_placement` | `dict`, comfort requests only | Bounded search status, cost mode, work counts, explicit violations, demand-change/replan accounting, actual baseline/final projected cost, and fallback reason. |
| `successful_solves` | `int` | Primary MPC solves performed in this call, including a baseline and at most one changed-demand rebuild; preserve probes are additional. No-battery plans use direct flow accounting and report zero. |
| `reused_steps` | `int` | Old battery schedule positions considered for reuse. Comfort is generated independently. |
| `energy_flows` | `dict` | Accepted physical energy accounting, with one entry per input slot. |

### `energy_flows` Fields
- `per_slot` follows input order and uses kWh per slot from the final accepted
  schedule, not a separate reconstruction or solve.
- Each item contains `load_kwh` (base load, scheduled comfort load and accepted
  battery charge/discharge), `base_load_kwh`, `comfort_load_kwh`,
  `grid_import_kwh`, `grid_export_kwh`, and `grid_charge_kwh`.
- Grid import/export are mutually exclusive in this accounting. Grid charging is
  included in both `load_kwh` and grid import when it cannot be met by PV.

### Battery Policy States
Battery schedule `state` values are inverter-control policies derived from the plan, not raw measured or forecast battery flows:

| State | Meaning |
| --- | --- |
| `preserve` | Prevent this battery from discharging. This saves stored energy when the optimizer shows that spending it now would make the plan worse or violate modeled constraints. PV charging may still be allowed by the user's inverter setup. |
| `self_consume` | Normal battery operation. Allow this battery to cover real load. Do not request grid charging. This is also the default when the model has no positive reason to preserve or grid-charge. |
| `grid_charge` | Request full feasible slot-rate charging for this battery and prevent the battery from being spent while doing so. PV surplus can provide part of the charge; the inverter stops early if capacity is reached. |

PV surplus charging is implicit/normal battery behavior, not a primary action state. PV export is site-level and multi-battery-sensitive, so a dedicated PV export policy is deferred to a future site-level design.

`grid_charge` is selected as an on/off decision in the MILP: charging while ON must use the full feasible slot rate, with capacity saturation and available PV reducing the grid contribution. A capacity-limited slot shorter than half the configured full slot is rejected, rather than relying on exact SoC equality near full. The action deadband prevents an otherwise PV-supplied ON slot from publishing a meaningless grid-charge request; it does not throttle charging. `preserve` is emitted from a model-backed counterfactual check when `infer_battery_preserve_policy` is enabled: when the optimizer chooses not to discharge a battery, WattPlan asks the same model whether forcing a small discharge from that battery for marginal unexpected load would be infeasible or make the objective worse than preserving the battery and importing that marginal energy. Modeled PV surplus is consumed first in that counterfactual, so PV export is not turned into a battery action state. If the forced-discharge alternative is worse, the slot is marked `preserve`; otherwise WattPlan emits `self_consume`. Forecast zero battery flow is not a preserve reason by itself.

The returned battery states remain policies rather than exact energy setpoints. A full-rate grid-charge choice is costed before publication, but real inverter limits, forecast error, and `self_consume`/`preserve` behavior can still differ from the model. WattPlan does not apply a blanket nonnegative-savings veto: mandatory battery targets and comfort requirements can legitimately cost more than the no-action baseline, and comparing policies safely requires an equivalent feasible reference with an explicit terminal-energy convention.

If `infer_battery_preserve_policy` is disabled, the `battery_preserve` boolean array is always `false`. In that mode, the schedule still emits `grid_charge` for modeled grid charging, but otherwise emits `self_consume` for battery slots.

### Notes on `entities` and `optional_entity_options`
- `entities` is the published schedule. On timed requests, battery points have `freshness` values `optimized`, `reprojected`, or `provisional`; comfort points are `scheduled` because their entire deterministic schedule is recalculated each time.
- Battery schedule points encode policy directly in `state`: `preserve`, `self_consume`, or `grid_charge`.
- `optional_entity_options` is advisory and computed on top of that baseline.
- Optional entities do not affect each other and do not modify `entities`.
- Each option's `incremental_cost` is its fixed-policy tariff replay cost minus the no-added-load fixed-policy tariff replay cost under the same signed tariffs. The replay can change hypothetical battery energy flows and later SoC, but it does not reoptimize or mutate the returned schedule or opaque state. Heuristic throughput and mode-switch weights are excluded.
- Alternatives are independent suggestions. Their costs do not assume that multiple options or optional loads run together.
- Ranking is optimal only among the configured candidate starts under the published fixed modes and supplied forecast horizon. No terminal battery value beyond that horizon is invented.

### `projections` Fields
- All projection cost and savings fields are tariff-only monetary estimates. They use supplied signed import/export tariffs and exclude the heuristic `throughput_cost_per_kwh` and `mode_switch_cost` objective weights. They therefore do not estimate battery degradation, switching wear, or total ownership cost.
- `baseline_cost`: Baseline net tariff cost across the horizon, including export revenue when `grid_export_price_per_kwh` is provided.
- `projected_cost`: Projected net tariff cost for the optimized schedule (`grid imports - grid export revenue`).
- `projected_savings_cost`: `baseline_cost - projected_cost`.
- `projected_savings_pct`: `(1 - projected_cost / baseline_cost) * 100`, which is equivalent to `(projected_savings_cost / baseline_cost) * 100` when `baseline_cost > 0`. The optimizer still emits the raw numeric result; Home Assistant sensors may choose not to expose extreme values as entity state.
- `per_slot`: List with one object per timeslot (same index/order as input arrays), each containing:
  - `baseline_cost`
  - `projected_cost`
  - `projected_savings_cost`
  - `projected_savings_pct`

### `suboptimal_reasons` Keys
Current machine-readable keys include:
- `battery_min_unmet`: At least one battery ends a slot below its configured `minimum_kwh` in the solved schedule. The minimum is a soft floor, so this is reported, not a solve failure, when a battery starts below its minimum and cannot recover within the first slot(s), or when other constraints force it under.
- `battery_target_unmet`: A battery `target` constraint (`at_least`/`at_most`/`exact`) was not met at its target timeslot.
- `comfort_target_unmet`: A comfort entity did not achieve its required ON slots within the rolling window.
- `comfort_history_unavailable`: Ordered pre-forecast history was unavailable. The optimizer used only aggregate credit guaranteed for every possible ordering, so it does not claim history-dependent comfort validity.
- `comfort_max_off_unmet`: A comfort entity exceeded its configured `max_consecutive_off_slots`.
- `comfort_min_run_unmet`: An inherited comfort lock could not be honored, or a forecast transition could not complete its configured minimum ON/OFF run within the horizon.

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
