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
misses alongside cost. The planners do not give leftover energy a separate
end-of-plan credit, so using energy down to the configured reserve can be the
cheapest forecast outcome.

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
