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
