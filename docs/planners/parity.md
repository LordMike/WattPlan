# Planner feature parity

This work brings the registered planners onto the live Home Assistant result
contract and exposes a finite set of named presets. HiGHS remains the default.
Search strategy and solution quality may differ; configured equipment, comfort
constraints, recommendations, health reporting and Home Assistant outputs must
have consistent meanings.

## Acceptance scope

- Preserve physical battery limits, efficiencies, ingress permissions and
  deadline target modes. Optimizing planners prioritize below-reserve recovery;
  self-consumption remains an explicitly limited, non-optimizing baseline.
- Share comfort scheduling, rolling-window requirements, maximum off times,
  minimum runs and continuity handling without a hidden native-solver fallback.
- Share full projections, costs, baselines, savings, energy flows and optional
  appliance window recommendations. Recommendations do not execute appliances.
- Report observed target/reserve/comfort violations accurately. Search budget
  exhaustion is distinct from an unmet constraint or proven infeasibility.
- Select named presets in HA; retain legacy HiGHS defaults and reset incompatible
  state when switching planner/configuration. Preserve preset identity in exports.
- Keep non-HiGHS imports solver-independent and optimizer imports HA-independent.
- Check each implementation independently, then run the complete integrated test
  suite and HACS packaging validation before benchmarking the frozen source.

## Validation and benchmark policy

Use the existing genuine-forecast and causal-forecast 48-hour recordings without
changing inputs, plus the synthetic target fixture. Each configuration/repeat
gets 120 seconds for the complete replay. Serialize CPU work. Keep failures and
timeouts unscored; never extend the cap to rescue a run.

The new matrix covers all published presets and previously selected benchmark
configurations, including historical HiGHS configurations that timed out.
Compare each matching configuration with its most recent earlier measurement;
label new configurations as new. Report realized cost, runtime, final energy,
target checks and cycle-start policy counts. Preserve earlier evidence.

The historical datasets have no executed comfort loads. Comfort feature parity
therefore also requires dedicated constraint, continuity, reporting and HA tests;
the battery-only replay cannot establish counterfactual comfort economics.

## Work record

Parity review found and corrected a preexisting policy-physics defect: a GRID-only
battery could charge while residual PV was exported, producing simultaneous site
import/export and falsely satisfying targets with a forbidden energy source.
Shared planning and simulation now suppress grid charging until all PV surplus
has been absorbed by permitted loads/batteries, matching HiGHS site-direction
constraints. Earlier benchmark costs can differ if their recorded inputs contain
this pattern; comparisons retain the old evidence and disclose the correction.

Published controls also normalize a `grid_charge` search label to `preserve`
when the accepted trajectory has exactly zero grid charging for that battery and
slot. This leaves forecast energy/cost and candidate budgets unchanged, while
optional recommendations replay the published controls. Historical action counts
may change, and realized replay costs can change under forecast errors because
an ineffective forecast control no longer activates unexpectedly in replay.

Each implementation received an individual subagent pass, followed by root
acceptance checks and an additional review/fix cycle. All nine factories support
comfort, optional recommendations and the complete HA result fields. Seventeen
named presets are available, with legacy HiGHS still the default.

The shared acceptance matrix checks every preset's comfort accounting, costs,
flows, recommendations and health, plus reachable immediate battery targets and
reserve recovery for the optimizing presets. Every preset also ran through real
HA planning with battery, comfort and optional equipment together, including
Outlook and degraded-history status. Algorithm-specific checks retain bounded
search, determinism, ingress, target modes, curves, multiple batteries and
solver-independent imports.

Review additionally corrected budget-exhausted beam recovery, incomplete export
identity, inactive lookahead text in setup summaries, and results outliving a
preset reload or shutdown. HA retains a shielded executor job across reloads to
prevent overlapping calls after timeout. This protects ownership and publication;
it does not pretend to terminate native threads.

The final integrated gate passed **1,445 tests, 89.80% coverage, 108.70 seconds**.
The HACS archive contains 119 integration files, includes all nine algorithms and
the shared modules, and matches the tested files byte-for-byte. Archive SHA256:
`2df60a9b59d5e5e4650c543a0b491e13fe77be105c0ad6bd53189659c4ba6cf9`.

The frozen [18-configuration benchmark](parity-benchmark.json) is complete across
all three datasets: 45 complete replays, six whole-replay timeouts and three
native HiGHS solve failures. Failures remain unscored and were not retried.
Both the independent physical replay audit and root's separate accounting/hash
audit passed all 54 saved attempts. The latter found a maximum site energy
conservation residual of `2.22e-16` kWh. The HACS archive was checked again after
benchmarking and still matches every integration source file.

The [comparison](parity-comparison.md) records all new measurements and changes
from prior runs, including target outcomes and terminal energy. Self-consumption remains explicitly
passive for battery targets/recovery; search quality and target feasibility are
not guaranteed merely by supporting a target constraint. Alternatives use
bounded comfort placement around a self-consumption cost estimate, while HiGHS
may reoptimize its battery plan during placement. These are disclosed differences
in optimization approach, not omitted comfort inputs.

Commit preparation removed trailing blank lines from `comfort_schedule.py` after
the benchmark. The archived benchmark source is preserved;
`commit-source-verification.json` in the private evidence directory verifies that
this is the only source difference and the parsed Python syntax tree is identical.
The rebuilt HACS archive still contains 119 files and matches the commit sources;
its SHA256 is
`d6f1a61f09a9de9bb3790223575113e5d9d50d9065b8c09c7dc743493c1fe9fe`.
The full suite passed again on these commit sources: **1,445 tests in 102.67
seconds**, with the log preserved as `commit-full-tests.log` alongside the
benchmark evidence.
