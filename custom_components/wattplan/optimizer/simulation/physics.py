"""Compatibility imports; shared physics now lives in optimizer.physics."""

from ..physics import (  # noqa: F401
    EPSILON,
    MIN_GRID_CHARGE_SLOT_FRACTION,
    POLICIES,
    Battery,
    Transition,
    battery_power_limits,
    grid_charge_headroom_sufficient,
    transition,
)
