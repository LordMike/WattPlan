# Planner simulator

The offline simulator executes interchangeable planners against recorded household
load, PV energy and tariffs, including multiple batteries. It does not call Home
Assistant or source providers during replay. Use the prepared Linux/WSL environment
in [development.md](development.md): importing the integration package needs that
environment, although `optimizer/simulation/` contains no Home Assistant imports.

## Collect inputs and actuals

Enable **Record planner reproductions to disk** and **Historical cost tracking**.
Configure cumulative household usage and PV meters. Grid import/export meters are
optional for simulation. Keep the site's import/export tariff sources configured.
The usage meter must represent household demand excluding battery charging;
net grid consumption cannot substitute for that physical boundary.

Copy daily files from
`<HA config>/wattplan_reproductions/<entry ID>/YYYY-MM-DD.jsonl`, or save complete
`wattplan.export_planner_reproductions` responses as `.json` files, into one input
directory. Duplicate downloads are accepted; conflicting decisions, overlapping
actuals and conflicting actuals are rejected. Use `--setup-id` to select among
multiple identified setups. Legacy records cannot be disambiguated among multiple
setups. Keep configuration/report JSON outside this directory: all its `.json`
and `.jsonl` files are treated as reproduction data.

The export service appends available retained actuals to older reproduction data
without rewriting archived files. No copied `.storage` file or new Recorder
export service is needed. History retention still limits this enrichment.
Actuals are recorded independently when the tracker completes an interval,
including when planning is disabled or fails. The final interval of a day belongs
to its **local start date**; export that day after its last interval completes.
Use at least two days of recording retention to retain that interval after midnight.

An absent PV meter is unknown, even when historical cost sensors treated it as
zero. Missing usage/PV/prices, meter resets, multi-slot deltas, gaps and the first
partially sampled interval cause validation to fail. Older history lacks interval-specific
provenance and partial-slot detection; reports disclose this. Older zero export
tariffs recorded without an export meter cannot establish a counterfactual tariff
and become missing. New tracking retains configured export tariffs independently
of an export meter. An explicitly disabled export tariff has zero monetary value.
New intervals retain their original duration and currency across configuration
changes. Mixed currencies are rejected; older intervals use the history store's
recorded granularity and carry the legacy provenance warning.

## Run

Activate the development venv, then:

```bash
python scripts/simulate_planner.py /path/to/snapshots --validate-only
python scripts/simulate_planner.py '/path/to/snapshots/2026-09-*.jsonl' --validate-only
python scripts/simulate_planner.py '/path/to/snapshots/*.jsonl' \
  --patch-one-slot previous --output /path/to/repaired-report
python scripts/simulate_planner.py /path/to/snapshots --output /path/to/report
python scripts/simulate_planner.py /path/to/snapshots \
  --config /path/to/variants.json --repeats 3 --output /path/to/comparison
```

By default this compares the current HiGHS planner with self-consumption. Output
must be a new or empty directory separate from input. Validation reports usable
windows and the inferred range without invoking planners. Quote globs so the CLI
receives one pattern; individual files and recursive directories are also accepted.
No match is an error.
Exit codes: `0` successful run/validation with complete coverage; `1` failed planner
run; `2` invalid input/configuration, including any coverage gap. Failure diagnostics
retain the inferred range and selected file hashes when the range can be inferred.

Daily `YYYY-MM-DD` filenames and saved export response `date` fields select whole
local calendar days, using the records' timezone. The range spans the earliest to
latest selected date, including absent days between them, and is end-exclusive.
DST days therefore contain 23 or 25 hours where applicable. Undated snapshots use
the union of actual interval bounds and recorded plan slots, **not** future forecast
horizons. Reports include UTC range timestamps, hours, slot count, inference basis,
raw recorded bounds and executed seconds. An absent endpoint in an undated snapshot
cannot be inferred without a dated file or response.

Every intended slot needs a recorded planning input and complete usable actual
coverage. Missing planning cycles fail even when an older schedule could continue.
Multiple replans within a slot are allowed. Input capture must occur within its
plan slot; normal capture offsets and legacy completion times remain causal.
The initial capture delay is reported separately from intended range duration;
the simulator does not backdate the first action. Forecast cadence must be uniform
and aligned to the range. Actual intervals may subdivide slots but must be continuous;
partial boundary coverage fails. Exact duplicates are accepted, conflicts/overlaps fail.

`--patch-one-slot previous` or `next` explicitly repairs **exactly one deficient
slot total**, using its immediately adjacent unpatched slot. A deficiency may affect
actuals, the planner input, or both in that same slot. Multiple, consecutive, larger,
unaligned or partial holes fail. Actual repair requires one usable whole neighboring
interval, copies load/PV/tariffs, and clears measured grid flows. Planner repair
requires exactly one adjacent recorded input, copies its forecast arrays unchanged,
shifts capture/plan timestamps by one slot and rebases target deadlines and optional
load windows; expired deadlines/windows are removed. Equipment/cadence boundaries
cannot be crossed. Rebased active deadlines/windows must fit the copied forecast
horizon; otherwise repair is rejected rather than moving or dropping active intent.
Inputs captured outside their plan slot are reported as skipped and cannot override
a repaired cycle. Decisions before the selected range provide no initial state;
only the first in-range input seeds SoC.
These are synthetic copied inputs, not measured facts. Exact
source/destination timestamps, direction and repair kind appear in coverage, JSON
and HTML; actual interval rows retain repair provenance. Original files are untouched.

Example configuration:

```json
{
  "variants": [
    {"name": "current"},
    {"name": "short-lookahead", "settings": {"lookahead_slots": 12}},
    {"name": "candidate", "planner": "my_experiment:make_planner",
     "options": {"search_budget": 100}}
  ]
}
```

Use `highs`, `self-consumption`, `greedy`, `local-search`, `beam-search`,
`dynamic-programming`, `charge-window`, `genetic`, `cross-entropy`, or an
importable `module:factory`. See the [planner guide](optimizer-profiles.md) for
the algorithms' strengths and limitations. A factory
receives `options` as keyword arguments and returns a callable accepting the
existing `OptimizationParams`. This is trusted local Python code. The shared
battery contract requires complete named schedules and finite projected levels.
Capabilities and ignored preferences are explicit, and state is namespaced to
the implementation/options/version. Optional recommendations remain advisory.
Private factory state is recreated at equipment segment boundaries. Production
HA selection is controlled separately through General settings. See
[Planner factories](optimizer-api.md#planner-factories) for the extension interface.

Allowed `settings`: `lookahead_slots`, `rolling_window_slots`,
`throughput_cost_per_kwh`, `action_deadband_kwh`, `mode_switch_cost`,
`solver_time_limit_seconds`, `infer_battery_preserve_policy`. Battery equipment,
targets, actuals, forecasts and slot duration remain common to variants.

## Execution semantics

The recorded input-capture timestamp determines each decision event. Legacy
records use completion time conservatively and produce a warning. Forecast slot
indices still use `plan_start`. Initial physical state is observed once per
segment; subsequent requests receive simulated SoC and their own planner state.
The simulator never resynchronizes to later observed or predicted battery levels.

For example, a forecast of 2 kWh followed by actual consumption of 0.5 kWh executes
only 0.5 kWh of demand. The next request still receives its recorded forecast;
only its starting SoC/state reflect simulated execution.

Actions continue until replaced or expired within the validated continuous range.
Execution splits at decision, action and measurement boundaries, spreading
measured energy uniformly within its interval. Peaks, curtailment, inverter delays
and subinterval meter timing are not reconstructed. Curves are sampled at each
execution step's SoC and scaled to its duration. Full-slot grid-charge headroom
eligibility is latched at each new action, so splitting an action does not turn
it off early. PV follows battery order; discharge is proportional among eligible
batteries. Limits, capacity, reserve, ingress permissions and efficiencies apply.
Starting below reserve does not manufacture energy.

The site has one grid direction per execution interval. After all permitted PV
charging, any remaining PV surplus is exported and prevents grid charging.
Simply netting simultaneous flows would wrongly let a GRID-only battery consume
PV. When other eligible batteries absorb all surplus, grid charging can proceed.
Different batteries may charge and discharge together, but discharge serves only
household/comfort demand, never another battery's charging load.

Historical self-consumption shares this kernel while retaining its sequential
discharge allocation. Continuous MILP control application retains its separate
deadband/control semantics and shares limit/headroom primitives.

Coverage segments are common to variants. Missing actuals, missing planning cycles
and expired plan coverage reject the dataset before any planner runs. Changed equipment
starts a new segment from observed SoC. Resets are reported. Configured comfort
entities are rejected because their counterfactual consumption is unknown.
Household load includes the appliances that actually ran. V1 also does not execute
optional suggestions.

Variants run sequentially in separate processes. Repeats default to one and
alternate variant order. **One complete replay of one planner configuration has
a 120-second wall-clock budget.** Each variant/repeat gets a fresh budget. All
cycles share that deadline: a 24-hour/96-cycle replay and a 48-hour/192-cycle
replay each get 120 seconds in total. The budget does not scale with data length,
slot duration or lookahead, and does not apply to the batch of all variants.

The deadline starts immediately before worker construction and includes startup,
request/result transfer, model validation, every internal solver call, simulated
physics, and outcome aggregation. Each worker call receives only the remaining
run time. Solver-specific budgets cannot extend the run deadline. Initial input
loading/coverage validation and final report writing are outside this budget, as
is process cleanup after completion. Synchronous process creation and simulation
steps are checked immediately after they return; blocked planner communication
is interrupted by killing its worker when the remaining budget expires. Bounded
process/thread cleanup can follow the cutoff.

An exhausted run is failed with no scored totals; later variants/repeats continue.
Partial rows, run-scope timeout diagnostics, the failed timestamp, completed replay
cycles and completed planner calls remain for diagnosis. JSON and HTML report
the configured limit and observed run wall time. Exceptions and invalid output
also invalidate the entire run. Per-call wall/CPU timings still cover completed
planner calls, excluding startup and input validation; `run_wall_seconds` covers
the replay budget's wider scope (and failure cleanup when needed). Compute
latency is not an actuator delay. This simulator budget does not change the
production Home Assistant integration's timeout.

## Reports

- `report.html`: portable outcome tables and cost/stored-energy plots.
- `summary.json`: coverage, resets, totals, latency summaries, comparisons, settings,
  run order, input/source hashes, revision/worktree state and package versions.
  Keep custom planner source separately: optimizer hashes do not cover external modules.
- `NN-run.json`: full cycles, executed intervals and segment boundaries.
- `NN-cycles.csv`, `NN-intervals.csv`: machine-readable diagnostics and outcomes.

Outcomes include import/export kWh, signed import cost/export revenue/net cost,
AC-side charge/discharge/throughput, losses, covered target-deadline misses and
initial/final SoC for each segment. Targets outside coverage are not assessed.
Preference weights are not actual electricity costs or wear estimates. No
terminal stored-energy price is invented: compare costs **together with final
SoC**, resets and coverage. Lower cost with less stored energy is not proof of a
better plan.

Battery-only serial benchmarks and selected battery outcome tests use this
simulator. Their fixture actuals are explicitly synthetic. Direct solver/API,
prefix reuse, comfort and provider tests retain their separate responsibilities.
