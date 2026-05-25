"""Runtime state for WattPlan testbed entities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import StateType
from homeassistant.util import slugify

from .const import (
    CONF_DEFAULT_SOC_PCT,
    CONF_INITIAL_TOTAL_KWH,
    CONF_SEED,
    CONF_SLOT_MINUTES,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    SUBENTRY_BATTERY,
    SUBENTRY_LOAD,
    SUBENTRY_PRICE,
    SUBENTRY_PV,
)
from .generators import (
    GenerationContext,
    context_from_config,
    floor_to_slot,
    load_energy_points,
    load_power_points,
    power_points_to_wh_hours,
    pv_power_points,
    price_points,
)


class TestbedRuntime:
    """Mutable runtime state shared by one testbed config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize runtime state."""
        self.hass = hass
        self.entry = entry
        self._entities: list[Any] = []
        self._overrides: dict[str, dict[str, Any]] = {}
        self._battery_state: dict[str, dict[str, Any]] = {}
        self._load_energy_state: dict[str, dict[str, Any]] = {}
        self._unsub: CALLBACK_TYPE | None = None

        for subentry in entry.subentries.values():
            if subentry.subentry_type == SUBENTRY_BATTERY:
                self._battery_state[subentry.subentry_id] = {
                    "soc_pct": float(subentry.data.get(CONF_DEFAULT_SOC_PCT, 50.0)),
                    "soc_available": True,
                    "availability": "on",
                }
            elif subentry.subentry_type == SUBENTRY_LOAD:
                self._load_energy_state[subentry.subentry_id] = {
                    "total_kwh": float(
                        subentry.data.get(CONF_INITIAL_TOTAL_KWH, 1000.0)
                    ),
                    "last_slot_start": None,
                }

    @property
    def entry_slug(self) -> str:
        """Return a stable slug for this testbed entry."""
        return slugify(self.entry.title) or "testbed"

    def device_info(self, *, subentry: Any | None = None) -> DeviceInfo:
        """Return device info for entry or subentry entities."""
        if subentry is None:
            return DeviceInfo(
                identifiers={(DOMAIN, self.entry.entry_id)},
                name=self.entry.title,
            )
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id, subentry.subentry_id)},
            name=f"{self.entry.title}: {subentry.title}",
        )

    @callback
    def register_entity(self, entity: Any) -> None:
        """Register an entity for runtime refreshes."""
        self._entities.append(entity)

    @callback
    def unregister_entity(self, entity: Any) -> None:
        """Unregister an entity."""
        if entity in self._entities:
            self._entities.remove(entity)

    @callback
    def start(self) -> None:
        """Start periodic refreshes."""
        if self._unsub is not None:
            return
        interval_minutes = int(
            self.entry.data.get(
                CONF_UPDATE_INTERVAL_MINUTES,
                self.entry.data.get(CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES),
            )
        )
        self._unsub = async_track_time_interval(
            self.hass, self._handle_refresh, timedelta(minutes=interval_minutes)
        )

    @callback
    def stop(self) -> None:
        """Stop periodic refreshes."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_refresh(self, _now: datetime) -> None:
        self.refresh_entities()

    @callback
    def refresh_entities(self) -> None:
        """Write all registered entity states."""
        for entity in list(self._entities):
            if getattr(entity, "hass", None) is not None:
                entity.async_write_ha_state()

    def config_for(self, subentry: Any) -> dict[str, Any]:
        """Return subentry config plus runtime number overrides."""
        return {
            **dict(self.entry.data),
            **dict(subentry.data),
            **self._overrides.get(subentry.subentry_id, {}),
        }

    def set_override(self, subentry_id: str, field: str, value: float) -> None:
        """Set one live generator override."""
        self._overrides.setdefault(subentry_id, {})[field] = float(value)
        self.refresh_entities()

    def override_value(self, subentry: Any, field: str) -> float:
        """Return current live value for a number control."""
        value = self._overrides.get(subentry.subentry_id, {}).get(
            field, subentry.data[field]
        )
        return float(value)

    def future_points(self, subentry: Any, *, kind: str) -> list[dict[str, Any]]:
        """Return fixed 24-hour future values for one source subentry."""
        config = self.config_for(subentry)
        ctx = context_from_config(config)
        if subentry.subentry_type == SUBENTRY_PRICE:
            return price_points(config, ctx)
        if subentry.subentry_type == SUBENTRY_PV:
            return pv_power_points(config, ctx)
        if subentry.subentry_type == SUBENTRY_LOAD:
            return load_power_points(config, ctx)
        return []

    def forecast_points(self, subentry: Any, *, kind: str) -> list[dict[str, Any]]:
        """Compatibility wrapper for old tests/scripts."""
        return self.future_points(subentry, kind=kind)

    def power_points(self, subentry: Any, *, kind: str) -> list[dict[str, Any]]:
        """Return power points for one source subentry."""
        if subentry.subentry_type != SUBENTRY_PV or kind != "pv":
            return []
        config = self.config_for(subentry)
        return pv_power_points(config, context_from_config(config))

    def initialize_load_energy(
        self, subentry: Any, restored_value: float | None = None
    ) -> None:
        """Initialize a live cumulative load meter."""
        config = self.config_for(subentry)
        slot_minutes = int(
            config.get(
                CONF_UPDATE_INTERVAL_MINUTES,
                config.get(CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES),
            )
        )
        state = self._load_energy_state.setdefault(subentry.subentry_id, {})
        if restored_value is not None:
            state["total_kwh"] = float(restored_value)
        else:
            state.setdefault(
                "total_kwh",
                float(config.get(CONF_INITIAL_TOTAL_KWH, 1000.0)),
            )
        state["last_slot_start"] = floor_to_slot(datetime.now(tz=UTC), slot_minutes)

    def load_energy_state(self, subentry: Any) -> float:
        """Return live cumulative load energy."""
        config = self.config_for(subentry)
        slot_minutes = int(
            config.get(
                CONF_UPDATE_INTERVAL_MINUTES,
                config.get(CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES),
            )
        )
        current_slot = floor_to_slot(datetime.now(tz=UTC), slot_minutes)
        state = self._load_energy_state.setdefault(
            subentry.subentry_id,
            {
                "total_kwh": float(config.get(CONF_INITIAL_TOTAL_KWH, 1000.0)),
                "last_slot_start": current_slot,
            },
        )
        last_slot_start = state.get("last_slot_start")
        if last_slot_start is None:
            state["last_slot_start"] = current_slot
            return round(float(state["total_kwh"]), 6)
        if current_slot <= last_slot_start:
            return round(float(state["total_kwh"]), 6)

        slots = int(
            (current_slot - last_slot_start).total_seconds() // (slot_minutes * 60)
        )
        ctx = GenerationContext(
            start_at=last_slot_start,
            slot_minutes=slot_minutes,
            slots=slots,
            seed=int(config.get(CONF_SEED, 1)),
        )
        state["total_kwh"] = float(state["total_kwh"]) + sum(
            float(point["value"]) for point in load_energy_points(config, ctx)
        )
        state["last_slot_start"] = current_slot
        return round(float(state["total_kwh"]), 6)

    def energy_pv_wh_hours(self) -> dict[str, float]:
        """Return aggregated Energy solar forecast data for this entry."""
        totals: dict[str, float] = {}
        for subentry in self.entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_PV:
                continue
            config = self.config_for(subentry)
            ctx = context_from_config(config)
            for start, wh in power_points_to_wh_hours(
                pv_power_points(config, ctx), slot_minutes=ctx.slot_minutes
            ).items():
                totals[start] = totals.get(start, 0.0) + wh
        return totals

    def battery_soc_state(self, subentry_id: str) -> StateType:
        """Return battery SoC state."""
        state = self._battery_state[subentry_id]
        if not state.get("soc_available", True):
            return None
        return round(float(state["soc_pct"]), 2)

    def battery_availability_state(self, subentry_id: str) -> str:
        """Return battery availability state marker."""
        return str(self._battery_state[subentry_id]["availability"])

    def set_battery_soc(self, subentry_id: str, soc_pct: float) -> None:
        """Set battery SoC and mark it available."""
        state = self._battery_state[subentry_id]
        state["soc_pct"] = max(0.0, min(100.0, float(soc_pct)))
        state["soc_available"] = True
        state["availability"] = "on"
        self.refresh_entities()

    def set_battery_available(self, subentry_id: str) -> None:
        """Set battery availability to on."""
        self._battery_state[subentry_id]["soc_available"] = True
        self._battery_state[subentry_id]["availability"] = "on"
        self.refresh_entities()

    def set_battery_unavailable(self, subentry_id: str) -> None:
        """Set battery availability to off."""
        self._battery_state[subentry_id]["availability"] = "off"
        self.refresh_entities()

    def set_battery_availability_unavailable(self, subentry_id: str) -> None:
        """Make the availability entity unavailable."""
        self._battery_state[subentry_id]["availability"] = "unavailable"
        self.refresh_entities()

    def set_battery_soc_unavailable(self, subentry_id: str) -> None:
        """Make the SoC entity unavailable."""
        self._battery_state[subentry_id]["soc_available"] = False
        self.refresh_entities()


def subentry_name(subentry: Any) -> str:
    """Return display name for a subentry."""
    return str(subentry.data.get(CONF_NAME, subentry.title))


def subentry_slug(subentry: Any) -> str:
    """Return slug for a subentry."""
    return slugify(subentry_name(subentry)) or "asset"
