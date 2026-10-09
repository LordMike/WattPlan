# Compare planners with the simulator

The simulator answers: **how would these planners have operated my batteries
using the forecasts WattPlan actually had?** It replays exported planning inputs
against measured household consumption, solar production, and tariffs. It runs
locally and does not control devices or contact Home Assistant during replay.

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

List the available presets:

```bash
python scripts/simulate_planner.py --list-planners
```

Compare selected presets, with three complete rounds each:

```bash
python scripts/simulate_planner.py .tmp/simulator/exports \
  --planners highs-16 beam-16 dynamic-programming-strong self-consumption \
  --rounds 3 --output .tmp/simulator/comparison-1
```

Or run every published preset:

```bash
python scripts/simulate_planner.py .tmp/simulator/exports \
  --planners all --rounds 1 --output .tmp/simulator/all-presets-1
```

`all` means every named configuration in [the planner guide](optimizer-profiles.md),
including each HiGHS lookahead. Bare algorithms such as `beam-search` use factory
defaults; `beam-16` selects the fixed preset available in HA. `highs` uses recorded
HiGHS settings, while `highs-16` fixes its lookahead to 16 slots. Without
`--planners` or `--config`, the comparison is HiGHS versus self-consumption,
regardless of which planner originally recorded the data.

One round replays the entire selected time range once per configuration, starting
each from the same observed battery levels. Rounds do not append days or carry
battery state between runs. Repeats help compare timing; fixed-seed planners
normally produce identical schedules from identical inputs. Four configurations
with three rounds produce 12 runs.

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
| `--planners NAME ...` | Presets or built-in algorithms; `all` selects all presets. |
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

## Advanced configurations

Use JSON for settings beyond published presets:

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
