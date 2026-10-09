# WattPlan Planner Presets and Battery Behavior

General settings offers a **Planner preset** and a separate **Battery behavior** setting.

## Planner presets

Each preset selects an algorithm with fixed search settings. Existing setups and new
setups default to **HiGHS — existing settings**, retaining the previous lookahead
and battery behavior. Selecting another preset reloads the integration and rejects
cached plans computed from the previous configuration.

| Preset IDs | Search configuration |
| --- | --- |
| `highs` | Existing HiGHS settings; configurable lookahead in hours |
| `highs-8`, `highs-16`, `highs-24` | HiGHS with 8, 16 or 24 lookahead **slots** |
| `self-consumption` | Baseline battery self-consumption; observes targets without pursuing them |
| `greedy-8`, `greedy-256` | Greedy search with 8 or 256 evaluations |
| `local-search-3`, `local-search-96` | Local search with 3 or 96 evaluations |
| `beam-2`, `beam-16` | Beam search retaining 2 or 16 alternatives |
| `dynamic-programming-fast`, `dynamic-programming-strong` | Bounded dynamic programming with 8 or 32 retained states |
| `charge-window-96` | Charge-window search with 96 evaluations |
| `genetic-fast`, `genetic-standard` | Genetic search with 16 or 48 evaluations |
| `cem-48` | Cross-entropy search with 48 evaluations |

At 15-minute resolution, `highs-16` considers four hours and `highs-24` six
hours. At 60-minute resolution they consider 16 and 24 hours. The complete
configured planning horizon is still returned. The editable HiGHS lookahead
field applies only to the `highs` existing-settings preset. Other algorithms
have their own fixed guidance horizons and work budgets; increasing the
HiGHS field does not tune them. Presets trade computation against search
quality and do not guarantee the cheapest result for every forecast.

The catalog is defined in `optimizer/planners/presets.py`. Diagnostics and
recorded planner reproductions include the preset, underlying algorithm and
fixed options so comparisons can reproduce the selected configuration.

All presets publish comfort schedules, advisory appliance options, projections
and observed constraint problems. Self-consumption retains passive battery
behavior; it reports battery target and reserve misses without planning grid
charging to correct them. Other planners prioritize those requirements within
their search limits. Discrete policy search can miss an exact target that the
HiGHS continuous model reaches; an unmet target is reported as such.

## Battery behavior

These profiles are integration presets. Internally, the optimizer operates on numeric controls such as throughput penalty, action deadband, and mode-switch penalty. The compatible optimizer API retains the historical field names `throughput_cost_per_kwh` and `mode_switch_cost`, but their values are heuristic objective weights, not monetary estimates of battery degradation or switching wear. They influence which plan the optimizer prefers; they are not added to WattPlan's tariff-only projected cost or savings fields. The integration translates the selected profile into those numeric values before calling the optimizer. The action deadband does not throttle grid charging: once selected, `grid_charge` requests the full feasible slot rate.

The policy-search planners use the deadband to classify charge/idle/discharge
for switching preferences; they do not suppress small physical flows. HiGHS
also uses it in its continuous control constraints. The same behavior profile
therefore expresses similar preferences without promising identical schedules.
Self-consumption keeps its fixed battery policy regardless of these preferences.

## When to use each profile

### Aggressive

Use this when savings are the main goal and you are comfortable with the battery moving more often.

Typical behavior:
- Takes more charging and discharging opportunities when they look economically useful
- Is more willing to make smaller battery moves
- Can produce more active battery schedules

This is usually a good fit when:
- You want WattPlan to chase price differences more actively
- Battery wear is a lower concern than short-term economics
- You prefer the battery to work harder when there is value in doing so

### Balanced

This is the default and should fit most homes.

Typical behavior:
- Still pursues useful savings opportunities
- Avoids some of the smaller or twitchier battery moves
- Keeps behavior calmer without making the battery overly passive

This is usually a good fit when:
- You want a practical middle ground
- You care about savings and battery wear
- You want stable behavior without giving up the main value of planning

### Conservative

Use this when you want the battery to behave more steadily and avoid marginal moves.

Typical behavior:
- Ignores more small or borderline battery actions
- Produces calmer plans with less switching
- Gives up some savings in exchange for less battery activity

This is usually a good fit when:
- Battery wear matters more than small extra savings
- You dislike frequent small charge/discharge changes
- You want simpler, quieter battery behavior

## What profiles do not do

Profiles do not raise the configured battery minimum.

If you want more reserve left in a battery, set that battery's minimum energy directly in the battery configuration. Profiles only control how willing WattPlan is to move battery energy around.

Profiles also do not replace battery targets. If you need a battery, such as an EV, to reach a specific level by a specific time, use a target. A common Home Assistant setup is an automation that sets a weekday morning target and adjusts it for holidays or other patterns.

Projected cost and savings remain estimates of grid import cost minus export revenue under the configured tariffs. They do not claim savings after battery degradation, switching wear, or other ownership costs.
