# Shared planner reporting

All policy planners use `planners/reporting.py` to translate their accepted
physical trajectory into the Home Assistant result contract. Costs, per-slot
savings, import/export, battery levels, and grid charging describe that same
accepted plan. The baseline is the forecast usage and PV without controlled
battery or comfort loads; the comfort finalizer restores this baseline after
planning with comfort consumption included.

`avg_price` is net tariff cost divided by imported energy, retaining the existing
1000 sentinel when there is no import. `fitness` describes the policy evaluator's
tariff plus reserve-recovery, throughput and mode-switch preferences; it is not comparable to the
HiGHS internal fitness. Neither preference penalties nor optional appliances are
added to projected electricity cost. The planner dispatcher measures complete
execution time; shared reporting does not invent solver counters.

`battery_min_unmet` means an accepted end-of-slot level remains below reserve.
An initially low battery that recovers within the first slot does not generate
this reason. `battery_target_unmet` reports an actual deadline miss outside its
tolerance. These reasons set the established `suboptimal` and `overconstrained`
fields so HA can display a degraded plan. They describe the accepted plan and
do not claim that every possible schedule would violate the constraint.

Optional appliance recommendations preserve every published battery policy and
compare the full-horizon tariff cost with and without the proposed appliance.
This accounts for battery energy used now that must be replaced later. Each
candidate resumes from the accepted state at its start, then reuses the suffix
only after battery levels exactly reconverge following the appliance's end.
Candidate costs are sorted by cost then start time; configured overlap and
minimum-gap rules match the existing HiGHS recommendations. Appliances are
ranked individually, not as a joint schedule.
