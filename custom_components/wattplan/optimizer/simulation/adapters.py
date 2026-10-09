"""Compatibility factory paths for existing simulator configurations."""

from ..planners.highs import factory as highs_factory
from ..planners.self_consumption import factory as self_consumption_factory

DEFAULT_PLANNER = "highs"
BASELINE_PLANNER = "self-consumption"

__all__ = [
    "DEFAULT_PLANNER",
    "BASELINE_PLANNER",
    "highs_factory",
    "self_consumption_factory",
]
