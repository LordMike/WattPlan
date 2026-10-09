# Greedy battery planner

Use `custom_components.wattplan.optimizer.planners.greedy:factory` in the offline
simulator or choose a greedy preset in Home Assistant; HiGHS remains the default.
Options are strict keyword arguments:
`max_evaluations=256` (positive integer) and `trade_window_slots=24` (integer 1–672).
The latter bounds a tariff trade's future release opportunity, in planner slots;
it is six hours with fifteen-minute slots. Legacy `lookahead_slots` is ignored.
The returned schedule and shared evaluation always cover the full supplied horizon.

The deterministic seed self-consumes positive-price load and preserves on zero or
negative import prices. Initially deficient batteries first recover their reserve
using legal ingress; this recovery is included even with a one-evaluation budget.
For a missed target, one constructive chain first preserves
through its inclusive deadline, then progressively adds cheap charging or valuable
discharge actions. Only the best fully evaluated candidate is retained, so a chain
can cross an empty-storage plateau. Subsequent fixed passes preserve cheap load,
try a plain cheap charge, try charging while preserving until one later unsupplied
release opportunity, and release remaining energy against valuable load. Releases
use remaining grid import from the current accepted replay, so an already supplied
peak does not hide another useful load. Each ranked candidate is visited once;
there are no convergence loops, restarts or general neighborhood search.

All decisions use request forecasts alone. Shared neutral physics evaluates PV
allocation in battery order, proportional discharge, ingress restrictions, tapered
curves, AC efficiencies, reserves and grid-charge headroom. Full-horizon target
misses are lexicographically first, then tariff cost plus reserve-recovery,
throughput and switching
preferences. Terminal energy credit is zero. Signed import/export tariffs retain
their sign; paid import can profitably leave stored energy at horizon end. Every
accepted candidate improves the same evaluator score; initial requests are untouched.

`SearchBudget` counts the seed and every candidate before replay. Target construction
shares remaining evaluations among target-bearing batteries; an identical initial
preserve candidate reuses its accepted evaluation. Unmet targets take priority
over economic passes, so a small budget can still construct a multi-charge target.
The remaining preserve/trade/release passes
divide available evaluations approximately 1:2:1. Work is bounded by
`max_evaluations` full replays: roughly O(max_evaluations × batteries × horizon),
with O(batteries × horizon) schedule storage, plus tariff sorting and bounded release
scans. Budget 1 returns the seed. Every configured battery still receives a complete
finite schedule. Battery search is stateless and never imports or falls back to `highspy`.

Both direct factories and registry calls use the shared comfort wrapper. It plans
comfort constraints and includes their consumption before battery search, then
publishes complete HA projections and base/comfort energy flows. Optional appliance
recommendations replay the accepted battery policies with each proposed load.
Whole-call execution time includes preparation and reporting. Reserve and target
misses appear in HA plan health as observed violations, without an infeasibility
claim. Battery search is stateless; the wrapper carries comfort continuity.

The heuristic does not guarantee a globally optimal schedule or feasible targets.
Discrete full-slot policies can miss an exact target that a continuous controller
would meet. A single monotone repair chain can miss a feasible combination of
charging, preserving and discharging; forced PV, reserve, demand and ingress can
make a target physically impossible. Limited evaluations can also leave a target
unrepaired, particularly with many batteries. Target outcomes and total misses are
reported rather than labeled infeasible without proof. Cheap-slot and battery-order
priority can favor earlier batteries at small budgets. One release opportunity per
trade cannot represent every profitable arbitrage/PV interaction. There is no
battery export, PV curtailment or invented load.

## Initial tuning ledger

Runs, exact configs, commands, source snapshots and source/input hashes are retained
outside the repository under `N:/Personal/WattPlan-planner-lab/greedy/`:
`comparison.json`, `selected.json`, `commands.txt`, `ledger.json`, `greedy-source.py`,
and each report directory's `summary.json`, run JSON and CSV files. Reports identify
foundation `35806f88a3959f4f3e2402bce2af287ecf8ab946` plus uncommitted source hashes.
The final implementation SHA256 is
`dde0810792902bcf653f21e83657c7484e55f0b34de672da8768e3b0aeaf6b36`.

The full synthetic UTC day contains 96 replans, heterogeneous batteries, a deadline,
forecast error and signed prices. Both initial variants used 256 evaluations maximum.
Costs below are in synthetic TEST currency; import/export/SoC are kWh. Target misses
are the simulator's covered-deadline count; every normal-day variant had zero.

| Report / implementation | Window | Net cost | Import | Export | Final SoC total | Wall p95 s | CPU p95 s | Total evaluations (per call range) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| synthetic-comparison / initial | 24 | 1.967313 | 18.237505 | 13.933657 | 5.7 | 0.194018 | 0.204115 | 18588 (192–209) |
| synthetic-comparison / initial | 96 | 3.585433 | 15.957124 | 12.077219 | 5.7 | 0.208580 | 0.219536 | 19322 (192–234) |
| synthetic-selected / forecast-deficit releases | 24 | 1.967313 | 18.237505 | 13.933657 | 5.7 | 0.192090 | 0.202294 | 18588 (192–209) |
| synthetic-tuned / final | 24 | 1.967313 | 18.237505 | 13.933657 | 5.7 | 0.204649 | 0.215715 | 18602 (192–210) |

Keep the initial six-hour window: the full-day window was worse with equal final
energy and targets, and unnecessarily deferred useful load toward distant peaks.
This single synthetic day cannot establish an optimal window for other homes or
seasons. The default is a modest local tariff-trade heuristic; the full evaluator
still protects all supplied targets and accounts for the complete tail. The
prepared same-input reference was HiGHS 2.116418 (zero misses) and self-consumption
4.891220 (one missed target), each ending at the same 5.7 kWh. These fixture results
are not a general superiority claim over the production solver.

An independent exhaustive four-slot checker exposed three general economic gaps,
retained at `N:/Personal/WattPlan-planner-lab/initial-oracle-greedy.json`. Cheap charging
before two peak slots cost 0.9 versus oracle 0.3; a PV opportunity cost 0.4 versus 0;
and paid import with an at_most deadline cost 0.63 versus 0.09. This motivated plain
charge insertion and remaining-import release selection, rather than more search
passes. New regressions reach 0.3, 0 and 0.09 respectively, without increasing target
misses. A separate regression prevents a high-price slot covered by PV from hiding
an earlier real deficit. The normal-day outcome did not change after these fixes.

The HA replay uses seven slots of genuine archived forecasts and reconstructed
step-held meter actuals on 2026-10-08 10:15–12:00 Europe/Copenhagen. Final `ha-tuned`
has zero net cost, import, export and target misses, final battery 1.141052019 kWh,
wall/CPU p95 0.197275/0.207700 seconds and 1344 evaluations (192 per call).
The earlier `ha-selected` smoke also had identical energy/cost outcomes, with
wall/CPU p95 0.265505/0.279771 seconds and 1393 evaluations (199 per call).
Both match the prepared baselines' zero cost and final energy. This short interval
is a plumbing smoke check, not evidence of economic gains.

Twenty-eight focused tests cover the shared conformance helper, efficiencies,
negative tariffs, PV-only ingress and tapering, all target modes and tolerance,
expensive charging with target priority, preference penalties, impossible exact and
at_most targets, deterministic 672-slot scheduling, and twelve batteries over 672
slots with a tight three-evaluation budget. Final full suite: 920 passed in 59.45 s,
88.52% coverage; Ruff F checks and formatting passed. All pytest runs used elevated
execution and the shared compute lock, with local `/tmp` venv/temp/coverage paths.
