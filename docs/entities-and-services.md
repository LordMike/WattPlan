# Entities and Services

This page describes the Home Assistant entities and services that WattPlan exposes after setup.

Entity names below use placeholders:

- `<setup_slug>` for the WattPlan setup name slug
- `<battery_name>` for a configured battery name
- `<comfort_name>` for a configured comfort load name
- `<optional_name>` for a configured optional load name

## Entry-level Entities

These exist once per WattPlan setup:

| Entity | Purpose |
| --- | --- |
| `sensor.<setup_slug>_status` | Current integration health: `ok`, `degraded`, or `failed`. Includes attributes such as `reason_codes`, `affected_sources`, `is_stale`, and `has_usable_plan`. |
| `sensor.<setup_slug>_status_message` | Human-readable summary of the current integration health. |
| `sensor.<setup_slug>_import_price_status` | Import price source health: `ok`, `degraded`, or `failed`. |
| `sensor.<setup_slug>_usage_status` | Present when usage is configured. Usage source health: `ok`, `degraded`, or `failed`. |
| `sensor.<setup_slug>_export_price_status` | Present when export price is configured. Export price source health: `ok`, `degraded`, or `failed`. |
| `sensor.<setup_slug>_pv_status` | Present when PV is configured. PV source health: `ok`, `degraded`, or `failed`. |
| `sensor.<setup_slug>_last_run` | Timestamp of the last successful optimize (plan calculation) cycle. |
| `sensor.<setup_slug>_next_run` | Timestamp of the next scheduled planning cycle. |
| `sensor.<setup_slug>_last_run_duration` | Duration of the last optimize cycle in milliseconds. |
| `sensor.<setup_slug>_plan_details` | Disabled by default. Raw planner-detail payload at WattPlan's configured slot size. |
| `sensor.<setup_slug>_plan_details_hourly` | Disabled by default. The same planner details, aggregated to hourly buckets. |
| `sensor.<setup_slug>_usage_forecast` | Present when the built-in usage source is configured. Exposes the generated usage forecast. |

When `sensor.<setup_slug>_status` is `failed`, plan-dependent entities such as action sensors, plan details, and usage forecast become unavailable rather than continuing to expose stale plan data.

## Historical Cost Entities

Historical cost tracking is opt-in from the WattPlan options flow. It uses Home Assistant Store storage owned by WattPlan, not recorder backfill or SQLite. The tracker samples completed slots from configured cumulative kWh meters and retains 60 local days.

Enabled by default when historical tracking is enabled:

| Entity | Purpose |
| --- | --- |
| `sensor.<setup_slug>_historical_actual_cost_today` | Measured grid import/export cost for today. |
| `sensor.<setup_slug>_historical_no_battery_cost_today` | Simulated cost for today if the site had usage and PV but no batteries. |
| `sensor.<setup_slug>_historical_self_consumption_cost_today` | Simulated cost for today with simple PV-first battery self-consumption. |
| `sensor.<setup_slug>_historical_savings_vs_no_battery_today` | No-battery cost minus actual cost for today. |
| `sensor.<setup_slug>_historical_savings_vs_self_consumption_today` | Self-consumption cost minus actual cost for today. |

Disabled by default:

| Entity | Purpose |
| --- | --- |
| `sensor.<setup_slug>_historical_actual_cost_this_month` | Measured grid import/export cost for the current local month. |
| `sensor.<setup_slug>_historical_no_battery_cost_this_month` | No-battery simulated cost for the current local month. |
| `sensor.<setup_slug>_historical_self_consumption_cost_this_month` | Self-consumption simulated cost for the current local month. |
| `sensor.<setup_slug>_historical_savings_vs_no_battery_this_month` | No-battery savings for the current local month. |
| `sensor.<setup_slug>_historical_savings_vs_self_consumption_this_month` | Self-consumption savings for the current local month. |

Historical sensors use the Home Assistant currency as their unit and expose `tracking_started_at`, `last_complete_slot`, `slots`, `missing_slots`, `period_start`, `period_end`, `scenario`, and `retention_days` attributes. Missing meters, meter resets, missing prices, and skipped slots are counted as missing slots instead of being spread across price intervals.

## Battery Entities

These exist once per configured battery:

| Entity | Purpose |
| --- | --- |
| `sensor.<setup_slug>_<battery_name>_action` | Current battery-control policy: `preserve`, `self_consume`, or `grid_charge`. WattPlan updates this entity on its planning schedule so **your own automation can translate the policy into a real inverter or battery command**. The state is a policy derived from the plan, not a raw forecast battery-flow value. See [extras.md](extras.md#real-life-examples) for automation examples. |
| `sensor.<setup_slug>_<battery_name>_target` | User-supplied target SoC in kWh. Includes a `by` attribute with the requested deadline and returns `unknown` when no active target is set. |

## Comfort Load Entities

These exist once per configured comfort load:

| Entity | Purpose |
| --- | --- |
| `sensor.<setup_slug>_<comfort_name>_action` | Current planned action: `on` or `off`. WattPlan updates this entity on its planning schedule so **your own automation can translate the planned action into the real device command**. Includes attributes describing the next action change. |

## Optional Load Entities

These exist once per configured optional load:

| Entity | Purpose |
| --- | --- |
| `sensor.<setup_slug>_<optional_name>_next_start_option` | First suggested start time. |
| `sensor.<setup_slug>_<optional_name>_next_end_option` | End time of the first suggested option. |
| `sensor.<setup_slug>_<optional_name>_option_1_start` | Start time for option 1. |
| `sensor.<setup_slug>_<optional_name>_option_2_start` | Start time for option 2. |

Additional `option_N_start` entities appear when more options are configured.

## Services

WattPlan operates in two distinct steps:

1. **Optimize** (`run_optimize_now`) — runs the planner and calculates a new plan. This is the slow step that reads all energy sources and solves the optimization. Run it as often as you want fresh plans, but it can be infrequent if the optimizer is slow.
2. **Refresh** (`refresh_sensors`) — reads the already-calculated plan and pushes the current slot's actions to HA sensor entities. This is fast and can be called frequently to keep action sensors up to date without re-running the optimizer.

WattPlan exposes the following services:

| Service | Purpose |
| --- | --- |
| `wattplan.set_target` | Set a battery target SoC that the optimizer should reach by a deadline. |
| `wattplan.clear_target` | Remove the active target for one or more batteries. |
| `wattplan.run_optimize_now` | Trigger a new planning (optimize) cycle immediately. |
| `wattplan.refresh_sensors` | Re-emit the current plan's actions to HA sensor entities immediately. |
| `wattplan.export_planner_input` | Rebuild and return the exact planner input for one WattPlan setup. |
| `wattplan.export_usage_forecast_debug` | Return raw debug data for the built-in usage forecast source. |

### `wattplan.set_target`

Set a battery target SoC that the optimizer should reach by a deadline.

Fields:

- `battery`
  - Optional WattPlan battery name.
- `entity_id`
  - Optional WattPlan target or action entity selection.
- `device_id`
  - Optional WattPlan device selection.
- `soc_kwh`
  - Required target state of charge in kWh.
- `reach_at`
  - Required deadline as a Home Assistant datetime.
- `entry_id`
  - Optional filter for a single WattPlan setup.

Example:

```yaml
service: wattplan.set_target
data:
  battery: <battery_name>
  soc_kwh: 8.0
  reach_at: "2026-03-09T00:30:00+01:00"
```

### `wattplan.clear_target`

Remove the active target for one or more batteries.

Fields:

- `battery`
  - Optional WattPlan battery name.
- `entity_id`
  - Optional WattPlan target or action entity selection.
- `device_id`
  - Optional WattPlan device selection.
- `entry_id`
  - Optional filter for a single WattPlan setup.

Example:

```yaml
service: wattplan.clear_target
data:
  battery: <battery_name>
```

### `wattplan.run_optimize_now`

Trigger a new planning cycle immediately.

Fields:

- `name`
  - Optional setup title filter.
- `entry_id`
  - Optional config entry filter.

### `wattplan.refresh_sensors`

Re-emit the current plan's actions to HA sensor entities immediately. Does not recalculate the plan — use `run_optimize_now` for that.

Fields:

- `name`
  - Optional setup title filter.
- `entry_id`
  - Optional config entry filter.

### `wattplan.export_planner_input`

Rebuild and return the exact planner input for one WattPlan setup.

Fields:

- `name`
  - Optional setup title filter.
- `entry_id`
  - Optional config entry filter.
- `as_json`
  - Return compact JSON instead of structured service data.

### `wattplan.export_usage_forecast_debug`

Return raw debug data for the built-in usage forecast source.

Fields:

- `name`
  - Optional setup title filter.
- `entry_id`
  - Optional config entry filter.
- `as_json`
  - Return compact JSON instead of structured service data.

## Repairs Issues

WattPlan can raise Home Assistant Repairs issues when a configured source is not usable for planning.

| Issue shown in Repairs | When it appears | What it means |
| --- | --- | --- |
| `<Source> forecast is unavailable for <setup_name>` | A price, export price, usage, or PV source throws an exception or returns no data at all. | WattPlan could not get fresh data from the configured source. |
| `<Source> forecast does not cover the horizon for <setup_name>` | A source returns some data, but after normal normalization and fill behavior it still does not cover the planning horizon. | WattPlan got data, but not enough to plan the full requested window. |

These issues are emitted per source type:

- price forecast
- export price forecast
- usage forecast
- solar forecast

### `<Source> forecast is unavailable for <setup_name>`

This issue means the source is currently not producing any usable fresh data.

Typical causes:

- the selected entity no longer exists
- the selected service call fails
- an energy provider integration returns no forecast
- the upstream integration is temporarily unavailable

Planner consequence:

- price or usage source unavailable:
  - planning will stop once any last successful source data is no longer usable
- export price source unavailable:
  - planning continues, but exported power is treated as having zero value
- solar source unavailable:
  - planning can continue for a while using the last successful solar data, but later plans will lose solar input once that data is no longer usable

User action:

- review whether the configured source is functioning
- inspect the source entity, service, or provider integration
- wait for the upstream integration to recover if the issue is temporary

### `source_incomplete`

This issue means the source is returning data, but not enough to cover the full planning horizon.

Typical causes:

- the upstream provider only returns a short forecast window
- a today/tomorrow source is only returning one side
- the source has gaps that current fixup settings do not fill

Planner consequence:

- price or usage source incomplete:
  - planning will stop once the remaining usable data is exhausted
- export price source incomplete:
  - planning continues, but exported power is treated as having zero value for the missing period
- solar source incomplete:
  - planning can continue for a while, but later parts of the plan will lose solar input once the remaining usable data runs out

User action:

- review whether the configured source is returning enough data for the chosen planning horizon
- inspect advanced source settings such as fixup profile, alignment, and gap handling
- use the Repairs submit action if offered to apply WattPlan's recommended horizon-filling defaults
