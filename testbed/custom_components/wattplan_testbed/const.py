"""Constants for the WattPlan testbed integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "wattplan_testbed"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
]

CONF_HORIZON_HOURS = "horizon_hours"
CONF_SLOT_MINUTES = "slot_minutes"
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"
CONF_BASE_OFFSET = "base_offset"
CONF_FACTOR = "factor"
CONF_NOISE = "noise"
CONF_PHASE_HOURS = "phase_hours"
CONF_SEED = "seed"
CONF_PEAK_KWH = "peak_kwh"
CONF_CLOUD_FACTOR = "cloud_factor"
CONF_PROFILE = "profile"
CONF_INITIAL_TOTAL_KWH = "initial_total_kwh"
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_DEFAULT_SOC_PCT = "default_soc_pct"

SUBENTRY_BATTERY = "battery"
SUBENTRY_LOAD = "load"
SUBENTRY_PRICE = "price"
SUBENTRY_PV = "pv"

GENERATOR_PRICE = "price_noise_v1"
GENERATOR_PV = "pv_daily_curve_v1"
GENERATOR_LOAD = "load_profile_v1"

DEFAULT_SLOT_MINUTES = 15
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
FUTURE_WINDOW_HOURS = 24
