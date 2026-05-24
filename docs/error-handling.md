# Error Handling

WattPlan exposes its runtime health through one main sensor:

- `sensor.<setup_slug>_status`

This is the first place to look when you want to know whether the planner is healthy right now.

## Main Health Sensor

`sensor.<setup_slug>_status` can be:

- `ok`
- `degraded`
- `failed`

It also exposes attributes such as:

- `reason_codes`
- `reason_summary`
- `affected_sources`
- `is_stale`
- `has_usable_plan`
- `plan_created_at`
- `expires_at`

`plan_created_at` is when the active snapshot was created. On the overall status sensor, `expires_at` is the end of the current usable plan coverage from the optimizer horizon. If planning fails and WattPlan keeps using a previous snapshot, `expires_at` remains the retained plan's coverage end. When the active or retained plan passes that time, the overall status becomes `failed`, `is_stale` becomes `true`, and `has_usable_plan` becomes `false`.

### `ok`

`ok` means WattPlan has a usable current plan and nothing important is reducing confidence in that plan.

In practice, this means:

- required source data is available
- planning succeeded
- the plan is current enough to trust

### `degraded`

`degraded` means WattPlan still has a usable plan, but something is wrong enough that you should treat the plan with less confidence.

Typical examples:

- a non-critical source such as PV or export price is unavailable
- a critical source is temporarily backed by stale cached data
- the optimizer solved the plan with reduced confidence
- planning failed, but a previous plan is retained and still covers the current time

In practice, `degraded` means:

- your plan still exists
- WattPlan is still working
- but the plan may be missing some information or be based on fallback behavior

This is the yellow state. Things are not fully healthy, but they are not fully broken either.

### `failed`

`failed` means WattPlan does not currently have a usable plan.

Typical examples:

- import price failed and no usable fallback remains
- configured usage forecast failed and no usable fallback remains
- planning failed entirely
- the active or retained plan no longer covers the current time
- coordinator state has gone stale and the plan can no longer be trusted

When this happens, plan-dependent entities become unavailable instead of continuing to show old plan values.

## Per-Source Health Sensors

WattPlan also exposes one status sensor per configured source:

- `sensor.<setup_slug>_import_price_status`
- `sensor.<setup_slug>_usage_status`
- `sensor.<setup_slug>_export_price_status`
- `sensor.<setup_slug>_pv_status`

Each source sensor also uses:

- `ok`
- `degraded`
- `failed`

These help answer which source is causing the overall status to change.

On source status sensors, `expires_at` describes the coverage end for that source's data or stale fallback. Source fallback staleness can degrade a still-usable plan. Overall plan expiry is separate: once the plan coverage itself has passed, the main status sensor reports `failed`.

## Repairs vs Status Sensors

Home Assistant Repairs issues are still used for actionable source problems.

Use them for:

- repair suggestions
- source-specific troubleshooting
- fixup recommendations

Use the status sensors for:

- current runtime health
- dashboard visibility
- automation conditions

In short:

- Repairs tell you what needs attention
- status sensors tell you whether WattPlan is still usable right now

## What To Check First

If WattPlan does not behave as expected:

1. Check `sensor.<setup_slug>_status`
2. Read `reason_summary` and `reason_codes`
3. Check the per-source status sensors
4. Open Home Assistant Repairs if a source is degraded or failed
