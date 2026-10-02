# Historical Cost Tracking

Historical cost tracking helps answer whether WattPlan is actually improving cost over time. It compares the measured cost of what happened in your home with simple reference scenarios calculated from the same completed energy slots.

Enable it after the main WattPlan setup is working and your automations are applying WattPlan's actions. Historical tracking is disabled by default.

## Setup Requirements

Historical tracking needs cumulative `kWh` meter sensors. These are different from the here-and-now forecast or power sensors used for planning:

| Sensor type | Used for | Example shape |
| --- | --- | --- |
| Planning sources | Future price, usage, and PV values for the optimizer. These can be forecast attributes, services, templates, or generated forecasts. | "What will the price/load/PV be for each future slot?" |
| Historical meters | Past measured energy totals. WattPlan reads the difference between two completed slots. | "The grid import meter has increased from 100.0 kWh to 101.2 kWh." |

Required historical meters:

| Meter | Required | Purpose |
| --- | --- | --- |
| Grid import | Yes | Measures how much energy was bought from the grid. |
| Usage/load | Yes | Measures total household consumption for the reference scenarios. |
| Grid export | No | Measures exported energy. If not configured, export is treated as zero. |
| PV production | No | Measures solar production for reference scenarios. If not configured, PV is treated as zero. |

Use sensors with a steadily increasing `kWh` total, usually with Home Assistant device class `energy` and state class `total` or `total_increasing`. Do not use instant `kW` power sensors, current battery level sensors, or forecast-only sensors as historical meters.

Historical tracking also needs prices for each completed slot. WattPlan keeps the normalized import/export prices from successful planner runs and falls back to live price source reads when needed. When the export price source was unavailable during a planner run, the zeros the planner substitutes are not kept as prices; the slot then uses a live export price read, or is marked as missing an export price, rather than booking zero export revenue.

## How The Numbers Update

Historical cost sensors are period-to-date totals, not last-slot snapshots.

| Period | Meaning |
| --- | --- |
| `today` | Accumulated from local midnight through the latest completed slot. |
| `this_month` | Accumulated from the first day of the local month through the latest completed slot. |

At a period rollover (local midnight, or the first of the month) the new period reads `0.0` until its first slot completes; the sensors do not go unavailable. A period is unavailable only when tracking began after it ended or when every slot it contains is missing a required input.

WattPlan only processes completed slots. If the setup uses 15-minute slots, the values update after a full 15-minute interval has finished. Missing inputs, meter resets, missing prices, and skipped slots are not spread across multiple prices. Each sensor counts a slot as missing only when an input required for that metric is unavailable: actual cost needs grid import/export and both applicable prices; grid-only cost needs usage and import price; simple self-consumption needs usage, PV, simulated grid flows, both prices, and trusted reference continuity. Savings use only slots where both actual and the selected reference cost are valid.

When historical tracking first reads the cumulative meters partway through a slot, that reading becomes the baseline for the current slot. At the next boundary, WattPlan records only the increase observed since that baseline in the current slot; it does not extrapolate energy for the unobserved beginning of the slot. Later slots use the preceding boundary reading normally. If one or more boundaries are missed instead, WattPlan keeps the existing gap behavior: it marks the affected slots as missing and reseeds the meter baseline without distributing accumulated energy across them.

Meter resets and unavailable readings cost as few slots as possible:

| Situation | What is recorded |
| --- | --- |
| A cumulative counter resets (the reading drops) | Only the reset slot is missing. The new reading becomes the baseline, so the next slot is a normal delta. |
| A meter is unavailable or not numeric for one slot | That slot is missing. The last good reading stays the baseline, and the next slot books the increase since it, which covers two slots of energy. That slot is flagged (`multi_slot_delta`) and priced at its own slot price. |
| A meter is unavailable for two or more slots in a row | Every affected slot, including the first one after the meter returns, is missing. The energy cannot be attributed to a single slot. |

## Scenarios

| Scenario | What it means | How to read it |
| --- | --- | --- |
| Grid only | A reference where the same measured household load is supplied entirely by the grid. It does not model PV, batteries, export credit, or behavior changes. | Useful as the broadest baseline: what the same consumption would have cost without local production or storage. |
| Simple self-consumption | A reference where PV serves usage first, PV surplus charges configured batteries, and batteries discharge before grid import. It has no grid charging, no price awareness, and no preserve behavior. | Usually the first comparison for battery setups because it represents a simple PV-first battery strategy without WattPlan scheduling. |
| Actual | What really happened after all planning, automation, manual control, or lack of control. | Grid import cost minus grid export value. Lower is better when comparing raw cost sensors. |

The reference scenarios are not predictions. They are recalculated from the same measured usage and PV facts that occurred in the completed slots.

The simple self-consumption simulation keeps its own simulated SoC within each
comparison segment. It does not re-sync every slot, because doing so would mix
actual WattPlan-controlled behavior into the counterfactual baseline.

Missing usage/PV, counter resets, skipped boundaries, or invalid simulation state
invalidate reference continuity. The affected reference costs are unavailable.
Once finite meter baselines and real battery SoC are available, tracking starts a
new explicitly identified segment; it does not reconstruct the missing history.
Sensor attributes expose continuity validity, the segment start/reason, and the
segment IDs represented in period totals. Totals across segments describe the
observed comparison segments, not one uninterrupted counterfactual. Missing
tariffs alone do not interrupt the energy simulation when usage and PV are known.

When older stored history has no continuity metadata, reference values from its
first retained missing usage/PV interval onward are conservatively marked
unavailable. Actual meter facts remain retained. Historical reference totals may
therefore change after upgrading rather than continue displaying unverified gains
or losses.

## Entities

Enabled by default when historical tracking is enabled:

| Entity | Meaning |
| --- | --- |
| `sensor.<setup_slug>_historical_actual_cost_today` | Actual measured net cost for today so far. |
| `sensor.<setup_slug>_historical_grid_only_cost_today` | Grid-only reference cost for today so far. |
| `sensor.<setup_slug>_historical_self_consumption_cost_today` | Simple self-consumption reference cost for today so far. |
| `sensor.<setup_slug>_historical_savings_vs_grid_only_today` | Grid-only reference cost minus actual cost for today so far. Positive means actual behavior is beating the grid-only model. |
| `sensor.<setup_slug>_historical_savings_vs_self_consumption_today` | Simple self-consumption reference cost minus actual cost for today so far. Positive means actual behavior is beating simple self-consumption. |

Disabled by default:

| Entity | Meaning |
| --- | --- |
| `sensor.<setup_slug>_historical_actual_cost_this_month` | Actual measured net cost for this month so far. |
| `sensor.<setup_slug>_historical_grid_only_cost_this_month` | Grid-only reference cost for this month so far. |
| `sensor.<setup_slug>_historical_self_consumption_cost_this_month` | Simple self-consumption reference cost for this month so far. |
| `sensor.<setup_slug>_historical_savings_vs_grid_only_this_month` | Grid-only reference cost minus actual cost for this month so far. Positive means actual behavior is beating the grid-only model. |
| `sensor.<setup_slug>_historical_savings_vs_self_consumption_this_month` | Simple self-consumption reference cost minus actual cost for this month so far. Positive means actual behavior is beating simple self-consumption. |

The simple self-consumption cost and savings entities also expose the current simulated battery SoC as attributes in kWh and percent. These values are the simulation's internal state, not the live battery SoC.

## Reading Savings

Savings sensors use this formula:

```text
savings = reference cost - actual cost
```

That means:

| Savings value | Meaning |
| --- | --- |
| Positive | Good for that comparison. Actual measured behavior cost less than the reference scenario. |
| Zero | Actual measured behavior cost the same as the reference scenario. |
| Negative | Actual measured behavior cost more than the reference scenario for the period so far. |

For example, if `sensor.<setup_slug>_historical_savings_vs_grid_only_today` is `2.50`, the real setup is currently `2.50` cheaper than supplying the same household load entirely from the grid today. If it is `-2.50`, the real setup is currently `2.50` more expensive than the grid-only model today.

Daily values can be noisy, especially early in the day when a battery may charge before later savings happen. Monthly sensors are usually better for judging whether WattPlan is helping over time.
