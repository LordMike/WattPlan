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

Historical tracking also needs prices for each completed slot. WattPlan keeps the normalized import/export prices from successful planner runs and falls back to live price source reads when needed.

## How The Numbers Update

Historical cost sensors are period-to-date totals, not last-slot snapshots.

| Period | Meaning |
| --- | --- |
| `today` | Accumulated from local midnight through the latest completed slot. |
| `this_month` | Accumulated from the first day of the local month through the latest completed slot. |

WattPlan only processes completed slots. If the setup uses 15-minute slots, the values update after a full 15-minute interval has finished. Missing meters, meter resets, missing prices, and skipped slots are counted as missing slots instead of being spread across multiple prices.

## Scenarios

| Scenario | What it means | How to read it |
| --- | --- | --- |
| Grid only | A reference where the same measured household load is supplied entirely by the grid. It does not model PV, batteries, export credit, or behavior changes. | Useful as the broadest baseline: what the same consumption would have cost without local production or storage. |
| Simple self-consumption | A reference where PV serves usage first, PV surplus charges configured batteries, and batteries discharge before grid import. It has no grid charging, no price awareness, and no preserve behavior. | Usually the first comparison for battery setups because it represents a simple PV-first battery strategy without WattPlan scheduling. |
| Actual | What really happened after all planning, automation, manual control, or lack of control. | Grid import cost minus grid export value. Lower is better when comparing raw cost sensors. |

The reference scenarios are not predictions. They are recalculated from the same measured usage and PV facts that occurred in the completed slots.

The simple self-consumption simulation is seeded once from the real battery SoC and then keeps its own simulated SoC. It does not re-sync every slot, because doing so would mix actual WattPlan-controlled behavior into the counterfactual baseline.

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
