# Planners and battery behavior

Choose a **Planner preset** in WattPlan's General settings. A preset selects an
algorithm and its search effort; **Battery behavior** separately controls how
readily the planner cycles the battery. The default is **HiGHS — existing
settings**, which retains your configured lookahead.

## Choosing a preset

Presets of the same algorithm share its features. For example, `highs-16` and
`highs-24` both use HiGHS, with different lookahead lengths. Larger search budgets
allow more alternatives to be considered, but take more computation and do not
guarantee greater savings. Forecast accuracy and your equipment also affect the
result. Switching presets reloads the integration and replaces the previous plan.

| Planner | Available presets |
| --- | --- |
| HiGHS | `highs`, `highs-8`, `highs-16`, `highs-24` |
| Self-consumption | `self-consumption` |
| Greedy | `greedy-8`, `greedy-256` |
| Local search | `local-search-3`, `local-search-96` |
| Beam search | `beam-1`, `beam-2`, `beam-8`, `beam-16` |
| Dynamic programming | `dynamic-programming-fast`, `dynamic-programming-strong` |
| Charge-window search | `charge-window-96` |
| Genetic search | `genetic-fast`, `genetic-standard` |
| Cross-entropy search | `cem-48` |

The numbers in HiGHS preset names count **forecast slots**, not hours. At
15-minute resolution, `highs-16` looks four hours ahead and `highs-24` six hours;
at hourly resolution they look 16 and 24 hours ahead. WattPlan still returns a
schedule for the complete planning horizon. The editable HiGHS lookahead setting
applies only to `highs`. Other presets have fixed search settings.

### HiGHS

HiGHS models battery charging and discharging together, including variable power
levels. This offers finer control within its model than planners that search
only charge, preserve, and self-consumption actions. More batteries and longer
lookahead can require substantially more computation. A limited solve can miss a
target or fail to produce a plan, and the inverter's execution of the published
actions can differ from the modeled power levels.

### Self-consumption

This is the simple, low-computation battery option: store permitted surplus solar
and use stored energy for household demand above the configured reserve. It does
not charge from the grid, save energy for expensive hours, or actively pursue
battery targets. It reports missed targets and reserve shortfalls. Comfort
scheduling and optional-appliance suggestions remain available.

### Greedy

Greedy searches for straightforward cheap-charge and later-use opportunities,
addressing battery targets before price improvements. It accepts improvements
in sequence, so an early choice can prevent a better combination later. Small
budgets can leave little room for price optimization, especially with several
batteries.

### Local search

Local search improves a complete schedule by moving individual actions or blocks
of charging and use. It can refine opportunities that a simple greedy pass
misses, but can settle on a schedule that needs several coordinated changes to
improve further. Most changes affect one battery at a time, limiting its ability
to coordinate several batteries.

### Beam search

Beam search keeps several possible schedules as it advances through the forecast.
This can preserve an initially costly charging choice that pays off later. It
fits one or a few batteries more naturally than large groups, where the number
of joint actions grows quickly. A wider beam retains more alternatives at greater
computational cost; discarded paths cannot be recovered later.

The preset number limits how many alternatives are retained: `beam-1` uses the
least search effort, `beam-2` is a fast starting choice, and `beam-8` and
`beam-16` explore more alternatives. All four share the same features and other
search settings. Compare their runtime and cost with your own exports using the
[simulator](simulator.md); a wider beam does not guarantee a cheaper result.

### Dynamic programming

This planner groups similar battery states to avoid exploring the same kinds of
schedule repeatedly. It is suited to one or a few batteries; the number of state
combinations grows with each battery. State grouping and search limits make this
implementation approximate: they can discard a path needed for a better price
or a later target. The strong preset searches more extensively than the fast
preset without guaranteeing a better result.

### Charge-window search

Charge-window search moves and refines blocks of charging, preservation, and
discharging. Its approach fits broad cheap and expensive periods, but may miss
scattered opportunities or improvements requiring coordinated changes across
batteries. With a small search budget, target charging can remain early even
when a cheaper feasible period exists later.

### Genetic search

Genetic search combines and varies complete candidate schedules. It can explore
combinations beyond a sequence of small improving changes, but only samples a
limited part of the possibilities. Longer horizons and more batteries make that
search harder. The presets use fixed random seeds so identical inputs can be
compared consistently.

### Cross-entropy search

Cross-entropy search samples schedules and adjusts the likelihood of actions
found in better candidates. It encourages coherent operating periods, but can
miss dependencies between actions far apart in time or across batteries. Like
genetic search, its preset uses a fixed random seed and a bounded search budget.

## Experimental planner families

Local refinement (`local-margin-*`, `local-preserve-*`, `local-tariff-*`) compares a small set of
complete schedules. The margin is an additional required forecast saving per
kWh of grid charging, in your tariff currency. Preservation variants add a
price-quantile rule for holding energy. Tariff variants also set the price
quantile used to construct a grid-charging candidate. These controls can avoid marginal trades,
but can also reject useful charging opportunities.

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
grid charging during value-policy search. Hybrid presets with `-chargepremiumN`
use the explicit premium `N` in your tariff currency per kWh. The premium can
discourage weak forecast trades but also reject profitable ones. It never adds
to reported electricity cost, and the passive branch, fleet fallback and target
repair retain their existing behavior. Zero disables the heuristic.

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

These families use shared comfort preparation and optional-load reporting.
They are experimental alternatives: compare them with the simulator using your
own exports before choosing one for daily operation.

## Shared features and limits

All presets provide battery schedules, comfort schedules, optional-appliance
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

## Battery behavior

| Profile | Intended behavior |
| --- | --- |
| Aggressive | Favors price opportunities and accepts more battery activity. |
| Balanced | The default balance between savings and calmer battery operation. |
| Conservative | Discourages marginal moves and frequent changes of operating mode. |

These are preferences, not guarantees about cycling or battery life. They do
not change your minimum reserve or replace a charging target. Set the battery's
minimum energy directly to retain more reserve, and use a
[battery target](entities-and-services.md) for a required level by a deadline.
Self-consumption keeps its fixed battery policy regardless of the profile.

The planners interpret these preferences according to their control models, so
the same profile can produce different schedules. Selecting grid charging still
requests the full feasible slot rate; Conservative is not a charging-power limit.
Preference penalties are not added to projected electricity cost or savings.
Use [historical cost tracking](historical-cost-tracking.md) to assess results on
your own installation.
