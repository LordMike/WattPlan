# Interchangeable planners

The production `optimize(OptimizationParams)` entry point continues to use the
existing HiGHS planner and its legacy state. Home Assistant selects named
[presets](optimizer-profiles.md) through the shared registry; the simulator can
also configure factories directly. Built-in names are `highs`, `self-consumption`, `greedy`, `local-search`,
`beam-search`, `dynamic-programming`, `charge-window`, `genetic`, and
`cross-entropy`; any importable `module:factory` works without registration.
See the [comparison ledger](planners/comparison.md) and its runnable
[configuration](planners/comparison.json). The old `simulation.adapters` factory
paths remain aliases. Solver modules load only when HiGHS is called.

## Additional search implementations

All four additional planners compute a fresh schedule from the current request on
every cycle, using the full supplied horizon. They share the physical evaluator
and target-first objective below. Their budgets bound search effort, not forecast
coverage; diagnostics disclose aggregation, pruning and fallback completion.

| Built-in name | Search method | Implementation and tuning notes |
| --- | --- | --- |
| `dynamic-programming` | Aggregate real battery trajectories by SoC state and previous physical behavior | [Dynamic programming](planners/dynamic-programming.md) |
| `charge-window` | Coarse-to-fine search over charge, preserve and release windows | [Charge-window search](planners/charge-window.md) |
| `genetic` | Population selection, crossover and mutation of complete schedules | [Genetic search](planners/genetic.md) |
| `cross-entropy` | Sample schedules and update their action distribution from elite candidates | [Cross-entropy search](planners/cross-entropy.md) |

The [selected benchmark configuration](planners/additional-benchmark.json)
contains two DP profiles, one charge-window profile, two genetic profiles and
one cross-entropy profile, plus self-consumption and two beam references.
These selections are initial tuning results, not changed production defaults.

The simulator gives each configuration/repeat a 120-second budget for its entire
replay, including all planning cycles. Count budgets and fixed random seeds make
search comparisons reproducible; a larger budget is not a guarantee of better
realized cost under imperfect forecasts.

## Implement one module

Place each algorithm in its own module under `optimizer/planners/`; for example,
`greedy.py`, `local_search.py`, or `beam_search.py`. Keep algorithm-specific tests in
`tests/optimizer/test_<name>_planner.py` and implementation notes in
`docs/planners/<name>.md`. No shared registry edit is needed. A factory receives
strict keyword-only options and returns a callable with this interface:

```python
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .shared import wrap_battery_planner

def factory(*, max_evaluations=100):
    # Validate options here, once per worker/physical equipment segment.
    def plan(params):
        # params is the existing validated OptimizationParams.
        policies = [["self_consume"] * len(params.usage_kwh)
                    for _ in params.battery_entities]
        evaluation = evaluate_schedule(params, policies)
        return build_result(params, policies, evaluation=evaluation,
                            diagnostics={"evaluations": 1,
                                         "termination_reason": "complete"})
    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
```

Policies are **battery-major**, in request battery order, covering the entire
supplied forecast horizon. Actions are `self_consume`, `preserve`, `grid_charge`.
Grid charging must be permitted by that battery's ingress mask. Return each
battery exactly once with finite projected levels. `build_result` adds the shared
HA reporting and advisory recommendation fields. `wrap_battery_planner` adds
comfort scheduling/continuity and whole-call timing. The inner battery search
receives modeled comfort consumption as part of its usage forecast. Both direct
factory and registry calls therefore support the same live features. The
simulator still validates a narrower battery-only execution contract.

The registry-wide tests in `test_planner_contract.py` already apply
`tests.optimizer.planner_conformance.assert_conforms` to every built-in planner.
Those cases cover empty equipment, multiple batteries, efficiencies, negative
prices, PV-only ingress, targets and an initial level below reserve. Do not copy
that parametrization into each implementation's file. External factories can use
the same helper explicitly. Shared live-feature and discrete-search suites cover
their respective contracts; see [test organization](development.md#testing).
Keep additional algorithm-specific fixtures, option boundaries and regressions
that exercise distinct behavior. Use closed-loop simulator runs to compare
outcomes together with terminal SoC and target misses.

## Shared objective and physics

`evaluate_schedule(params, policies) -> Evaluation` uses only request forecasts,
through neutral `optimizer/physics.py`. Its score is a tuple to **minimize**:

1. Sum of battery target misses in kWh, after deadline tolerance and a 1e-6 kWh
   numerical tolerance. Targets apply at the end of their inclusive deadline slot.
2. Tariff net cost + `throughput_cost_per_kwh * throughput_kwh` +
   `mode_switch_cost * mode_switches` + 5000 times the sum of end-of-slot
   reserve shortfalls in kWh, matching the original solver's recovery preference.

Throughput is AC charging input plus delivered discharge. Switching counts changes
in actual charge/idle/discharge behavior, initially idle, using the inclusive
`action_deadband_kwh` threshold (numeric zero is always idle). Physical policy
execution is not deadband-clipped. Reported tariff cost excludes preference
penalties. This tuple objective is deliberately different from HiGHS's scalar
soft target penalty and internal continuous-control objective; compare realized
simulation outcomes, not the numeric objectives across algorithms.

There is no terminal energy credit, battery energy export, PV curtailment, or
invented load. Final SoC is reported separately. Physical discharge stops at the
reserve. An observation already below reserve is preserved and its initial
shortfall is reported without manufacturing energy. Optimizing planners use an
immediate reserve-recovery seed or search action and the disclosed shortfall
penalty. Self-consumption deliberately remains a passive battery baseline.
Recovery penalties never enter reported tariff cost. See
[planner presets](optimizer-profiles.md) for supported features and limitations.
`build_result` accepts a previously computed `evaluation` to avoid replaying an
accepted candidate; it must correspond to those exact params and policies.

The evaluator scores the **full supplied horizon**. It does not apply
`lookahead_slots`. Each algorithm must state whether it uses that preference or
has a separate search horizon, then update its capability declaration. A shorter
search still returns a complete schedule and may score its completed tail.
Do not truncate the request and lose targets or report terminal energy at a
different date without explicit diagnostics.

## Capabilities, options and budgets

Attach `PlannerCapabilities` to the returned callable. Defaults reject comfort
inputs, explicitly ignore optional recommendations, optimize battery targets,
and declare `rolling_window_slots`, `lookahead_slots`,
`solver_time_limit_seconds`, and `infer_battery_preserve_policy` unused. Adjust
`ignored_preferences` to the implementation's behavior. Ignored input preferences
appear in diagnostics; normal archived requests do not fail merely because they
contain legacy solver preferences. Self-consumption records target outcomes but
declares targets `observed`; it cannot claim to meet them. These are the defaults
for a bare battery implementation: the shared wrapper advertises comfort and
optional support and activates `rolling_window_slots`. HiGHS-specific solve,
lookahead and preserve-inference preferences remain disclosed as unused by the
alternatives. Presets provide each algorithm's own fixed search configuration.

`options` configure the implementation; `settings` override allowed shared request
preferences. Unknown keyword options fail factory signature validation. Reject
invalid option values in the factory. For example:

```json
{"variants": [
  {"name": "reference", "planner": "highs"},
  {"name": "baseline", "planner": "self-consumption"},
  {"name": "candidate", "planner": "custom_components.wattplan.optimizer.planners.local_search:factory",
   "options": {"max_evaluations": 100}}
]}
```

`SearchBudget(max_evaluations, max_seconds=None)` counts attempts via `take()`
**before** each candidate evaluation. `diagnostics()` reports count, termination
reason, and search time. Include seeds and restart counts in algorithm diagnostics
when relevant. Fixed evaluation counts and fixed seeds give reproducible search;
an optional wall deadline is hardware-dependent. Search deadlines are cooperative,
checked between candidates; the simulator separately kills a worker exceeding the
remaining whole-replay budget. Count initial candidates and final replays
honestly. No automatic solver fallback is hidden by the shared factory.

## State ownership

Return `state=None` for stateless algorithms. Otherwise return the existing
base64 JSON form (`v=1`, using `models.encode_state_blob`). `create_planner` wraps
this in an envelope identifying canonical factory path, options, and
`state_version` (a positive integer attribute, default 1). The matching factory
receives its own inner blob on the next cycle. Another implementation, options,
or state version resets it with a diagnostic reason. Built-in and legacy alias
paths share identity. Only HiGHS accepts unwrapped legacy state.

The shared comfort wrapper may return state even when its inner battery search
is stateless. It carries minimum-run commitments separately from battery state
and checks configuration, aligned time, observed modes and history before reuse.

The envelope proves **ownership, not applicability**. Re-evaluate cached policies
against current forecasts, SoC, equipment, targets and absolute plan time before
using them. A changed target or time advance must never reuse an old score.
HiGHS retains its existing fingerprint and cadence checks. Worker recreation at
equipment-segment boundaries also clears private closure state. Each variant and
repeat has its own worker, and archived real-world state never seeds a simulation.

## Reproducible data before a full real day exists

```bash
python scripts/generate_simulation_data.py /tmp/wattplan-synthetic
python scripts/simulate_planner.py /tmp/wattplan-synthetic --validate-only
python scripts/simulate_planner.py /tmp/wattplan-synthetic --output /tmp/wattplan-reference
```

The generator writes a complete UTC day (96 fifteen-minute cycles), 24-hour
forecasts, two heterogeneous batteries, an absolute charging deadline, PV,
negative import/export tariffs, and independently perturbed actual usage/PV.
Every record is marked synthetic; no real future actuals supply a forecast.
`--days N` provides multiple full days; `--stress` starts below reserve and adds
an unreachable exact deadline. `--start YYYY-MM-DD` fixes the first day. The
output folder must be empty and is validated immediately with strict gap checks.
Keep generated/private data and reports outside the repository. Both factory
code hashes and input hashes appear in simulator reports.
