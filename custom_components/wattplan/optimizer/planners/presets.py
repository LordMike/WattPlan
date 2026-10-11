"""Named, reproducible planner configurations shared by HA and benchmarks.

The legacy ``highs`` preset leaves request tuning unchanged. Numeric HiGHS
suffixes are lookahead *slots*, not hours. Other numeric suffixes name the
algorithm's search budget or beam width, as described by their labels.
"""

from dataclasses import dataclass, field
from typing import Any

from ..models import OptimizationParams


@dataclass(frozen=True)
class PlannerPreset:
    """One algorithm and its fixed search settings."""

    planner: str
    label: str
    options: dict[str, Any] = field(default_factory=dict)


DEFAULT_PRESET = "highs"
PRESETS = {
    "highs": PlannerPreset("highs", "HiGHS — existing settings"),
    **{
        f"highs-{slots}": PlannerPreset(
            "highs", f"HiGHS — {slots} lookahead slots", {"lookahead_slots": slots}
        )
        for slots in (8, 16, 24)
    },
    "self-consumption": PlannerPreset(
        "self-consumption", "Self-consumption — baseline (observes targets)"
    ),
    **{
        f"greedy-{count}": PlannerPreset(
            "greedy", f"Greedy — {count} evaluations", {"max_evaluations": count}
        )
        for count in (8, 256)
    },
    **{
        f"local-search-{count}": PlannerPreset(
            "local-search", f"Local search — {count} evaluations",
            {"max_evaluations": count, "seed": 0},
        )
        for count in (3, 96)
    },
    **{
        f"beam-{width}": PlannerPreset(
            "beam-search", f"Beam search — width {width}",
            {"beam_width": width, "guidance_slots": 24, "guidance_scale": 1,
             "max_expansions": 30000, "max_joint_actions": 81},
        )
        for width in (1, 2, 8, 16)
    },
    "dynamic-programming-fast": PlannerPreset(
        "dynamic-programming", "Dynamic programming — fast",
        {"soc_step_kwh": 0.1, "max_states": 8, "max_expansions": 6000,
         "max_joint_actions": 81, "value_horizon_slots": 24},
    ),
    "dynamic-programming-strong": PlannerPreset(
        "dynamic-programming", "Dynamic programming — strong",
        {"soc_step_kwh": 0.25, "max_states": 32, "max_expansions": 20000,
         "max_joint_actions": 81, "value_horizon_slots": 144},
    ),
    "charge-window-96": PlannerPreset(
        "charge-window", "Charge window — 96 evaluations",
        {"max_evaluations": 96, "window_candidates": 3},
    ),
    **{
        f"genetic-{name}": PlannerPreset(
            "genetic", f"Genetic — {name}",
            {"max_evaluations": evaluations, "population_size": population,
             "generations": 8, "elite_count": 2, "tournament_size": 3,
             "mutation_rate": 0.9, "crossover_rate": 0.8,
             "guidance_slots": guidance, "seed": 0, "max_seconds": None},
        )
        for name, evaluations, population, guidance in (
            ("fast", 16, 6, 24), ("standard", 48, 8, 144)
        )
    },
    "cem-48": PlannerPreset(
        "cross-entropy", "Cross-entropy — 48 evaluations",
        {"max_evaluations": 48, "population_size": 12, "elite_fraction": 0.25,
         "smoothing": 0.7, "probability_floor": 0.02, "block_slots": 4,
         "guidance": 0.85, "seed": 0},
    ),
    **{
        f"local-margin-{margin:g}": PlannerPreset(
            "local-refinement", f"Local refinement — grid margin {margin:g}/kWh",
            {"grid_margin_per_kwh": margin},
        )
        for margin in (0.1, 0.3, 0.6)
    },
    **{
        f"local-preserve-{fraction}": PlannerPreset(
            "local-refinement", f"Local refinement — preservation quantile {fraction:g}",
            {"preserve_fraction": fraction},
        )
        for fraction in (0.0, 0.15, 0.35, 0.5, 0.65, 0.8, 0.95)
    },
    **{
        f"local-tariff-{fraction}-preserve-0.35": PlannerPreset(
            "local-refinement", f"Local refinement — tariff quantile {fraction}, preservation 0.35",
            {"tariff_fraction": fraction, "preserve_fraction": 0.35},
        )
        for fraction in (0.05, 0.1)
    },
    **{
        f"price-reserve-c{charge}-d{discharge}-m{margin}-p{pv}-r12-f{fill}": PlannerPreset(
            "price-reserve", f"Price reserve — charge {charge}%, discharge {discharge}%, margin {margin / 100:g}",
            {"charge_quantile": charge / 100, "discharge_quantile": discharge / 100,
             "margin": margin / 100, "pv_reserve": pv / 100,
             "reserve_slots": 12, "max_fill": fill / 100},
        )
        for charge, discharge, margin, pv, fill in (
            (5, 15, 10, 50, 100), (15, 35, 10, 50, 100),
            (25, 50, 10, 50, 100), (15, 35, 30, 100, 100),
            (15, 15, 10, 50, 75), (15, 0, 10, 50, 100),
            (0, 35, 0, 0, 100), (5, 65, 10, 50, 100),
            (15, 80, 10, 50, 100), (25, 65, 0, 0, 100),
            (5, 80, 10, 100, 75),
        )
    },
    **{
        f"value-grid{grid}-pv{pv}-terminal0": PlannerPreset(
            "value-policy", f"Value policy — {grid} energy intervals, PV {pv}%",
            {"energy_intervals": grid, "pv_scale": pv / 100,
             "terminal_value_fraction": 0.0},
        )
        for grid, pv in ((4, 100), (8, 100), (4, 75), (4, 50), (2, 100), (2, 50), (6, 100))
    },
    **{
        f"price-reserve-c15-d{discharge}-m10-p50-r{slots}-f100-demand1-fm{margin}-grid0": PlannerPreset(
            "price-reserve", f"Price reserve — demand reserve, {slots} slots, spread {margin / 100:g}",
            {"charge_quantile": 0.15, "discharge_quantile": discharge / 100,
             "margin": 0.1, "pv_reserve": 0.5, "reserve_slots": slots,
             "max_fill": 1.0, "demand_reserve": True,
             "future_price_margin": margin / 100, "grid_enabled": False},
        )
        for discharge, slots, margin in ((65, 12, 0), (65, 24, 20), (100, 24, 0), (100, 12, 20))
    },
    **{
        f"value-grid4-pv100-terminal{credit}": PlannerPreset(
            "value-policy", f"Value policy — 4 energy intervals, terminal value {credit}%",
            {"energy_intervals": 4, "pv_scale": 1.0,
             "terminal_value_fraction": credit / 100},
        )
        for credit in (25, 50)
    },
    **{
        f"value-grid{grid}-pv100-terminal0-chargeknots1": PlannerPreset(
            "value-policy", f"Value policy — {grid} energy intervals plus charging thresholds",
            {"energy_intervals": grid, "pv_scale": 1.0,
             "terminal_value_fraction": 0.0, "include_charge_knots": True},
        )
        for grid in (2, 4)
    },
    **{
        f"value-grid4-pv100-demand{demand}-terminal50": PlannerPreset(
            "value-policy", f"Value policy — 4 energy intervals, demand {demand}%, terminal value 50%",
            {"energy_intervals": 4, "pv_scale": 1.0,
             "demand_scale": demand / 100, "terminal_value_fraction": 0.5},
        )
        for demand in (75, 125)
    },
    **{
        f"value-grid4-pv100-terminal50-scenario{spread}": PlannerPreset(
            "value-policy", f"Value policy — 4 intervals, terminal value 50%, PV spread {spread}%",
            {"energy_intervals": 4, "pv_scale": 1.0,
             "terminal_value_fraction": 0.5, "pv_scenario_spread": spread / 100},
        )
        for spread in (50, 100)
    },
    **{
        f"linear-dispatch-h{horizon}-round{rounding}-pv{pv}-tv{credit}": PlannerPreset(
            "linear-dispatch", f"Linear dispatch — {horizon} slots, rounding {rounding}%",
            {"max_slots": horizon, "round_fraction": rounding / 100,
             "pv_scale": pv / 100, "terminal_value_fraction": credit / 100},
        )
        for horizon, rounding, pv, credit in ((24, 50, 100, 0), (36, 25, 50, 25))
    },
    **{
        f"value-solar-gate-{ratio:g}": PlannerPreset(
            "hybrid-policy", f"Value policy — solar surplus gate {ratio:g}",
            {"surplus_ratio": ratio, "energy_intervals": 4},
        )
        for ratio in (1.0, 1.5, 2.0)
    },
    **{
        f"value-solar-gate-{ratio:g}-grid{grid}": PlannerPreset(
            "hybrid-policy", f"Value policy — solar surplus gate {ratio:g}, {grid} energy intervals",
            {"surplus_ratio": ratio, "energy_intervals": grid},
        )
        for ratio, grid in ((1.0, 6), (1.5, 6), (1.5, 8))
    },
    **{
        f"linear-dispatch-marginal-h{horizon}-pv100-tv0": PlannerPreset(
            "linear-dispatch", f"Linear dispatch — marginal value, {horizon} slots",
            {"dispatch_mode": "marginal", "max_slots": horizon,
             "pv_scale": 1.0, "terminal_value_fraction": 0.0},
        )
        for horizon in (24, 36)
    },
    **{
        f"value-solar-gate-1.5-grid6-terminal{credit}": PlannerPreset(
            "hybrid-policy", f"Value policy — solar surplus gate 1.5, 6 intervals, terminal value {credit}%",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "terminal_value_fraction": credit / 100},
        )
        for credit in (25, 50)
    },
    **{
        f"value-solar-gate-1.5-grid6-terminal0-scenario{spread}": PlannerPreset(
            "hybrid-policy", f"Value policy — solar surplus gate 1.5, 6 intervals, PV spread {spread}%",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "terminal_value_fraction": 0.0, "pv_scenario_spread": spread / 100},
        )
        for spread in (50, 100)
    },
    "value-solar-gate-1.5-grid8-terminal0-scenario100": PlannerPreset(
        "hybrid-policy", "Value policy — solar surplus gate 1.5, 8 intervals, PV spread 100%",
        {"surplus_ratio": 1.5, "energy_intervals": 8,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario0-prerefill1": PlannerPreset(
        "hybrid-policy", "Value policy — solar surplus gate 1.5, 6 intervals, pre-refill guard",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 0.0,
         "prerefill_guard": True},
    ),
    **{
        f"value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium{premium:g}": PlannerPreset(
            "hybrid-policy", f"Value policy — solar gate 1.5, PV spread 100%, grid charge premium {premium:g}/kWh",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
             "grid_charge_premium_per_kwh": premium},
        )
        for premium in (0.01, 0.03, 0.1)
    },
    **{
        f"adaptive-solar-gate1.5-grid6-switch{ratio:g}-low100-high{spread}": PlannerPreset(
            "adaptive-solar-policy", f"Adaptive solar policy — switch ratio {ratio:g}, PV spread 100%/{spread}%",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "spread_switch_ratio": ratio, "low_surplus_spread": 1.0,
             "high_surplus_spread": spread / 100},
        )
        for ratio, spread in ((0.5, 0), (1.0, 0), (0.5, 50), (1.0, 50))
    },
    **{
        f"adaptive-solar-gate1.5-grid6-switch0.5-low100-high{spread}-premiumswitch0.01-low0-high0.03": PlannerPreset(
            "adaptive-solar-policy", f"Adaptive solar policy — PV spread 100%/{spread}%, grid premium 0/0.03 at ratio 0.01",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "spread_switch_ratio": 0.5, "low_surplus_spread": 1.0,
             "high_surplus_spread": spread / 100,
             "low_surplus_grid_charge_premium_per_kwh": 0.0,
             "high_surplus_grid_charge_premium_per_kwh": 0.03,
             "premium_switch_ratio": 0.01},
        )
        for spread in (100, 50)
    },
    **{
        ("value-solar-gate-1.5-grid6-terminal0-scenario100"
         + (f"-chargepremium{premium:g}" if premium else "")
         + "-chargeknots1"): PlannerPreset(
            "hybrid-policy", f"Value policy — solar gate 1.5, PV spread 100%, charging knots, grid premium {premium:g}/kWh",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
             "grid_charge_premium_per_kwh": premium, "include_charge_knots": True},
        )
        for premium in (0.0, 0.03)
    },
    **{
        ("value-solar-gate-1.5-grid6-terminal0-scenario100"
         + (f"-chargepremium{premium:g}" if premium else "")
         + "-rollout2"): PlannerPreset(
            "hybrid-policy", f"Value policy — solar gate 1.5, PV spread 100%, two-slot first-action rollout, grid premium {premium:g}/kWh",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
             "grid_charge_premium_per_kwh": premium, "first_action_rollout": True},
        )
        for premium in (0.0, 0.03, 0.1)
    },
    **{
        f"value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h{label}": PlannerPreset(
            "hybrid-policy", f"Value policy — solar gate 1.5, PV spread 100%, paired demand spread 25%, demand horizon {label}",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
             "grid_charge_premium_per_kwh": 0.03,
             "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": slots},
        )
        for label, slots in (("12", 12), ("full", None))
    },
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-rollout2": PlannerPreset(
        "hybrid-policy", "Value policy — paired demand spread 25% for 12 slots, two-slot first-action rollout",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "first_action_rollout": True},
    ),
    **{
        f"value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand{spread}-h{slots}": PlannerPreset(
            "hybrid-policy", f"Value policy — paired demand spread {spread}% for {slots} slots",
            {"surplus_ratio": 1.5, "energy_intervals": 6,
             "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
             "grid_charge_premium_per_kwh": 0.03,
             "paired_demand_scenario_spread": spread / 100, "paired_demand_scenario_slots": slots},
        )
        for spread, slots in ((10, 12), (50, 12), (25, 6), (25, 24))
    },
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-rollout2-pvpersist2": PlannerPreset(
        "hybrid-policy", "Value policy — two-slot first-action rollout with persistent PV uncertainty",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "first_action_rollout": True, "rollout_pv_persistence": True},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1": PlannerPreset(
        "hybrid-policy", "Value policy — paired demand spread 25% for 12 slots, exact initial energy knot",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True},
    ),
    "value-solar-gate-1.5-grid8-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1": PlannerPreset(
        "hybrid-policy", "Value policy — 8 intervals, paired demand spread 25% for 12 slots, exact initial energy knot",
        {"surplus_ratio": 1.5, "energy_intervals": 8,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-pvtailq1of3": PlannerPreset(
        "hybrid-policy", "Value policy — exact initial knot, pre-refill guard, mean-preserving asymmetric PV tail",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True, "prerefill_guard": True,
         "pv_tail_low_probability": 1 / 3},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-preserveprefix24h": PlannerPreset(
        "hybrid-policy", "Value policy — exact initial knot, pre-refill guard, current-first preserve prefix",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True, "prerefill_guard": True,
         "first_preserve_prefix": True},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-nominalmix50-h6": PlannerPreset(
        "hybrid-policy", "Value policy — exact initial knot, pre-refill guard, nominal joint mixture for 6 slots",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True, "prerefill_guard": True,
         "include_nominal_joint_scenario": True, "nominal_joint_scenario_slots": 6},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1-dischargebudgetknot1": PlannerPreset(
        "hybrid-policy", "Value policy — exact initial knot, pre-refill guard, per-slot discharge budget knot",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True, "prerefill_guard": True,
         "include_discharge_budget_knot": True},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.05-demand25-h12-initialknot1-prerefill1": PlannerPreset(
        "hybrid-policy", "Value policy — grid-charge premium 0.05, paired demand spread 25% for 12 slots, exact initial knot, pre-refill guard",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.05,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True, "prerefill_guard": True},
    ),
    "value-solar-gate-1.5-grid6-terminal0-scenario100-chargepremium0.03-demand25-h12-initialknot1-prerefill1": PlannerPreset(
        "hybrid-policy", "Value policy — paired demand spread 25% for 12 slots, exact initial energy knot, pre-refill guard",
        {"surplus_ratio": 1.5, "energy_intervals": 6,
         "terminal_value_fraction": 0.0, "pv_scenario_spread": 1.0,
         "grid_charge_premium_per_kwh": 0.03,
         "paired_demand_scenario_spread": 0.25, "paired_demand_scenario_slots": 12,
         "include_initial_knot": True, "prerefill_guard": True},
    ),
}


def get_preset(name: str = DEFAULT_PRESET) -> PlannerPreset:
    """Reject unknown persisted values instead of silently changing algorithms."""
    try:
        return PRESETS[name]
    except (KeyError, TypeError) as err:
        raise ValueError(f"Unknown planner preset: {name!r}") from err


def lookahead_slots(name: str, legacy_slots: int, plan_slots: int) -> int:
    """Return the effective solve horizon for comfort input validation."""
    preset = get_preset(name)
    if preset.planner == "highs":
        return min(plan_slots, preset.options.get("lookahead_slots", legacy_slots))
    return plan_slots


def run_preset(params: OptimizationParams, name: str = DEFAULT_PRESET) -> dict:
    """Instantiate inside the caller's executor, keeping imports off HA's loop."""
    from .registry import create_planner

    preset = get_preset(name)
    result = create_planner(preset.planner, dict(preset.options))(params)
    result.setdefault("diagnostics", {}).setdefault("planner", {}).update(
        {"preset": name, "options": dict(preset.options)}
    )
    return result
