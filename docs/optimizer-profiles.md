# Planner configurations

Each entry in Home Assistant's planner dropdown is a complete **configuration**
that you can choose and run. The dropdown calls this field **Planner
configuration**. Related configurations
are grouped below by their underlying planner so you can compare their behavior.

There are **19 planners and 112 configurations**. The dropdown displays the
**configuration name**; exports and simulator commands use its **Id**. The four
normal choices below are **non-experimental**. **Charge-window search** is the
default for new setups. Existing setups keep their algorithm, including those
that used the previous implicit HiGHS default. Enable **Show experimental configurations** under **Options →
Troubleshooting** to see the rest. A selected experimental configuration stays
visible if experimental configurations are hidden again.

## Choosing a configuration

Choose a configuration in WattPlan's General settings. Battery reserve, targets,
and charging permissions remain separate battery settings. Configurations use
fixed neutral switching and wear preferences.

| Configuration | How it plans |
| --- | --- |
| [Charge-window search](#charge-window-search) | Searches and refines charging, preservation, and discharging blocks. |
| [Guarded value policy](#solar-gated-value-policy) | Uses forecast energy value and guards to decide when to intervene. |
| [Joint seed search](#joint-seed-search) | Compares complete joint battery plans, including supplied availability and departure constraints. |
| [Self-consumption](#self-consumption) | Stores permitted surplus energy and uses it for demand, without economic grid charging. |

Each algorithm receives all batteries configured in WattPlan. Availability,
external energy withdrawals, and departure requirements are battery constraints,
not separate types of home or a way to select which batteries are managed.
Algorithms differ in how they search and which constraints they support; see
their behavior and limits below. Comfort scheduling and optional-appliance
recommendations are shared features, so their counts do not require separate
configuration choices.

Configurations of the same planner share its core features. For example,
`highs-16` and `highs-24` both use HiGHS with different lookahead lengths.
Larger search budgets allow more alternatives, take more computation, and do
not guarantee greater savings. Forecast accuracy and equipment also affect the
result. Switching configurations reloads the integration and replaces the
previous plan.

### Relative timing and savings

The four normal choices compare as follows, using **Self-consumption** as the
baseline. Figures are rounded, indicative comparisons of individual forecast
plans.

| Configuration | Planning time vs baseline | Savings vs baseline |
| --- | ---: | ---: |
| [Charge-window search](#charge-window-search) | About 25× | About 23% lower net electricity cost |
| [Guarded value policy](#solar-gated-value-policy) | About 11× | About 22% lower net electricity cost |
| [Joint seed search](#joint-seed-search) | About 6× | About 21% lower net electricity cost |
| [Self-consumption](#self-consumption) | 1× (baseline) | No additional savings (baseline) |

Planning time is the median multiple of Self-consumption's time on matching
inputs; a larger multiple means more computation. Savings compare average net
electricity costs on the same inputs, including export credits. Self-consumption
already benefits from storing solar energy; this column shows the additional
reduction from changing algorithms.

Tariffs, forecasts, battery constraints, planning horizon, and energy left in
batteries at the end affect the cost comparison. Planning time also depends on
the number of batteries, comfort loads, and optional loads. Optional-load
suggestions contribute to timing; their execution is excluded from savings.
These percentages describe modeled plans and do not predict annual bill savings.
Small differences between the optimizing choices should not be treated as a
fixed ranking. Use the [simulator](simulator.md) to compare them with your own
recordings.

## Planners

Click a planner name for its complete configuration table. **Good for**
highlights a planner's main strength; **-** means no distinct advantage is
highlighted.

| Planner | Id | Configurations | Short description | Good for |
| --- | --- | ---: | --- | --- |
| [HiGHS](#highs) | `highs` | 4 | Optimizes variable battery charge and discharge power with a mixed-integer solver. | - |
| [Self-consumption](#self-consumption) | `self-consumption` | 1 | Stores surplus solar and uses it for demand above reserve. | Solar self-consumption with minimal computation. |
| [Greedy](#greedy) | `greedy` | 2 | Accepts cheap-charge and later-use improvements in sequence. | - |
| [Local search](#local-search) | `local-search` | 2 | Improves a complete schedule by changing individual actions or charging blocks. | Fast price-aware planning with `local-search-3`. |
| [Beam search](#beam-search) | `beam-search` | 4 | Retains several promising schedules while advancing through the forecast. | Balancing search depth and runtime with a small beam. |
| [Dynamic programming](#dynamic-programming) | `dynamic-programming` | 2 | Searches grouped battery states with bounded transitions and future-value guidance. | Price optimization for one or a few batteries. |
| [Charge-window search](#charge-window-search) | `charge-window` | 1 | Moves and refines blocks of charging, preservation, and discharging. | Searching improvements across complete charging and discharging periods. |
| [Genetic search](#genetic-search) | `genetic` | 2 | Combines and mutates complete candidate schedules. | - |
| [Cross-entropy search](#cross-entropy-search) | `cross-entropy` | 1 | Samples schedules and increases the probability of successful actions. | - |
| [Local refinement](#local-refinement) | `local-refinement` | 12 | Compares complete schedules using charging margins and price quantiles. | - |
| [Price reserve](#price-reserve) | `price-reserve` | 15 | Uses tariff quantiles and forecast solar or demand to decide when to charge or hold. | - |
| [Value policy](#value-policy) | `value-policy` | 15 | Estimates future energy value on a coarse battery-energy grid. | - |
| [Linear dispatch](#linear-dispatch) | `linear-dispatch` | 4 | Solves a continuous approximation and converts it into inverter actions. | Fast price shifting for one battery. |
| [Solar-gated value policy](#solar-gated-value-policy) | `hybrid-policy` | 36 | Switches between passive solar use and value dispatch using forecast surplus. | Price shifting while staying passive when solar is sufficient. |
| [Adaptive solar policy](#adaptive-solar-policy) | `adaptive-solar-policy` | 6 | Adjusts value-dispatch uncertainty and charging premium from forecast surplus. | Low-solar price shifting with forecast-dependent uncertainty. |
| [Mobility service baseline](#mobility-service-baseline) | `mobility-baseline` | 1 | Charges a connected mobile battery at its first opportunity to cover service needs. | Simple mobile-battery service scheduling. |
| [Mobility tariff seeds](#mobility-tariff-seeds) | `mobility-tariff-seeds` | 1 | Compares immediate mobile charging with a cheaper connected-slot schedule. | Shifting EV charging to cheaper connected slots. |
| [Joint seed search](#joint-seed-search) | `mobility-hybrid-seeds` | 1 | Compares four complete joint battery plans with supplied availability and service constraints. | Comparing complete plans while accounting for intermittent battery availability. |
| [Mobility fleet beam](#mobility-fleet-beam) | `mobility-fleet-beam` | 2 | Searches joint household and mobile-battery actions within service constraints. | Joint household/EV dispatch with surplus solar. |

All planners share the [features and limits](#shared-features-and-limits) described below.
Numbers in HiGHS configuration IDs count lookahead **slots**, not hours.
At 15-minute resolution, `highs-16` looks four hours ahead; at hourly
resolution it looks 16 hours ahead. `highs` uses 22 slots. Every configuration
still returns a schedule for the complete planning horizon.
Price margins and premiums use your tariff currency per kWh. Forecast scaling,
uncertainty, and terminal credits below affect search; reported costs use original
inputs without terminal search credit. Larger search budgets do not guarantee a
better result.

## HiGHS

Optimizes variable battery charge and discharge power with a mixed-integer solver.

Each configuration changes only the lookahead length; it still returns a schedule for the full planning horizon.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| HiGHS — 22 lookahead slots | `highs` | 22 lookahead slots; retained for existing selections. | Experimental |
| HiGHS — 8 lookahead slots | `highs-8` | 8 lookahead slots. | Experimental |
| HiGHS — 16 lookahead slots | `highs-16` | 16 lookahead slots. | Experimental |
| HiGHS — 24 lookahead slots | `highs-24` | 24 lookahead slots. | Experimental |

### Behavior and limits

HiGHS models battery charging and discharging together, including variable power
levels. This offers finer control within its model than planners that search
only charge, preserve, and self-consumption actions. More batteries and longer
lookahead can require substantially more computation. A limited solve can miss a
target or fail to produce a plan, and the inverter's execution of the published
actions can differ from the modeled power levels.

## Self-consumption

Stores surplus solar and uses it for demand above reserve.

Passive baseline: permitted surplus PV is stored automatically. Battery targets are observed, without active target charging.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Self-consumption | `self-consumption` | Baseline solar storage and passive battery use; no economic grid charging. | **Non-experimental** |

### Behavior and limits

This is the simple, low-computation battery option: store permitted surplus solar
and use stored energy for household demand above the configured reserve. It does
not charge from the grid, save energy for expensive hours, or actively pursue
battery targets. It reports missed targets and reserve shortfalls. Comfort
scheduling and optional-appliance suggestions remain available.

## Greedy

Accepts cheap-charge and later-use improvements in sequence.

The evaluation limit is the maximum candidate budget, not a guarantee that every candidate is useful.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Greedy — 8 evaluations | `greedy-8` | At most 8 candidate evaluations. | Experimental |
| Greedy — 256 evaluations | `greedy-256` | At most 256 candidate evaluations. | Experimental |

### Behavior and limits

Greedy searches for straightforward cheap-charge and later-use opportunities,
addressing battery targets before price improvements. It accepts improvements
in sequence, so an early choice can prevent a better combination later. Small
budgets can leave little room for price optimization, especially with several
batteries.

## Local search

Improves a complete schedule by changing individual actions or charging blocks.

Both configurations use random seed 0; the candidate evaluation budget distinguishes them.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Local search — 3 evaluations | `local-search-3` | At most 3 candidate evaluations. | Experimental |
| Local search — 96 evaluations | `local-search-96` | At most 96 candidate evaluations. | Experimental |

### Behavior and limits

Local search improves a complete schedule by moving individual actions or blocks
of charging and use. It can refine opportunities that a simple greedy pass
misses, but can settle on a schedule that needs several coordinated changes to
improve further. Most changes affect one battery at a time, limiting its ability
to coordinate several batteries.

## Beam search

Retains several promising schedules while advancing through the forecast.

All configurations use 24-slot guidance at scale 1, a 30,000-expansion budget, and at most 81 joint actions. Only beam width changes.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Beam search — width 1 | `beam-1` | Retains one schedule per step. | Experimental |
| Beam search — width 2 | `beam-2` | Retains up to 2 schedules per step. | Experimental |
| Beam search — width 8 | `beam-8` | Retains up to 8 schedules per step. | Experimental |
| Beam search — width 16 | `beam-16` | Retains up to 16 schedules per step. | Experimental |

### Behavior and limits

Beam search keeps several possible schedules as it advances through the forecast.
This can preserve an initially costly charging choice that pays off later. It
fits one or a few batteries more naturally than large groups, where the number
of joint actions grows quickly. A wider beam retains more alternatives at greater
computational cost; discarded paths cannot be recovered later.

## Dynamic programming

Searches grouped battery states with bounded transitions and future-value guidance.

Both configurations cap joint actions at 81; state grouping, retained states, expansion budget, and value horizon differ.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Dynamic programming — fast | `dynamic-programming-fast` | 0.1 kWh state grouping; up to 8 states; 6,000 expansions; 24 value-guidance slots. | Experimental |
| Dynamic programming — strong | `dynamic-programming-strong` | 0.25 kWh state grouping; up to 32 states; 20,000 expansions; 144 value-guidance slots. | Experimental |

### Behavior and limits

This planner groups similar battery states to avoid exploring the same kinds of
schedule repeatedly. It is suited to one or a few batteries; the number of state
combinations grows with each battery. State grouping and search limits make this
implementation approximate: they can discard a path needed for a better price
or a later target. The strong configuration searches more extensively than the fast
configuration without guaranteeing a better result.

## Charge-window search

Moves and refines blocks of charging, preservation, and discharging.

Searches charging/preservation windows with a fixed candidate budget.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Charge-window search | `charge-window-96` | 96 evaluations; 3 candidate windows; default for new setups. | **Non-experimental** |

### Behavior and limits

Charge-window search moves and refines blocks of charging, preservation, and
discharging. Its approach fits broad cheap and expensive periods, but may miss
scattered opportunities or improvements requiring coordinated changes across
batteries. With a small search budget, target charging can remain early even
when a cheaper feasible period exists later.

## Genetic search

Combines and mutates complete candidate schedules.

Both use 8 generations, 2 elites, tournament size 3, mutation rate 0.9, crossover rate 0.8, seed 0, and no separate wall-time limit.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Genetic — fast | `genetic-fast` | 16 evaluations; population 6; 24-slot guidance. | Experimental |
| Genetic — standard | `genetic-standard` | 48 evaluations; population 8; 144-slot guidance. | Experimental |

### Behavior and limits

Genetic search combines and varies complete candidate schedules. It can explore
combinations beyond a sequence of small improving changes, but only samples a
limited part of the possibilities. Longer horizons and more batteries make that
search harder. The configurations use fixed random seeds so identical inputs can be
compared consistently.

## Cross-entropy search

Samples schedules and increases the probability of successful actions.

Fixed-seed probabilistic search over coherent action blocks.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Cross-entropy — 48 evaluations | `cem-48` | 48 evaluations; population 12; top 25% elites; smoothing 0.7; probability floor 0.02; 4-slot blocks; guidance 0.85; seed 0. | Experimental |

### Behavior and limits

Cross-entropy search samples schedules and adjusts the likelihood of actions
found in better candidates. It encourages coherent operating periods, but can
miss dependencies between actions far apart in time or across batteries. Like
genetic search, its configuration uses a fixed random seed and a bounded search budget.

## Local refinement

Compares complete schedules using charging margins and price quantiles.

Base settings: no extra grid-charge margin, no preservation quantile, and tariff quantile 0. The table shows the rule each configuration adds.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Local refinement — grid margin 0.1/kWh | `local-margin-0.1` | Extra grid-charge margin 0.1/kWh. | Experimental |
| Local refinement — grid margin 0.3/kWh | `local-margin-0.3` | Extra grid-charge margin 0.3/kWh. | Experimental |
| Local refinement — grid margin 0.6/kWh | `local-margin-0.6` | Extra grid-charge margin 0.6/kWh. | Experimental |
| Local refinement — preservation quantile 0 | `local-preserve-0.0` | Preservation price quantile 0. | Experimental |
| Local refinement — preservation quantile 0.15 | `local-preserve-0.15` | Preservation price quantile 0.15. | Experimental |
| Local refinement — preservation quantile 0.35 | `local-preserve-0.35` | Preservation price quantile 0.35. | Experimental |
| Local refinement — preservation quantile 0.5 | `local-preserve-0.5` | Preservation price quantile 0.5. | Experimental |
| Local refinement — preservation quantile 0.65 | `local-preserve-0.65` | Preservation price quantile 0.65. | Experimental |
| Local refinement — preservation quantile 0.8 | `local-preserve-0.8` | Preservation price quantile 0.8. | Experimental |
| Local refinement — preservation quantile 0.95 | `local-preserve-0.95` | Preservation price quantile 0.95. | Experimental |
| Local refinement — tariff quantile 0.05, preservation 0.35 | `local-tariff-0.05-preserve-0.35` | Preservation price quantile 0.35; grid-charging tariff quantile 0.05. | Experimental |
| Local refinement — tariff quantile 0.1, preservation 0.35 | `local-tariff-0.1-preserve-0.35` | Preservation price quantile 0.35; grid-charging tariff quantile 0.1. | Experimental |

### Behavior and limits

Local refinement (`local-margin-*`, `local-preserve-*`, `local-tariff-*`) compares a small set of
complete schedules. The margin is an additional required forecast saving per
kWh of grid charging, in your tariff currency. Preservation variants add a
price-quantile rule for holding energy. Tariff variants also set the price
quantile used to construct a grid-charging candidate. These controls can avoid marginal trades,
but can also reject useful charging opportunities.

## Price reserve

Uses tariff quantiles and forecast solar or demand to decide when to charge or hold.

The table spells out the charge/discharge tariff percentiles, charging margin, PV reserve, reserve horizon, and fill limit. Margins use the tariff currency per kWh.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Price reserve — charge 5%, discharge 15%, margin 0.1 | `price-reserve-c5-d15-m10-p50-r12-f100` | Charge/discharge percentiles 5%/15%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 15%, discharge 35%, margin 0.1 | `price-reserve-c15-d35-m10-p50-r12-f100` | Charge/discharge percentiles 15%/35%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 25%, discharge 50%, margin 0.1 | `price-reserve-c25-d50-m10-p50-r12-f100` | Charge/discharge percentiles 25%/50%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 15%, discharge 35%, margin 0.3 | `price-reserve-c15-d35-m30-p100-r12-f100` | Charge/discharge percentiles 15%/35%; margin 0.3/kWh; PV reserve 100%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 15%, discharge 15%, margin 0.1 | `price-reserve-c15-d15-m10-p50-r12-f75` | Charge/discharge percentiles 15%/15%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 75%. | Experimental |
| Price reserve — charge 15%, discharge 0%, margin 0.1 | `price-reserve-c15-d0-m10-p50-r12-f100` | Charge/discharge percentiles 15%/0%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 0%, discharge 35%, margin 0 | `price-reserve-c0-d35-m0-p0-r12-f100` | Charge/discharge percentiles 0%/35%; margin 0/kWh; PV reserve 0%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 5%, discharge 65%, margin 0.1 | `price-reserve-c5-d65-m10-p50-r12-f100` | Charge/discharge percentiles 5%/65%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 15%, discharge 80%, margin 0.1 | `price-reserve-c15-d80-m10-p50-r12-f100` | Charge/discharge percentiles 15%/80%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 25%, discharge 65%, margin 0 | `price-reserve-c25-d65-m0-p0-r12-f100` | Charge/discharge percentiles 25%/65%; margin 0/kWh; PV reserve 0%; 12-slot reserve; fill limit 100%. | Experimental |
| Price reserve — charge 5%, discharge 80%, margin 0.1 | `price-reserve-c5-d80-m10-p100-r12-f75` | Charge/discharge percentiles 5%/80%; margin 0.1/kWh; PV reserve 100%; 12-slot reserve; fill limit 75%. | Experimental |
| Price reserve — demand reserve, 12 slots, spread 0 | `price-reserve-c15-d65-m10-p50-r12-f100-demand1-fm0-grid0` | Charge/discharge percentiles 15%/65%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%; demand-based preservation; future price margin 0/kWh; economic grid charging disabled. | Experimental |
| Price reserve — demand reserve, 24 slots, spread 0.2 | `price-reserve-c15-d65-m10-p50-r24-f100-demand1-fm20-grid0` | Charge/discharge percentiles 15%/65%; margin 0.1/kWh; PV reserve 50%; 24-slot reserve; fill limit 100%; demand-based preservation; future price margin 0.2/kWh; economic grid charging disabled. | Experimental |
| Price reserve — demand reserve, 24 slots, spread 0 | `price-reserve-c15-d100-m10-p50-r24-f100-demand1-fm0-grid0` | Charge/discharge percentiles 15%/100%; margin 0.1/kWh; PV reserve 50%; 24-slot reserve; fill limit 100%; demand-based preservation; future price margin 0/kWh; economic grid charging disabled. | Experimental |
| Price reserve — demand reserve, 12 slots, spread 0.2 | `price-reserve-c15-d100-m10-p50-r12-f100-demand1-fm20-grid0` | Charge/discharge percentiles 15%/100%; margin 0.1/kWh; PV reserve 50%; 12-slot reserve; fill limit 100%; demand-based preservation; future price margin 0.2/kWh; economic grid charging disabled. | Experimental |

### Behavior and limits

Price reserve (`price-reserve-*`) uses import-price quantiles, charging losses
and forecast solar surplus to decide when to charge or hold energy. In its
names, `c` and `d` are charge and discharge percentiles, `m` is the margin in
hundredths of your tariff currency per kWh, `p` is the percentage of forecast
solar surplus reserved, `r` is the number of forecast slots considered for that
reserve, and `f` is the usable-capacity fill percentage. Full-power charging can
overshoot the intended headroom; holding energy too long can increase costs.
`demand1` variants limit preservation to energy needed for more expensive
forecast deficits before a forecast solar surplus of at least 10% of battery
capacity in one slot. `fm` gives the required future
price difference in hundredths of the tariff currency per kWh, and `grid0`
disables economic grid charging while retaining target and reserve recovery.

## Value policy

Estimates future energy value on a coarse battery-energy grid.

Base settings for comparison: 4 energy intervals, 100% of supplied PV and usage, no terminal energy credit, deterministic PV, and no extra charging knots. Rows show changes to that base.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Value policy — 4 energy intervals, PV 100% | `value-grid4-pv100-terminal0` | Base settings. | Experimental |
| Value policy — 8 energy intervals, PV 100% | `value-grid8-pv100-terminal0` | 8 energy intervals. | Experimental |
| Value policy — 4 energy intervals, PV 75% | `value-grid4-pv75-terminal0` | Search uses 75% of forecast PV. | Experimental |
| Value policy — 4 energy intervals, PV 50% | `value-grid4-pv50-terminal0` | Search uses 50% of forecast PV. | Experimental |
| Value policy — 2 energy intervals, PV 100% | `value-grid2-pv100-terminal0` | 2 energy intervals. | Experimental |
| Value policy — 2 energy intervals, PV 50% | `value-grid2-pv50-terminal0` | 2 energy intervals; search uses 50% of forecast PV. | Experimental |
| Value policy — 6 energy intervals, PV 100% | `value-grid6-pv100-terminal0` | 6 energy intervals. | Experimental |
| Value policy — 4 energy intervals, terminal value 25% | `value-grid4-pv100-terminal25` | Terminal value credit 25%. | Experimental |
| Value policy — 4 energy intervals, terminal value 50% | `value-grid4-pv100-terminal50` | Terminal value credit 50%. | Experimental |
| Value policy — 2 energy intervals plus charging thresholds | `value-grid2-pv100-terminal0-chargeknots1` | 2 energy intervals; charging-threshold energy knots. | Experimental |
| Value policy — 4 energy intervals plus charging thresholds | `value-grid4-pv100-terminal0-chargeknots1` | Charging-threshold energy knots. | Experimental |
| Value policy — 4 energy intervals, demand 75%, terminal value 50% | `value-grid4-pv100-demand75-terminal50` | Search uses 75% of forecast usage; terminal value credit 50%. | Experimental |
| Value policy — 4 energy intervals, demand 125%, terminal value 50% | `value-grid4-pv100-demand125-terminal50` | Search uses 125% of forecast usage; terminal value credit 50%. | Experimental |
| Value policy — 4 intervals, terminal value 50%, PV spread 50% | `value-grid4-pv100-terminal50-scenario50` | Terminal value credit 50%; PV scenarios +/-50%. | Experimental |
| Value policy — 4 intervals, terminal value 50%, PV spread 100% | `value-grid4-pv100-terminal50-scenario100` | Terminal value credit 50%; PV scenarios +/-100%. | Experimental |

### Behavior and limits

Value policy (`value-grid*-pv*-terminal*`) estimates future energy value on a
coarse battery-energy grid. `grid` gives the number of energy intervals and `pv`
the percentage of forecast PV used during search. Optional `demand` gives the
percentage of forecast usage; reporting uses the original inputs. These scalings
express forecast assumptions, not corrections learned from your history.
It is designed for one battery and uses bounded joint-state dynamic
programming for multiple batteries. Coarse grids can miss narrow targets or
charging thresholds. `terminal` gives the percentage of an estimated future
energy value used to score remaining energy during search; zero disables it.
This heuristic can encourage excessive retention. Reported cost never includes
that credit.
`chargeknots1` variants add energy points at charging-rate saturation and
grid-charge eligibility thresholds. If the selected schedule misses a battery
target, a bounded target-repair search may replace it; feasibility is still not
guaranteed for every request.

`scenarioN` variants consider equally likely PV forecasts `N` percent below and
above the supplied forecast, choosing each action by its average future cost.
They still report flows and costs using the original inputs. This models
independent uncertainty in each slot, not a calibrated weather forecast, and can
overpay for protection against low solar production.

An optional `grid_charge_premium_per_kwh` adds a cost per AC kWh of battery
grid charging during value-policy search. Hybrid configurations with `-chargepremiumN`
use the explicit premium `N` in your tariff currency per kWh. The premium can
discourage weak forecast trades but also reject profitable ones. It never adds
to reported electricity cost, and the passive branch, fleet fallback and target
repair retain their existing behavior. Zero disables the heuristic.

## Linear dispatch

Solves a continuous approximation and converts it into inverter actions.

PV and terminal credit percentages affect search; reported electricity costs replay original inputs without a terminal credit.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Linear dispatch — 24 slots, rounding 50% | `linear-dispatch-h24-round50-pv100-tv0` | 24-slot LP horizon; charging-rounding threshold 50%; search PV 100%; terminal credit 0%. | Experimental |
| Linear dispatch — 36 slots, rounding 25% | `linear-dispatch-h36-round25-pv50-tv25` | 36-slot LP horizon; charging-rounding threshold 25%; search PV 50%; terminal credit 25%. | Experimental |
| Linear dispatch — marginal value, 24 slots | `linear-dispatch-marginal-h24-pv100-tv0` | 24-slot LP horizon; chooses inverter actions using marginal future-energy value; 100% PV; no terminal credit. | Experimental |
| Linear dispatch — marginal value, 36 slots | `linear-dispatch-marginal-h36-pv100-tv0` | 36-slot LP horizon; chooses inverter actions using marginal future-energy value; 100% PV; no terminal credit. | Experimental |

### Behavior and limits

Linear dispatch (`linear-dispatch-h*-round*-pv*-tv*`) solves a continuous
single-battery approximation, then rounds it into executable inverter policies.
`h` limits the search horizon in slots, `round` sets the percentage of maximum
grid charge needed to select charging, and `pv` and `tv` set forecast PV and
terminal-value percentages. `linear-dispatch-marginal-h*` instead evaluates each
legal inverter policy through shared physics, then adds the next LP energy-row
dual times its resulting stored energy to the immediate forecast cost. This dual
is a local future-cost slope at the relaxed solution; full-power actions can
cross its valid region, and degenerate LP solutions can yield different slopes.
Rounding, marginal approximation and simplified power curves can miss
opportunities or buy excess energy. The relaxation can choose PV export where
physical replay automatically stores available surplus. Negative-import slots
forbid relaxed discharge to prevent profitable lossy cycling. Targets, reserve
shortfalls and multiple batteries use deadline/reserve preparation instead of
the LP. A fresh native solver gives every eligible request its own solve budget.
Reported flows and costs always replay selected policies against the original
inputs and never include terminal search credit.

## Solar-gated value policy

Switches between passive solar use and value dispatch using forecast surplus.

Base settings for comparison: solar gate ratio 1.5, 4 energy intervals, no terminal credit, deterministic PV, zero grid-charge premium, and no extra guards or grid/rollout refinements. Rows show changes to that base. Premiums use the tariff currency per AC kWh.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Value policy — solar surplus gate 1 | `value-solar-gate-1` | Solar gate ratio 1. | Experimental |
| Value policy — solar surplus gate 1.5 | `value-solar-gate-1.5` | Base settings. | Experimental |
| Value policy — solar surplus gate 2 | `value-solar-gate-2` | Solar gate ratio 2. | Experimental |
| Value policy — solar surplus gate 1, 6 energy intervals | `value-solar-gate-1-grid6` | Solar gate ratio 1; 6 energy intervals. | Experimental |
| Value policy — solar surplus gate 1.5, 6 energy intervals | `value-solar-gate-1.5-grid6` | 6 energy intervals. | Experimental |
| Value policy — solar surplus gate 1.5, 8 energy intervals | `value-solar-gate-1.5-grid8` | 8 energy intervals. | Experimental |
| Value policy — solar surplus gate 1.5, 6 intervals, terminal value 25% | `value-solar-gate-1.5-grid6-terminal25` | 6 energy intervals; terminal value credit 25%. | Experimental |
| Value policy — solar surplus gate 1.5, 6 intervals, terminal value 50% | `value-solar-gate-1.5-grid6-terminal50` | 6 energy intervals; terminal value credit 50%. | Experimental |
| Value policy — solar surplus gate 1.5, 6 intervals, PV spread 50% | `value-solar-gate-1.5-grid6-terminal0-scenario50` | 6 energy intervals; PV scenarios +/-50%. | Experimental |
| Value policy — solar surplus gate 1.5, 6 intervals, PV spread 100% | `value-solar-gate-1.5-grid6-terminal0-scenario100` | 6 energy intervals; PV scenarios +/-100%. | Experimental |
| Value policy — solar surplus gate 1.5, 8 intervals, PV spread 100% | `value-solar-gate-1.5-grid8-terminal0-scenario100` | 8 energy intervals; PV scenarios +/-100%. | Experimental |
| Value policy — solar surplus gate 1.5, 6 intervals, pre-refill guard | `value-solar-gate-1.5-grid6-terminal0-scenario0-prerefill1` | 6 energy intervals; pre-refill opportunity guard. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, grid charge premium 0.01/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.01` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.01/kWh. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, grid charge premium 0.03/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, grid charge premium 0.1/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.1` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.1/kWh. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, charging knots, grid premium 0/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargeknots1` | 6 energy intervals; PV scenarios +/-100%; charging-threshold energy knots. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, charging knots, grid premium 0.03/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-chargeknots1` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; charging-threshold energy knots. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, two-slot first-action rollout, grid premium 0/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-rollout2` | 6 energy intervals; PV scenarios +/-100%; two-slot first-action rollout. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, two-slot first-action rollout, grid premium 0.03/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-rollout2` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; two-slot first-action rollout. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, two-slot first-action rollout, grid premium 0.1/kWh | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.1-rollout2` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.1/kWh; two-slot first-action rollout. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, paired demand spread 25%, demand horizon 12 | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots. | Experimental |
| Value policy — solar gate 1.5, PV spread 100%, paired demand spread 25%, demand horizon full | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-hfull` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for the full horizon. | Experimental |
| Value policy — paired demand spread 25% for 12 slots, two-slot first-action rollout | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-rollout2` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; two-slot first-action rollout. | Experimental |
| Value policy — paired demand spread 10% for 12 slots | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand10-h12` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-10% for 12 slots. | Experimental |
| Value policy — paired demand spread 50% for 12 slots | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand50-h12` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-50% for 12 slots. | Experimental |
| Value policy — paired demand spread 25% for 6 slots | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h6` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 6 slots. | Experimental |
| Value policy — paired demand spread 25% for 24 slots | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h24` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 24 slots. | Experimental |
| Value policy — two-slot first-action rollout with persistent PV uncertainty | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-rollout2-pvpersist2` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; two-slot first-action rollout; persistent PV uncertainty across both rollout slots. | Experimental |
| Value policy — paired demand spread 25% for 12 slots, exact initial energy knot | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot. | Experimental |
| Value policy — 8 intervals, paired demand spread 25% for 12 slots, exact initial energy knot | `value-solar-gate-1.5-grid8-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1` | 8 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot. | Experimental |
| Value policy — exact initial knot, pre-refill guard, mean-preserving asymmetric PV tail | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-pvtailq1of3` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot; pre-refill opportunity guard; PV tail factors 0/1.5 weighted 1/3 and 2/3. | Experimental |
| Value policy — exact initial knot, pre-refill guard, current-first preserve prefix | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-preserveprefix24h` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot; pre-refill opportunity guard; current-first preservation comparison over up to 24 hours. | Experimental |
| Value policy — exact initial knot, pre-refill guard, nominal joint mixture for 6 slots | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-nominalmix50-h6` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot; pre-refill opportunity guard; nominal joint scenario weighted 50% for 6 slots. | Experimental |
| Value policy — exact initial knot, pre-refill guard, per-slot discharge budget knot | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-dischargebudgetknot1` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot; pre-refill opportunity guard; per-slot discharge-budget knot. | Experimental |
| Value policy — grid-charge premium 0.05, paired demand spread 25% for 12 slots, exact initial knot, pre-refill guard | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.05-demand25-h12-initialknot1-prerefill1` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.05/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot; pre-refill opportunity guard. | Experimental |
| Guarded value policy | `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1` | 6 energy intervals; PV scenarios +/-100%; grid-charge premium 0.03/kWh; paired demand scenarios +/-25% for 12 slots; exact initial-energy knot; pre-refill opportunity guard. | **Non-experimental** |

### Behavior and limits

Solar-gated value policy (`value-solar-gate-*`) uses passive self-consumption
when the next 24 forecast hours have enough surplus PV relative to both demand
deficits and battery headroom. The suffix is the required ratio. Otherwise it
uses value dispatch with four energy intervals, or the count specified by a
`-gridN` suffix. An optional `-terminalN` suffix applies the same terminal-value
percentage as value policy. `-scenarioN` applies PV uncertainty to value dispatch;
the passive-policy gate still uses the supplied forecast. Targets, low reserves, fleets,
negative tariffs and battery preference penalties always retain value dispatch.
The gate can miss price opportunities and does not guarantee that forecast solar
arrives before demand. `-prerefill1` variants check the order of forecast slots:
they retain value dispatch if a feasible cheaper grid-charge opportunity precedes
expensive imports before solar restores the initial battery level. This is a
heuristic opportunity check, not proof that buying early will save money.
Hybrid `-chargeknots1` variants forward the value policy's additional physical
charging breakpoints to its energy grid. They retain the same passive gate,
original-input replay and fleet fallback; extra knots can improve or worsen
the approximate value table and increase its search work.

Hybrid `-initialknot1` variants add the request's exact starting battery energy
to the value table's energy grid when it is not already present. This can avoid
interpolation error when holding the current charge, but also changes the
approximation at other energy levels and adds search work. It is not a guarantee
of savings. The option defaults to false; passive dispatch, fleet fallback and
target repair omit this grid refinement.

The `value-solar-gate-1.5-grid8-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1`
variant uses eight energy intervals with the same unguarded grid6 uncertainty,
premium and initial-knot settings. The finer table adds search work and can change
interpolation choices; it does not guarantee greater savings.

The `value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1`
composition retains the same uncertainty assumptions and energy grid as its
`-initialknot1` counterpart, adding the existing pre-refill guard to reject passive
dispatch when it detects an earlier feasible cheaper grid buy; this does not
guarantee a profitable trade.

The guarded grid6 `-chargepremium0.05-demand25-h12-initialknot1-prerefill1`
variant changes only the search premium from 0.03 to 0.05. It tests whether a
stronger charging penalty improves trade selection; it does not predict a savings
gain and retains the default equal-weight PV tail.

The guarded grid6 `-dischargebudgetknot1` composition enables the optional
`include_discharge_budget_knot` (default false). It adds at most one node to each
value-table layer: reserve plus the remaining adverse demand the constant
discharge rate can serve, adjusted for discharge efficiency. It requires a
constant discharge curve and exactly zero scaled worst-case PV; unsupported
requests retain the existing tables. Nodes above capacity are omitted and
existing nodes deduplicate. Rate-limited household imports can remain unavoidable.
This refinement adds search work and does not certify optimality or savings;
original-input reporting, passive dispatch, fleet fallback and target repair
retain their existing behavior.

The guarded grid6 `-nominalmix50-h6` composition changes only the nearest six
joint uncertainty slots: adverse, nominal and optimistic outcomes receive
weights 0.25, 0.5 and 0.25, preserving both forecast means. Remaining paired
slots and the PV-only tail retain their existing assumptions. The optional
`include_nominal_joint_scenario` defaults to false; `nominal_joint_scenario_slots`
is a positive integer defaulting to six and is capped by the paired demand bound
and supplied horizon. Enabled use requires positive PV and paired demand spreads
with an explicit paired bound, and does not support first-action rollout.
These fixed probabilities are a search assumption, not calibrated confidence or
a savings prediction. Reporting replays original inputs; passive dispatch,
fleet fallback and target repair omit the mixture.

The guarded grid6 `-preserveprefix24h` composition enables `first_preserve_prefix`
(default false) only after the existing gate and pre-refill guard select passive
dispatch. It compares all-passive with preserving the first slot then consuming
passively, using original forecast tariffs and shared physics. It accepts only a
strictly cheaper prefix whose battery levels reconverge exactly within the same
relative 24-hour cap. Targets, reserve recovery, fleets,
negative tariffs and battery preferences retain value dispatch. The comparison
does not credit terminal energy or change uncertainty assumptions. Its prefix
gain is hypothetical: forecast errors and later replanning can change the result.
Full-horizon reporting replays the concrete accepted policy against original inputs.

The guarded grid6 `-pvtailq1of3` composition leaves the nearest 12 paired demand/PV
slots unchanged. Later slots use PV factors 0 and 1.5 with probabilities one third
and two thirds, preserving the forecast mean while retaining a complete-cloud
outcome. This fixed distribution is an explicit search assumption, not a
calibrated probability or a guarantee of savings. Reporting uses original inputs;
passive dispatch, fleet fallback and target repair omit the assumption.
The optional `pv_tail_low_probability` defaults to 0.5 and must be finite with
`0 < q <= 0.5`. A nondefault value requires positive PV and paired demand spreads,
an explicit positive paired demand slot bound, and disabled first-action rollout.

Hybrid `-rollout2` variants refine only the first action through two exact physical
slots before interpolating the remaining value. Each action is chosen before
that slot's PV realization; the second action may respond to the first slot's
observed successor state. Later planned actions use the ordinary value table.
The rollout leaves the passive gate, original-input replay and fleet fallback
unchanged. It is an approximate search refinement, not a guarantee of savings;
target repair can replace the proposed schedule. The option defaults to false.

The `-rollout2-pvpersist2` variant assumes the same low/high PV factor across
the two rollout slots. The first action precedes the PV outcome; the second can
use the factor only if the first slot's observed PV distinguishes it. Zero PV
or zero PV scale reveals nothing, so the next action still precedes its own PV
outcome. Equal battery levels alone do not determine what was observed. The
remaining value table retains independent PV scenarios. This explicit persistence
assumption is not calibrated and can worsen costs. It requires first-action rollout
and currently rejects pairing with demand uncertainty; default behavior is unchanged.

Hybrid `-demandN-hM` variants pair the lower PV branch with demand increased by
`N` percent, and the higher PV branch with demand decreased by that percentage.
The two equally weighted outcomes retain the supplied means and assume negative
dependence between PV and demand; this is a stress assumption, not a calibrated
forecast distribution. `hM` applies demand uncertainty only to the nearest `M`
forecast slots; `hfull` applies it throughout the supplied horizon. PV uncertainty
continues beyond that bound. Slots have the request's configured duration.
Each action precedes its own joint outcome, including when first-action rollout
is enabled. The demand is the aggregate after fixed comfort placement; comfort
is not rescheduled by branch. The passive gate and reported costs use original
inputs, while fleet fallback and target repair omit this uncertainty assumption.
Zero demand spread preserves the existing PV model. Additional branches can
increase work and cause unnecessary charging; compare a complete annual replay.
The `-demand25-h12-rollout2` configuration combines the 12-slot demand model with the
two-slot first-action refinement; compare it separately from each component.
Additional demand configurations compare spreads of 10% and 50% over 12 slots, and a
25% spread over 6 or 24 slots. These are fixed forecast assumptions, with the
same solar gate, PV spread and charging premium; none selects settings by date.

## Adaptive solar policy

Adjusts value-dispatch uncertainty and charging premium from forecast surplus.

All configurations use solar gate ratio 1.5, 6 energy intervals, and zero terminal credit. Below each switch ratio the low setting applies; at or above it the high setting applies. Premium switching is independent of PV-spread switching.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Adaptive solar policy — switch ratio 0.5, PV spread 100%/0% | `adaptive-solar-gate1.5-grid6-switch0.5-low100-high0` | PV-spread switch ratio 0.5; low/high PV spreads 100%/0%; no grid-charge premium. | Experimental |
| Adaptive solar policy — switch ratio 1, PV spread 100%/0% | `adaptive-solar-gate1.5-grid6-switch1-low100-high0` | PV-spread switch ratio 1; low/high PV spreads 100%/0%; no grid-charge premium. | Experimental |
| Adaptive solar policy — switch ratio 0.5, PV spread 100%/50% | `adaptive-solar-gate1.5-grid6-switch0.5-low100-high50` | PV-spread switch ratio 0.5; low/high PV spreads 100%/50%; no grid-charge premium. | Experimental |
| Adaptive solar policy — switch ratio 1, PV spread 100%/50% | `adaptive-solar-gate1.5-grid6-switch1-low100-high50` | PV-spread switch ratio 1; low/high PV spreads 100%/50%; no grid-charge premium. | Experimental |
| Adaptive solar policy — PV spread 100%/100%, grid premium 0/0.03 at ratio 0.01 | `adaptive-solar-gate1.5-grid6-switch0.5-low100-high100-premiumswitch0.01-low0-high0.03` | PV-spread switch ratio 0.5; low/high PV spreads 100%/100%; premium switch ratio 0.01; low/high grid-charge premiums 0/0.03 per kWh. | Experimental |
| Adaptive solar policy — PV spread 100%/50%, grid premium 0/0.03 at ratio 0.01 | `adaptive-solar-gate1.5-grid6-switch0.5-low100-high50-premiumswitch0.01-low0-high0.03` | PV-spread switch ratio 0.5; low/high PV spreads 100%/50%; premium switch ratio 0.01; low/high grid-charge premiums 0/0.03 per kWh. | Experimental |

### Behavior and limits

Adaptive solar policy (`adaptive-solar-gate1.5-grid6-switchR-low100-highN`)
chooses the PV uncertainty spread from the same next-24-hour forecast surplus
ratio used by the passive gate. Below switch ratio `R` it uses a 100% symmetric
spread; at or above it uses `N` percent. The denominator is the greater of
forecast deficits and battery AC charging headroom. The chosen hybrid policy
then runs once, retaining the passive gate at 1.5 and six energy intervals.
This is a forecast heuristic, without calendar rules or learned weather
probabilities. Switching assumptions can improve or worsen costs; compare a
complete replay before selecting it.

An optional adaptive `-premiumswitchR-lowL-highH` suffix independently chooses
the grid charging premium from that forecast ratio: below `R` it uses `L`, and
at or above it uses `H`, in tariff currency per AC kWh of battery grid charging.
Both premiums default to zero. Equal PV spreads isolate the premium selector
from uncertainty selection. Zero surplus selects the low options; positive
surplus with zero demand deficit and charging headroom selects the high options.
Exactly one configured hybrid policy runs. Selected options are diagnostics,
not a claim that passive dispatch, fleet fallback or target repair use premiums.
This forecast assumption can prevent useful trades and does not guarantee savings.

## Mobility service baseline

Charges a connected mobile battery at its first opportunity to cover service needs.

Requires supplied connection windows and departure/withdrawal requirements. Household batteries remain passive.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Mobility — first-available service baseline | `mobility-baseline` | One first-available service schedule with passive household batteries. | Experimental |

## Mobility tariff seeds

Compares immediate mobile charging with a cheaper connected-slot schedule.

Requires supplied mobile-battery constraints. Household batteries remain passive; the complete service baseline is retained as a candidate.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Mobility — two service/tariff seed candidates | `mobility-tariff-seeds-2` | Two complete candidates: first-available and tariff-delayed mobile charging. | Experimental |

## Joint seed search

Compares four complete joint battery plans.

Four joint candidates combine passive or grid4 value control with first-available
or tariff-aware charging for batteries with supplied connection and departure
constraints. All configured batteries are included, and connection constraints
are optional. Batteries are identified by their supplied constraints rather
than by a vehicle or home label.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Joint seed search | `mobility-hybrid-seeds-4-grid4` | Four complete joint candidate schedules; value grid has 4 energy intervals; supplied availability and service constraints are included. | **Non-experimental** |

## Mobility fleet beam

Searches joint household and mobile-battery actions within service constraints.

Requires supplied mobile-battery constraints. Both configurations cap joint actions at 27 and retain the complete service baseline as a candidate.

| Configuration name (UI) | Id | What distinguishes this configuration | Status |
| --- | --- | --- | --- |
| Mobility fleet beam — width 4, 1500 expansions | `mobility-fleet-beam-4-exp1500-joint27` | Beam width 4; 1,500 transition expansions. | Experimental |
| Mobility fleet beam — width 8, 6000 expansions | `mobility-fleet-beam-8-exp6000-joint27` | Beam width 8; 6,000 transition expansions. | Experimental |

## Comparing configurations

Use the [simulator](simulator.md) to compare configurations with your own
exported planning inputs. List the available configuration IDs and names:

```bash
python scripts/simulate_planner.py --list-planners
```

This includes experimental configurations and bare algorithm choices; it does
not print experimental status. Use configuration IDs with `--planners` to run
the same choices offered by the Home Assistant dropdown.

## Mobility-aware planners

These API configurations support batteries with supplied connection windows, repeated
departure energy requirements and end-of-slot external DC energy withdrawals,
such as an EV. They do not acquire a vehicle calendar or infer future trips.
Other planner families reject active mobility inputs; selecting a mobility
configuration is explicit and does not rename an existing algorithm. No new Home
Assistant vehicle source configuration or automatic routing is provided.

`mobility-baseline` keeps stationary batteries in self-consumption. It charges a
mobile battery from the grid at its first connected opportunity until it has
enough forecast energy for its next departure and intervening withdrawals, then
preserves that energy. Permitted PV absorption remains automatic. If no departure
remains, it considers remaining withdrawals and reserve. A full charging slot can
overshoot a minimum requirement; it is an inverter policy, not a fractional
charge setpoint. Existing ordinary battery targets are observed by this baseline.

The new mobility planners identify mobile control by departure requirements or
explicit connection/external-energy metadata, even if the current horizon is connected with
zero draws and no departure. This keeps the baseline preserving the vehicle
during a horizon without a visible trip; batteries with no such metadata remain
stationary. The legacy capability rule separately treats all-true/zero metadata
as inert for backward compatibility.

`mobility-tariff-seeds-2` compares that complete baseline with one schedule that
delays costly charging to cheaper supplied connected slots before the next
departure or withdrawal. Its rate-based delay test is optimistic about curves,
PV competition and saturation; full shared evaluation retains the baseline if
the alternative is worse. It performs two complete candidate evaluations and
does not search all economic schedules.
Both these specialized controls leave metadata-free household batteries passive.
They observe ordinary household battery targets and reserve shortfalls rather
than deliberately charging the household battery to recover reserve or meet
targets. Mobile service charging is separate. The tariff seed's canonical score
still includes observed target misses when comparing its two candidates, but
that limited candidate construction does not pursue ordinary battery targets.

`mobility-hybrid-seeds-4-grid4` evaluates four complete joint schedules: passive
or raw hybrid-policy stationary control, crossed with first-available or tariff
mobile charging. The stationary search uses grid 4 and default deterministic
forecasts; integrated comfort demand is prepared once. Mobile actions are
recomputed from each candidate's joint battery levels and shared PV allocation.
The exact first-available/passive baseline is retained, including duplicate
seeds. Full service-first scoring chooses among four candidates; it does not
search arbitrary fleet schedules or model branch uncertainty. Multiple stationary
batteries inherit hybrid-policy's bounded fleet fallback. An ordinary mobile
battery target instead dispatches the whole fleet to beam width 4 with 1500
expansions, retaining the service baseline and explicitly reporting that fallback.
Neither branch guarantees target/service feasibility, optimality or annual savings.

The fleet beam searches joint charge/preserve/self-consumption policies, including
using vehicle energy for household demand and replenishing it before departure.
The names specify beam width, transition expansion budget, and a cap of 27 joint
actions per prefix. Availability, PV allocation, charging permissions, variable
curves, reserve and efficiency remain shared physical rules. Optimistic future
recharge guidance ranks prefixes; it is not a joint service-feasibility proof.
Pruning and capped action enumeration can miss useful or feasible schedules.
The complete service baseline remains a candidate even if the search exhausts
its budget. Mandatory seed construction, tail completion, full candidate replay,
comfort scheduling and reporting add work outside the transition budget. No
annual runtime or greater-savings guarantee is implied by a larger configuration.

For active mobility requests the objective first minimizes departure shortfalls
plus unserved external energy, then ordinary target misses, then tariff and
configured wear/switch/reserve-recovery penalties. Vehicle trip energy is a DC
battery withdrawal, not household import or export, inverter throughput, or an
invented tariff charge. Forecast service can be impossible due to capacity,
connection windows, charge rates or the discrete charging policy; reported
`departure_checks`, `mobility_unserved_kwh` and `service_feasible` describe the
accepted schedule, not a certificate that no other schedule could meet service.
Reported electricity costs do not credit remaining terminal battery energy.
The normal comfort wrapper runs once and optional appliances remain advisory.

The planner capability `economic_scope` describes household economics
(`household`), only mobility charging economics (`mobility`), or no economic
search (`none`). Household scope can also support mobility when the separate
mobility capability is true, as fleet beam does. `reserve_recovery=True` means
deliberate initial-below-reserve recovery is pursued across the supported fleet,
subject to ingress and physical limits; false means that behavior is only
observed or incidental. These declarations describe planner scope, not
feasibility or optimality guarantees. They distinguish economic search from
passive controls without silently changing a baseline into an optimizer.

## Shared features and limits

All configurations provide battery schedules, comfort schedules, optional-appliance
suggestions, cost and energy projections, and warnings about unmet constraints.
Feature support does not mean identical schedules or equally low costs.
Optimizing planners try to meet battery targets and recover reserve shortfalls;
self-consumption only observes them. Search limits and discrete battery actions
can leave targets unmet, including exact targets that a variable-power model
could reach. Check the [plan health](error-handling.md) when a deadline matters.

Comfort loads are scheduled to meet their runtime constraints, with a bounded
search for cheaper placement. The alternative planners fix that comfort demand
before optimizing batteries; HiGHS can also reconsider its battery plan after
moving comfort. None performs an unrestricted joint search over all load start
times and battery actions.

Optional appliances are recommendations, ranked independently by their extra
electricity cost under the accepted battery plan. Equal-cost starts favor the
earlier time. The first suggestion is not automatically run, and suggestions for
different appliances do not reserve capacity against one another. Use your
[automations](extras.md) to control devices and coordinate competing loads.

Projected cost is grid import cost minus export revenue. It excludes battery
wear estimates and optional appliances you have not scheduled. A cheaper plan
may also finish with less stored energy: compare final battery levels and target
misses alongside cost. Reported costs do not give leftover energy a separate
end-of-plan credit. Some value-policy variants use such a credit internally to
guide their search; other planners can favor using energy down to the reserve.

## Battery operation settings

Configurations use fixed algorithm settings. They do not set a battery's physical
minimum reserve, charging permissions, or target; configure those directly on
the battery. Projected tariff cost includes grid import cost minus export
revenue, without a monetary estimate for battery wear. Use
[historical cost tracking](historical-cost-tracking.md) to assess costs on your
own installation.
