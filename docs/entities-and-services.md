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
| `sensor.<setup_slug>_status` | Current planner status such as `planned`, `suboptimal`, or `error`. |
| `sensor.<setup_slug>_last_run` | Timestamp of the last successful planning cycle. |
| `sensor.<setup_slug>_next_run` | Timestamp of the next scheduled planning cycle. |
| `sensor.<setup_slug>_last_run_duration` | Duration of the last planning cycle in milliseconds. |
| `sensor.<setup_slug>_projected_cost_savings` | Horizon-wide cost savings for the current plan. |
| `sensor.<setup_slug>_projected_savings_percentage` | Horizon-wide savings percentage for the current plan. |
| `sensor.<setup_slug>_projected_cost_savings_next_interval` | Disabled by default. Savings for the next planner interval only. |
| `sensor.<setup_slug>_projected_savings_percentage_next_interval` | Disabled by default. Savings percentage for the next planner interval only. |
| `sensor.<setup_slug>_plan_details` | Disabled by default. Raw planner-detail payload at WattPlan's configured slot size. |
| `sensor.<setup_slug>_plan_details_hourly` | Disabled by default. The same planner details, aggregated to hourly buckets. |
| `sensor.<setup_slug>_usage_forecast` | Present when the built-in usage source is configured. Exposes the generated usage forecast. |

## Battery Entities

These exist once per configured battery:

| Entity | Purpose |
| --- | --- |
| `sensor.<setup_slug>_<battery_name>_action` | Current planned action: `charge`, `discharge`, or `hold`. WattPlan updates this entity on its planning schedule so **your own automation can translate the planned action into a real inverter or battery command**. Includes attributes such as `next_action` and `next_action_timestamp`. |
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

WattPlan exposes the following services:

| Service | Purpose |
| --- | --- |
| `wattplan.set_target` | Set a battery target SoC that the optimizer should reach by a deadline. |
| `wattplan.clear_target` | Remove the active target for one or more batteries. |
| `wattplan.run_optimize_now` | Trigger a new planning cycle immediately. |
| `wattplan.run_plan_now` | Emit actions immediately from the current plan. |
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

### `wattplan.run_plan_now`

Emit actions immediately from the current plan.

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
