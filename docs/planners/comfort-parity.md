# Shared comfort scheduling

Every battery planner uses the same comfort scheduling layer. HiGHS and the
shared layer call the same solver-independent constraint-driven constructor;
alternatives never import the HiGHS planner or invoke a native solve.

The layer enforces rolling-window ON targets, maximum OFF streaks, minimum ON
and OFF runs, terminal run completion, and current runtime state. Ordered
observations establish recent history. Missing history retains the original
conservative aggregate fallback and reports `comfort_history_unavailable`.
Impossible early rolling targets and conflicting locks are returned as explicit
comfort violations and degraded plan status; they do not silently disappear.

Bounded price placement tests at most 32 local schedule moves per entity and 128
total, retaining hard constraints. Its cost estimate replays the batteries in
self-consumption mode, including PV and export tariffs. The selected battery
algorithm then optimizes around the accepted comfort consumption. This differs
from HiGHS's optional battery reoptimization during placement: cost quality is
heuristic and may differ, while the configured comfort constraints remain the
same. Diagnostics report the cost model, considered improvement, and violations.

The battery request includes comfort energy exactly once. Public schedules,
base and comfort flow components, optional-load costs, and projections include
those actions. Baseline cost keeps the original HiGHS definition of base demand
minus PV without scheduled comfort or battery controls.

Opaque state retains separate battery payload and comfort trajectories. Saved
locks are reused only for aligned slot times, unchanged comfort configuration,
and matching runtime/history observations. Live observations override predicted
state. Removing comfort unwraps the battery payload; switching planner presets
still obeys the registry's state ownership rules. No-comfort requests preserve
the original economics and avoid placement work.
