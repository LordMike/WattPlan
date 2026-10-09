# Self-consumption baseline

Factory: `custom_components.wattplan.optimizer.planners.self_consumption:factory`.
Available in the registry and Home Assistant as `self-consumption`.

Every battery receives `self_consume` for every slot. Shared physics applies
capacity, minimum reserve, ingress permissions, efficiencies, SoC-dependent power
limits and PV allocation. No grid-charge action is introduced, including during
negative prices, reserve shortfalls or target deadlines. This preserves a useful
physical baseline for benchmarks.

Targets are **observed**, not optimized. Unmet deadlines and end-of-slot reserve
shortfalls appear in the common diagnostics and degrade HA plan status. A battery
starting below reserve remains below it until permitted PV charging recovers the
energy; reported energy is never clamped upward. A recovered later slot does not
erase an earlier shortfall from the plan's diagnostics.

The shared live wrapper schedules configured comfort loads with rolling-window
history, minimum on/off commitments and maximum off duration, then incorporates
their demand into the battery replay. Aligned replan state preserves active
comfort commitments. Optional appliances receive fixed-policy incremental-cost
recommendations without being added to the executed plan. These shared features
do not turn baseline batteries into a target or tariff optimizer.

The result supplies per-slot and total tariff costs, comparison costs, savings,
energy flows, battery levels, comfort schedules, status and whole-call timings.
There is one counted battery replay, plus separately reported shared comfort
placement and optional ranking work. Battery behavior preferences are reported
as unused by baseline policy selection, and no solver counters are fabricated.
