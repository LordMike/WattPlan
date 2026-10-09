# Offline planner comparison ledger

The simulator runs nine built-in planners side by side: `highs`,
`self-consumption`, `greedy`, `local-search`, `beam-search`,
`dynamic-programming`, `charge-window`, `genetic`, and `cross-entropy`.
Home Assistant continues to call the existing HiGHS planner. Experimental
implementations optimize batteries; comfort inputs are rejected and optional
recommendations are explicitly ignored. The original five-planner results below
are retained as historical evidence.

Use the supplied [comparison configuration](comparison.json):

```bash
python scripts/generate_simulation_data.py /tmp/wattplan-example-input
python scripts/simulate_planner.py /tmp/wattplan-example-input \
  --config docs/planners/comparison.json --output /tmp/wattplan-example-report
```

Use new or empty output directories and the prepared Linux/WSL environment.
See [simulator.md](../simulator.md) for real recordings, strict coverage and
explicit single-slot repairs. Both datasets below passed without repairs.

The [additional independent comparison](additional-comparison.md) reports the
frozen dynamic-programming, charge-window, genetic and cross-entropy profiles
against fresh beam/self-consumption controls on two 48-hour forecast scenarios.
The historical tables below remain unchanged.

## Foundation and implementation gates

Shared contracts, capabilities, namespaced state, forecast evaluation, neutral
physics, budgets, lazy solver imports and the synthetic generator were committed
as `35806f88a3959f4f3e2402bce2af287ecf8ab946` before parallel implementation began.
The foundation passed 892 tests, 88.44% coverage and HACS packaging. Eight original
planner responses, including full/prefix reuse and targets, were byte-identical
after excluding timing fields. The plumbing and HA-data agents independently
cross-checked the synthetic day and reconstructed HA replay before this commit.

Each new planner was implemented and initially tuned by a separate
`gpt-6.1-sol/high` agent in its own worktree, then reviewed and merged. The HA
dataset was prepared by a `gpt-6.1-sol/medium` agent. No Z3 implementation was added.

| Planner | Implementation commit | Independent worktree/branch | Detailed ledger |
| --- | --- | --- | --- |
| Greedy | `e49d2fdb4644e3ea54f5f7b5a6415cf7d9047077` | `WattPlan-planner-greedy` / `codex/planner-greedy` | [Greedy](greedy.md) |
| Local search | `5d28804cb03f54189477cb9fb2cde805a46ea2ae` | `WattPlan-planner-local-search` / `codex/planner-local-search` | [Local search](local_search.md) |
| Beam search | `5b1d875c51eb544514882337c49217fec760c630` | `WattPlan-planner-beam-search` / `codex/planner-beam-search` | [Beam search](beam_search.md) |

## Combined synthetic run, 2026-10-09

Input range: **2026-06-15 00:00 UTC to 2026-06-16 00:00 UTC**, end exclusive:
**24 hours / 96 fifteen-minute slots**, all 86,400 seconds executed. The generated
data has two heterogeneous batteries, a charging deadline, signed import/export
tariffs and forecast errors. Actuals and forecasts use separate deterministic
profiles. Every record is explicitly synthetic.

All five variants started with 4.0 kWh and ended with **house 0.7 / vehicle 5.0 kWh**.
Timings below cover planner calls across all 96 cycles, excluding worker startup
and input validation. CPU-heavy tests and runs were serialized with a shared lock.
This is one final run, not a statistical timing guarantee; earlier sweeps are in
the individual ledgers.

| Planner | Net cost (TEST) | Import / export kWh | Target misses | CPU total s | Wall total s | Wall p95 s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| HiGHS | 2.116418 | 17.405753 / 13.101905 | 0 | 130.315 | 82.660 | 2.679616 |
| Self-consumption | 4.891220 | 11.707444 / 7.942458 | 1 | 0.115 | 0.109 | 0.001824 |
| Greedy | 1.967313 | 18.237505 / 13.933657 | 0 | 17.994 | 17.064 | 0.206757 |
| Local search | 1.967313 | 18.237505 / 13.933657 | 0 | 8.602 | 8.130 | 0.094737 |
| Beam search | 1.967313 | 18.237505 / 13.933657 | 0 | 15.926 | 15.096 | 0.202914 |

The new planners share a lexicographic target-miss/economic objective and score
the full supplied horizon. HiGHS retains its existing scalar soft-target objective
and this fixture's 32-slot economic lookahead. Greedy uses a 24-slot trade window;
beam search uses 24-slot economic guidance. These different planning semantics
prevent attributing the cost difference solely to algorithm quality. All final
costs exclude heuristic penalties and assign no terminal energy credit.

Local search's default was reduced from 384 to 96 evaluations after larger budgets
failed to improve this day's realized result. Greedy retained its 24-slot trade
window after a 96-slot window performed worse. Beam retained width 16 after a
separate 24-request comparison showed fewer target misses than width 4. These
are initial defaults, not universal optimum settings.

## Combined HA smoke run

Input range: **2026-10-08 08:15 to 10:00 UTC**, or **10:15 to 12:00 Copenhagen**:
**1.75 hours / 7 slots**. Execution covers 6,278.098309 seconds after the initial
21.901691-second legacy capture delay. All variants started at 2.25 kWh.

Seven genuine archived forecasts were selected from the complete October 8 export
without changing their forecast arrays. Actuals were reconstructed locally from
step-held cumulative Deye load/PV readings, with archived first-slot effective
tariffs. They are disclosed hybrid actuals, not original completed tracker intervals.
The first meter baseline was excluded; maximum boundary sample ages were 4.08
seconds for load and 64.05 seconds for PV. The configured load source was confirmed,
but the vendor meter's physical boundary was not independently certified.

| Planner | Net cost DKK | Import / export kWh | Final SoC kWh | Wall total s | Wall p95 s |
| --- | ---: | ---: | ---: | ---: | ---: |
| HiGHS | 0 | 0 / 0 | 1.141052 | 34.111 | 8.144450 |
| Self-consumption | 0 | 0 / 0 | 1.141052 | 0.018 | 0.003693 |
| Greedy | 0 | 0 / 0 | 1.141052 | 1.726 | 0.274222 |
| Local search | 0.267512 | 0.136184 / 0 | 1.292368 | 0.799 | 0.120166 |
| Beam search | 1.888337 | 0.920000 / 0 | 2.163274 | 0.616 | 0.093294 |

All runs completed with zero covered target misses. Local and beam search spent
more while retaining more energy against their future forecasts. This short smoke
does not establish an economic winner; comparing cost without final SoC would
misrepresent the outcomes. No private recordings are committed to the repository.

## Independent checks and remaining limitations

- Final combined suite: **986 passed, 88.71% coverage**; static checks and HACS
  packaging passed. The archive contains all five planner modules.
- All eight original planner responses remain unchanged after the merges.
- Final JSON/CSV totals, coverage, input hashes and energy conservation were
  independently checked for all ten completed runs; energy residuals were below
  1e-8 kWh. Source inputs were unchanged.
- Exhaustive four-slot review: greedy and beam matched the optimum in all six
  cases after greedy improvements. Local search matched five; one feasible
  at-most case cost 0.15 versus optimum 0.09. This bounded-search limitation is
  retained in its ledger. Six cases are not an optimality proof.
- Local and beam stress-day runs reported the unreachable exact target's
  4.152 kWh miss and met the other battery's deadline. Greedy has direct infeasible
  target and 672-slot/many-battery tests. Every implementation reports unmet
  targets; it cannot generally distinguish physical infeasibility from a search
  limitation.
- Real-world qualification needs longer contiguous measured data and varied
  equipment/seasons. Comfort consumption and optional-load execution remain
  outside the simulator's counterfactual model.

## Local artifacts and reproduction

Private/generated artifacts live under `N:/Personal/WattPlan-planner-lab` (WSL
`/mnt/n/Personal/WattPlan-planner-lab`). `combined-synthetic/report.html` and
`combined-ha/report.html` are the final combined reports. Each directory contains
full run JSON, CSV, configurations, input/source hashes, revision/worktree state
and timing summaries. `final-report-audit.json` records the independent checks;
`combined-independent-review.json` records the oracle and compatibility results.
`ha-provenance.json` and `synthetic-independent-review.json` preserve the original
two-agent dataset gate. The per-planner directories retain tuning runs, exact
commands and source snapshots.

The final combined runs used the committed comparison config with no setting or
option overrides, one repeat, on `synthetic/` and
`ha-enriched/observed-meter-replay.jsonl` respectively. For example, from the repo:

```bash
flock /tmp/wattplan-planner-compute.lock \
  /tmp/wattplan-simulation-venv/bin/python scripts/simulate_planner.py \
  /mnt/n/Personal/WattPlan-planner-lab/synthetic \
  --config docs/planners/comparison.json --output /tmp/wattplan-comparison-new
```

Input SHA256s:

- Synthetic: `e34c2cb9c30fde03c396e9ffc2236e1b98b8ada62f99e2b672613dea0c2b00b9`.
- HA replay: `8d2bad02edf8e2807712af1bef6f206aaa313c07bf31df9bc5dd45e33d08bbf4`.
