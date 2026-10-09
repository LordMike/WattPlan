# Planner Diagnostics and History

WattPlan can retain the exact inputs and result of each successful planning run,
plus completed measured intervals, in append-only JSONL files. This is intended for investigations and
does not use Home Assistant Recorder's attribute storage.

## Turn on recording

Open `Settings` -> `Devices & services` -> `WattPlan` -> `Configure` ->
`Troubleshooting`. **Record planner reproductions to disk** is off by default.
The retention setting accepts **1 to 730 local calendar days**, defaulting to
**14**. Changing either option reloads WattPlan; it does not recover earlier
plans. Scheduled and `wattplan.run_optimize_now` plans are recorded alike.

Recording uses roughly **10 MB of disk per day** at 15-minute planning
intervals (one full request and result per plan), so the 14-day default needs
about 140 MB and the 730-day maximum several gigabytes. There is deliberately
no size cap; pick a retention that fits your storage, and turn recording off
once an investigation is finished.

Each successful run appends one complete JSON object and a newline to
`<HA config>/wattplan_reproductions/<config entry ID>/YYYY-MM-DD.jsonl`, using
Home Assistant's configured local date. Each record has its own UTC creation
timestamp, integration version, validated unrounded optimizer request (including
settings and initial state), and full optimizer result. No separate settings
file or reference is needed to replay it. Keep these files private: they contain
household energy forecasts and battery configuration.

New records use schema version 2. Plan records (`record_type: plan`) preserve
`request.optimizer_params` and `result`, adding setup ID, timezone, currency,
`input_captured_at`, actuals status and available preceding completed intervals.
With historical tracking, independent `record_type: actuals` updates append
measured usage/PV, stored tariffs, optional grid flows, source roles and quality
flags when a slot completes, even without a successful plan. Actuals updates use
the interval's local **start** date. Unknowns stay null; forecasts never replace
measurements. See [Planner simulator](simulator.md) for collection and replay.

After each append, WattPlan deletes this setup's day files older than the
retention window. A one-day setting keeps today's file only. Turning recording
off stops writes but leaves existing files until recording resumes and the next
append performs cleanup. A disk error is logged and does not invalidate a
successful plan. Incomplete trailing lines from an interrupted write are
excluded from exports and removed before the next append.

## Retrieve a day

Call the response service `wattplan.export_planner_reproductions` with a required
local `date` (`YYYY-MM-DD`). With one loaded setup no selector is needed; with
multiple setups pass its `entry_id`, `name`, or any WattPlan `entity_id` (for
example `sensor.wattplan_last_run`). Multiple selectors must agree. The response
contains:

```yaml
date: "2026-09-29"
data: '{"created_at":"...",...}\n{"created_at":"...",...}\n'
min_available_date: "2026-09-20"
max_available_date: "2026-09-29"
incomplete_tail: false
actuals_status: tracking_enabled
```

`data` is the day's JSONL text, enriched with available retained actuals absent
from the archive, without rewriting files or changing forecasts. A date with no
archived or retained data returns an empty string. Min/max describe disk files
and are `null` if none exist. Call the
service once per date to collect a range. Its response is a complete day's
contents, **not a stream**; a large day may be several megabytes and a client
may limit the response size.

To replay an individual line locally:

```python
import json

from custom_components.wattplan.optimizer import OptimizationParams, optimize

record = json.loads(line)
# Select a plan record; actuals-only updates have no request/result.
replayed = optimize(OptimizationParams(**record["request"]["optimizer_params"]))
print(replayed["entities"] == record["result"]["entities"])
```

The former Planner Reproduction entity and its Recorder-based binary payload
are no longer used. The existing Plan Details entities remain current inspection
views; their large attributes are still excluded from Recorder history.
