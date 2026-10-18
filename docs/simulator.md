# Compare planners with the simulator

The simulator answers: **how would these planners have operated my batteries
using the forecasts WattPlan actually had?** It replays exported planning inputs
against measured household consumption, solar production, and tariffs. It runs
locally and does not control devices or contact Home Assistant during replay.

For an existing export, start with the [comparison workflow for users and
agents](#comparison-workflow-for-users-and-agents). The numbered sections explain
how to collect the recordings first.

## 1. Enable recording in Home Assistant

Open **Settings → Devices & services → WattPlan → Configure** for your setup.

1. Under **Troubleshooting**, enable **Record planner reproductions to disk**.
   Keep the default 14-day retention, or enough days for your collection period
   plus the download day. One-day retention cannot keep yesterday after midnight.
2. Under **Historical costs**, enable **historical cost tracking** and configure
   the [cumulative energy meters](historical-cost-tracking.md#setup-requirements).
   Replay needs measured usage and PV energy, plus import and export tariffs.
   Household usage must exclude battery charging. Grid import is required by the
   historical tracking setup, although replay calculates its own grid flows.
3. Keep scheduled planning enabled at every configured slot, and keep the price
   sources and meters available throughout collection.

Recording captures forecasts; historical tracking supplies what actually happened.
Both are needed. Enabling recording does not recover earlier plans. Replay rejects
unknown PV actuals, including an omitted PV meter; it does not infer zero production
from a missing sensor.

Historical replay currently supports batteries only. Exports containing configured
comfort loads are rejected because their consumption under a different schedule
is unknown. Optional suggestions are not executed; measured household demand
already includes the appliances that actually ran.

## 2. Wait for a complete day

Collect **at least one complete local calendar day**. Enable the settings before
that day starts, let WattPlan run through it, and export after the last interval
has completed and been recorded. Starting partway through today generally means
waiting **about 24–48 hours**, until just after tomorrow ends. Allow another
planning interval after midnight for the final records to become available.

For example, enable recording on Monday afternoon, collect Tuesday, and download
Tuesday's export on Wednesday after midnight. With 15-minute slots, a normal day
contains 96 planning slots. Daylight-saving transitions can make it 23 or 25 hours.

Several complete days show more price and weather conditions. Compare them
separately or select consecutive days together. The first partially recorded day
and the current unfinished day fail whole-day validation. Waiting longer does not
fill gaps caused by missing plans or unavailable meters.

## 3. Download the exports

Call `wattplan.export_planner_reproductions` once per local date. In Home Assistant's
**Developer tools → Actions**, the request looks like this:

```yaml
action: wattplan.export_planner_reproductions
data:
  date: "2026-10-08"
```

Replace the date with a completed day you recorded. With multiple WattPlan setups,
also supply `entry_id`, `name`, or a WattPlan `entity_id` for the intended setup.
Save the complete service response as JSON, or its decoded `data` string as JSONL.
Do not save the displayed YAML as a `.json` file.

For a direct download from a Linux/WSL shell, run this from your local WattPlan
repository. Set `ha_url` to your instance's address and enter a long-lived access
token created in your Home Assistant profile:

```bash
mkdir -p .tmp/simulator/exports
ha_url='http://homeassistant.local:8123'
export_date='2026-10-08'
read -rsp 'Home Assistant access token: ' ha_token
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $ha_token" \
  -H 'Content-Type: application/json' \
  --data "{\"date\":\"$export_date\"}" \
  "$ha_url/api/services/wattplan/export_planner_reproductions?return_response" \
  --output ".tmp/simulator/exports/$export_date.json"
unset ha_token
```

Add a setup selector to the JSON payload when needed, for example
`{"date":"2026-10-08","name":"Home"}`. Saved REST responses, including the
`service_response` wrapper, work directly. The request uses Home Assistant's
documented [service response API](https://developers.home-assistant.io/docs/api/rest/).
Check that the download succeeded before running the simulator.

Alternatively, copy daily files directly from
`<HA config>/wattplan_reproductions/<entry ID>/YYYY-MM-DD.jsonl` into your exports
folder. Service exports can enrich older recordings with retained actuals, so
prefer them if copied files lack measurements. Keep household recordings private;
`.tmp/` is ignored by Git.

## 4. Prepare and validate locally

Use a local checkout of WattPlan and the Python environment described in
[development setup](development.md#setup). On Windows, use WSL. Activate that
environment with `source "$venv/bin/activate"`, then run commands from the
repository root. You do not need a running local Home Assistant.

Keep exports and reports in separate folders:

```text
.tmp/simulator/
  exports/
    2026-10-08.json
    2026-10-09.json
  comparison-1/          ← created by the simulator
```

Validate the exports before running planners:

```bash
python scripts/simulate_planner.py .tmp/simulator/exports --validate-only
```

Validation prints the inferred date range, coverage, warnings, and selected
configurations. Every intended slot needs a recorded planning input and complete
usable actuals. Dated exports select **whole local days**; multiple dates select
the full range between them, including missing days. Exact duplicate downloads
are accepted; conflicting values are rejected.

Directories are read recursively. Keep configuration and report JSON outside the
exports folder: all `.json` and `.jsonl` files there are treated as recordings.

## 5. Select planners and run comparisons

See the [complete configuration catalog](optimizer-profiles.md) for all configuration IDs, their
Home Assistant dropdown labels, and experimental status. Use configuration IDs in
`--planners`, including when the dropdown shows a descriptive label.

The normal Home Assistant choices are **Local search**, **Charge-window search**,
**Guarded value policy**, **Joint seed search**, and **Self-consumption**. Start with
[Choosing a configuration](optimizer-profiles.md#choosing-a-configuration), then
use each planner's configuration table to find its ID. Initial Home Assistant
setup recommends Local search (`local-search-3`); the simulator uses the exact ID.

List the available configurations and labels from your checkout, including experimental
configurations (the command does not print experimental status):

```bash
python scripts/simulate_planner.py --list-planners
```

Compare Charge-window search, Joint seed search, and Self-consumption, with three
complete rounds each:

```bash
python scripts/simulate_planner.py .tmp/simulator/exports \
  --planners self-consumption charge-window-96 mobility-hybrid-seeds-4-grid4 \
  --rounds 3 --output .tmp/simulator/comparison-1
```

Or run every published configuration:

```bash
python scripts/simulate_planner.py .tmp/simulator/exports \
  --planners all --rounds 1 --output .tmp/simulator/all-configurations-1
```

`all` means every named configuration in the [configuration catalog](optimizer-profiles.md),
including each HiGHS lookahead and `beam-1`, `beam-2`, `beam-8`, and `beam-16`.
`all` includes experimental configurations and can be costly for long recordings.
Prefer selected configurations for long recordings. Each configuration and
round runs a separate complete replay, so a sweep of all 112 configurations can
take substantial time. Use `all` for short recordings when a complete sweep is
useful.
Bare algorithms such as `beam-search` use factory
defaults; `beam-16` selects the fixed configuration available in HA. Named configurations apply
their fixed algorithm settings to recorded inputs, just as HA does: `highs` uses
22 lookahead slots, while `highs-16` uses 16. Without
`--planners` or `--config`, the simulator retains its HiGHS versus Self-consumption
comparison, regardless of which planner originally recorded the data. Supply
explicit configuration IDs to compare the current Home Assistant choices.

One round replays the entire selected time range once per configuration, starting
each from the same observed battery levels. Rounds do not append days or carry
battery state between runs. Repeats help compare timing; fixed-seed planners
normally produce identical schedules from identical inputs. The three
configurations above with three rounds produce nine runs.

Select individual exports or quoted patterns instead of a whole folder:

```bash
python scripts/simulate_planner.py \
  .tmp/simulator/exports/2026-10-08.json .tmp/simulator/exports/2026-10-09.json \
  --planners greedy-256 cem-48 --rounds 2 --output .tmp/simulator/comparison-2

python scripts/simulate_planner.py '.tmp/simulator/exports/*.json' --validate-only
```

Put input paths before options. Unmatched paths/patterns are errors even if other
inputs match; overlapping paths are deduplicated. Output must be a new or empty
directory. Choose a new output name when rerunning a comparison.

| Argument | Meaning |
| --- | --- |
| Input paths | One or more export files, quoted globs, or recursive directories. |
| `--planners NAME ...` | Configurations or built-in algorithms; `all` selects all configurations. |
| `--list-planners` | List choices without loading exports or running planners. |
| `--rounds N` / `--repeats N` | Complete replays per configuration; default 1, positive values only. |
| `--output PATH` | New or empty report directory; required except for validation/listing. |
| `--validate-only` | Check data and show configurations without executing them. |
| `--setup-id ID` | Select one setup when files contain several identified setups. |
| `--config FILE` | Advanced JSON variants; cannot be combined with `--planners`. |
| `--patch-one-slot previous\|next` | Explicitly copy one deficient slot from its neighbor. |

## 6. Read the results

Open `report.html`. Compare net electricity cost **alongside final stored energy,
target misses, and run status**. A plan that costs less because it empties the
battery is not necessarily better. Costs exclude battery wear and assign no extra
monetary credit to energy left at the end.

The output also contains `summary.json`, full `NN-run.json` records, and
`NN-cycles.csv` / `NN-intervals.csv` files. These include effective configurations,
input hashes, coverage, battery resets, timings, and failure details.

Runs are sequential, reversing configuration order on alternate rounds. Each
configuration in each round has a **120-second limit for the entire replay**,
including all planning cycles and worker startup. Longer recordings do not get
more time. Timeouts and invalid results leave unscored failed runs; later runs
still execute. Start with one full day and one round to assess runtime.

The simulator preserves recorded forecasts, executes actions against actual
energy, and feeds simulated battery levels into the next decision. It does not
reset to historical battery levels each slot. Equipment changes start new
segments and are reported as resets. Replay does not reconstruct inverter delays
or power peaks within a slot; results remain modeled outcomes.

## Troubleshooting coverage

| Problem | What to check |
| --- | --- |
| Empty export | Was recording enabled on that date, and is it within retention? |
| Missing start/end of day | Export a fully recorded past day after its last interval completes. |
| Missing planning cycles | Confirm scheduled planning ran successfully every slot. |
| Invalid actuals | Check tracking, usage/PV meters, tariffs, and counter resets. |
| Multiple setups | Export one setup or select its ID with `--setup-id`. |
| Comfort-load error | Historical replay does not support configured comfort loads. |
| Input/output conflict | Keep reports and configurations outside the exports folder. |

For one missing slot, `--patch-one-slot previous` or `next` can copy its adjacent
valid slot. This approximation is disclosed in reports and leaves exports
unchanged. It cannot repair multiple missing slots, partial coverage, or
equipment/cadence boundaries. Prefer a complete day of measured data.

Exit codes: `0` successful validation or complete runs; `1` a failed planner run;
`2` invalid inputs/configuration, including coverage gaps.

## Comparison workflow for users and agents

1. Use the [development environment](development.md#setup) and keep recordings,
   custom configuration files, and generated reports under ignored `.tmp/` or
   outside the repository. Household exports are private data. The synthetic
   generator is for development; keep its labeled outputs separate from actual
   household recordings.
2. Validate the original exports with `--validate-only` before running planners.
   Read the inferred date range, coverage, and warnings. The loader rejects
   unsupported schemas, malformed record/request/actual shapes, unknown record
   types, conflicting duplicates, overlapping intervals, invalid measurements,
   and ambiguous setups. Coverage validation also rejects missing or unusable
   actuals, missing planning cycles, mixed cadence, and incomplete boundaries.
   An incomplete trailing JSONL record is explicitly warned about and omitted;
   remaining records must still pass coverage validation.
3. Resolve rejected inputs at their source. Collect a complete day with the
   required meters and forecasts. Do not replace missing measurements with
   forecasts or zeros, remove comfort loads from an export to bypass the
   battery-only replay restriction, or silently shorten the comparison period.
   The explicit one-slot repair described above is the only built-in repair and
   records what was copied.
4. Select exact configuration IDs from [Planner configurations](optimizer-profiles.md),
   keeping inputs, equipment, targets, and initial observations identical. Put
   **`self-consumption` first** when it should be the reference. Report cost
   deltas use the first run as their reference; a failed or service-infeasible
   reference leaves comparison deltas empty. An alternative is never silently
   promoted to reference.
5. Start with one full day and one round, writing to a new report folder outside
   the input directory. `--validate-only` checks recording coverage and variant
   structure without running algorithms; factory-option and capability errors
   can still fail execution. Each failed or timed-out run is unscored. Do not
   treat a partial trajectory as a completed result or extrapolate it into
   annual savings.
6. Check run status, coverage, service feasibility, target misses, segment resets,
   and final stored energy before comparing net cost. Optional suggestions are
   not executed. The electricity cost excludes wear and gives no separate credit
   for leftover battery energy. Repeat identical inputs to compare timing, then
   use additional complete days to explore different conditions. Retain the
   report's configuration, file hashes, and provenance alongside the results.

## Advanced configurations

Use JSON for settings beyond published configurations:

```json
{
  "variants": [
    {"name": "reference", "planner": "self-consumption"},
    {"name": "custom-highs", "planner": "highs", "options": {"lookahead_slots": 12}}
  ]
}
```

```bash
python scripts/simulate_planner.py .tmp/simulator/exports \
  --config .tmp/simulator/variants.json --rounds 2 \
  --output .tmp/simulator/custom-comparison
```

Names must be unique. `options` configure the factory; `settings` override shared
preferences such as `throughput_cost_per_kwh` or `mode_switch_cost`. Variants cannot
change equipment, targets, forecasts, actuals, or slot duration. Custom
`module:factory` planners execute trusted local Python code; see
[Planner factories](optimizer-api.md#planner-factories) for the interface.
