"""Runtime tracker for historical cost slots."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import math
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time

from ..const import (
    CONF_CAN_CHARGE_FROM_PV,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_HISTORICAL_GRID_EXPORT_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_PV_SENSOR,
    CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION,
    CONF_HISTORICAL_USAGE_SENSOR,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_MINIMUM_KWH,
    CONF_SOC_SOURCE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCES,
    SOURCE_MODE_NOT_USED,
    SUBENTRY_TYPE_BATTERY,
)
from ..source_pipeline import build_source_value_provider
from ..source_types import SourceProvider, SourceProviderError, SourceWindow
from .models import (
    FLAG_GAP,
    FLAG_METER_RESET,
    FLAG_MISSING_EXPORT_PRICE,
    FLAG_MISSING_IMPORT_PRICE,
    FLAG_MISSING_METER,
    FLAG_SELF_CONSUMPTION_UNAVAILABLE,
    HistoricalMetric,
    SCENARIO_ACTUAL,
    SCENARIO_GRID_ONLY,
    SCENARIO_SELF_CONSUMPTION,
    SlotRecord,
)
from .simulations import (
    BatterySimulationConfig,
    simulate_self_consumption_slot,
)
from .store import HistoricalCostStore, HistoricalPeriodSummary

_LOGGER = logging.getLogger(__name__)
SCHEDULE_OFFSET = timedelta(seconds=5)

type HistoricalUpdateListener = Callable[[], None]


class HistoricalCostTracker:
    """Track completed historical cost slots for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        slot_minutes: int,
    ) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.entry = entry
        self.slot_minutes = int(slot_minutes)
        self.store = HistoricalCostStore(
            hass,
            entry_id=entry.entry_id,
            slot_minutes=slot_minutes,
            currency=hass.config.currency,
        )
        self._interval = timedelta(minutes=self.slot_minutes)
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._source_providers: dict[str, SourceProvider] = {}
        self._listeners: set[HistoricalUpdateListener] = set()
        self._process_lock = asyncio.Lock()

    async def async_start(self) -> None:
        """Load state, seed cursors, and start scheduling."""
        await self.store.async_load()
        if self._meter_config() != self.store.data.get("meter_config"):
            await self._async_seed(
                datetime.now(tz=UTC), simulation_reason="meter_configuration_changed"
            )
            return
        if not self.store.last_meter_values():
            await self._async_seed(
                datetime.now(tz=UTC), simulation_reason="tracking_initialized"
            )
            return
        if self.scenario_enabled(SCENARIO_SELF_CONSUMPTION):
            configuration = self._simulation_configuration()
            continuity = self.store.simulation_continuity()
            if continuity["configuration"] != configuration:
                await self._async_seed(
                    datetime.now(tz=UTC),
                    simulation_reason="battery_configuration_changed",
                )
                return
            expected_ids = {item["subentry_id"] for item in configuration}
            if continuity["valid"] and set(self.store.simulation_soc()) != expected_ids:
                self.store.invalidate_simulation(
                    at=datetime.now(tz=UTC), reason="invalid_persisted_state"
                )
        else:
            self.store.invalidate_simulation(
                at=datetime.now(tz=UTC), reason="scenario_disabled"
            )
        self._schedule_next(datetime.now(tz=UTC))

    async def async_shutdown(self) -> None:
        """Stop scheduling and flush pending store state."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        await self.store.async_flush()

    @callback
    def async_add_listener(self, listener: HistoricalUpdateListener) -> CALLBACK_TYPE:
        """Subscribe to historical data updates."""
        self._listeners.add(listener)

        @callback
        def _remove() -> None:
            self._listeners.discard(listener)

        return _remove

    def summary(
        self,
        *,
        metric: HistoricalMetric,
        period: str,
        scenario: str | None,
    ) -> HistoricalPeriodSummary:
        """Return an aggregate summary for an entity."""
        return self.store.summary(metric=metric, period=period, scenario=scenario)

    def scenario_enabled(self, scenario: str | None) -> bool:
        """Return whether a historical scenario is currently enabled."""
        if scenario is None or scenario == SCENARIO_ACTUAL:
            return True
        if scenario == SCENARIO_GRID_ONLY:
            return True
        if scenario == SCENARIO_SELF_CONSUMPTION:
            return bool(
                self.entry.options.get(
                    CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION,
                    True,
                )
            )
        return False

    def remember_price_series(
        self,
        *,
        start_at: datetime,
        slot_minutes: int,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Retain normalized planner prices for later historical slot processing."""
        self.store.remember_price_series(
            start_at=start_at,
            slot_minutes=slot_minutes,
            import_prices=import_prices,
            export_prices=export_prices,
        )
        self._notify()

    def self_consumption_simulation_attributes(self) -> dict[str, Any]:
        """Return current self-consumption simulation state attributes."""
        soc_kwh = self.store.simulation_soc()
        continuity = self.store.simulation_continuity()
        battery_configs = {
            battery.subentry_id: battery for battery in (self._battery_configs() or [])
        }
        soc_percent: dict[str, float] = {}
        for subentry_id, soc in soc_kwh.items():
            battery = battery_configs.get(subentry_id)
            if battery is None or battery.capacity_kwh <= 0.0:
                continue
            soc_percent[subentry_id] = round((soc / battery.capacity_kwh) * 100.0, 4)
        return {
            "simulated_soc_kwh_by_battery": soc_kwh,
            "simulated_soc_percent_by_battery": soc_percent,
            "simulation_soc_seeded_from_real_soc": True,
            "simulation_soc_resyncs_each_slot": False,
            "reference_continuity_valid": continuity["valid"],
            "reference_segment_id": continuity["segment_id"],
            "reference_segment_started_at": continuity["segment_started_at"],
            "reference_segment_reason": continuity["segment_reason"],
            "reference_untrusted_since": continuity["untrusted_since"],
            "reference_untrusted_reason": continuity["untrusted_reason"],
        }

    async def async_process_completed_slot(
        self,
        now: datetime | None = None,
    ) -> None:
        """Process the latest completed slot if one is ready."""
        async with self._process_lock:
            await self._async_process_completed_slot(now)

    async def _async_process_completed_slot(
        self,
        now: datetime | None,
    ) -> None:
        """Process one completed slot while holding the transaction lock."""
        now = now or datetime.now(tz=UTC)
        completed_slot = self._floor_to_slot(now) - self._interval
        last_processed = self.store.last_processed_slot()
        if last_processed is None or not self.store.last_meter_values():
            await self._async_seed(now)
            return
        seeded_cursor = self.store.data.get("meter_cursor_seeded") is True
        if seeded_cursor and self.store.has_slot(last_processed):
            self.store.data.pop("meter_cursor_seeded", None)
            self.store.mark_dirty()
            seeded_cursor = False
        next_slot = (
            last_processed if seeded_cursor else last_processed + self._interval
        )
        if completed_slot < next_slot:
            return
        if completed_slot != next_slot:
            self.store.invalidate_simulation(
                at=next_slot, reason="skipped_slot_boundary"
            )
            missing_slot = next_slot
            while missing_slot <= completed_slot:
                await self._async_append_gap(missing_slot, FLAG_GAP)
                missing_slot += self._interval
            await self._async_seed(
                now,
                processed_slot=completed_slot,
                simulation_reason="reinitialized_after_skipped_slots",
            )
            self._notify()
            return

        current_meters, meter_flags = self._read_meter_values()
        previous_meters = self.store.last_meter_values()
        deltas, delta_flags = self._meter_deltas(previous_meters, current_meters)
        for key in ("grid_import", "grid_export", "usage", "pv"):
            previous_value = previous_meters.get(key)
            current_value = current_meters.get(key)
            if (
                previous_value is not None
                and current_value is not None
                and current_value < previous_value
            ):
                current_meters[key] = None
        flags = meter_flags | delta_flags
        import_price = await self._async_price(CONF_SOURCE_IMPORT_PRICE, completed_slot)
        if import_price is None:
            flags |= FLAG_MISSING_IMPORT_PRICE
        export_price = await self._async_export_price(completed_slot)
        if export_price is None:
            flags |= FLAG_MISSING_EXPORT_PRICE

        self_import: float | None = None
        self_export: float | None = None
        simulation: tuple[float, float] | None = None
        if self.scenario_enabled("self_consumption"):
            simulation = self._simulate_self_consumption(deltas, completed_slot)
            if simulation is None:
                flags |= FLAG_SELF_CONSUMPTION_UNAVAILABLE
            else:
                self_import, self_export = simulation

        record = SlotRecord(
            start=completed_slot,
            import_price=import_price,
            export_price=export_price,
            grid_import=deltas.get("grid_import"),
            grid_export=deltas.get("grid_export"),
            usage=deltas.get("usage"),
            pv=deltas.get("pv"),
            self_consumption_grid_import=self_import,
            self_consumption_grid_export=self_export,
            self_consumption_segment_id=(
                self.store.simulation_continuity()["segment_id"]
                if simulation is not None
                else None
            ),
            flags=flags,
        )
        self.store.append_slot(record)
        self.store.update_metadata(
            last_processed_slot=completed_slot,
            last_meter_values=current_meters,
            meter_config=self._meter_config(),
        )
        self.store.data.pop("meter_cursor_seeded", None)
        if (
            self.scenario_enabled(SCENARIO_SELF_CONSUMPTION)
            and simulation is None
            and current_meters.get("usage") is not None
            and current_meters.get("pv") is not None
        ):
            self._start_self_consumption_segment(
                completed_slot + self._interval,
                reason="reinitialized_after_untrusted_slot",
            )
        self._notify()

    async def async_refresh(self, now: datetime | None = None) -> None:
        """Process due historical data and publish current aggregate state."""
        await self.async_process_completed_slot(now)
        self._notify()

    async def _async_timer(self, now: datetime) -> None:
        """Handle one scheduled tracker tick."""
        try:
            await self.async_refresh(now)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Historical cost tracking failed (entry_id=%s): %s",
                self.entry.entry_id,
                err,
            )
        finally:
            self._schedule_next(datetime.now(tz=UTC))

    def _schedule_next(self, now: datetime) -> None:
        """Schedule the next slot-aligned tick."""
        if self._unsub_timer is not None:
            self._unsub_timer()
        refresh_at = self._floor_to_slot(now) + self._interval + SCHEDULE_OFFSET
        self._unsub_timer = async_track_point_in_utc_time(
            self.hass,
            self._async_timer,
            refresh_at,
        )

    async def _async_seed(
        self,
        now: datetime,
        *,
        processed_slot: datetime | None = None,
        simulation_reason: str = "explicit_reinitialization",
    ) -> None:
        """Seed meter cursors and self-consumption SoC without creating a slot."""
        meters, _flags = self._read_meter_values()
        seed_slot = processed_slot or self._floor_to_slot(now)
        if self.scenario_enabled("self_consumption"):
            boundary = (
                seed_slot + self._interval if processed_slot is not None else seed_slot
            )
            self._start_self_consumption_segment(boundary, reason=simulation_reason)
        self.store.update_metadata(
            last_processed_slot=seed_slot,
            last_meter_values=meters,
            meter_config=self._meter_config(),
        )
        if processed_slot is None:
            self.store.data["meter_cursor_seeded"] = True
        else:
            self.store.data.pop("meter_cursor_seeded", None)
        self._schedule_next(now)

    async def _async_append_gap(self, slot_start: datetime, flags: int) -> None:
        """Record an explicit missing/gap slot."""
        record = SlotRecord(
            start=slot_start,
            import_price=None,
            export_price=None,
            grid_import=None,
            grid_export=None,
            usage=None,
            pv=None,
            self_consumption_segment_id=None,
            flags=flags,
        )
        self.store.append_slot(record)

    def _read_meter_values(self) -> tuple[dict[str, float | None], int]:
        """Read configured cumulative meter states."""
        config = self._meter_config()
        values: dict[str, float | None] = {
            "grid_import": None,
            "grid_export": 0.0,
            "usage": None,
            "pv": 0.0,
        }
        flags = 0
        for key, required in (
            ("grid_import", True),
            ("usage", True),
            ("grid_export", False),
            ("pv", False),
        ):
            entity_id = config.get(key)
            if not entity_id:
                if required:
                    flags |= FLAG_MISSING_METER
                continue
            value = self._float_state(str(entity_id))
            if value is None:
                flags |= FLAG_MISSING_METER
            values[key] = value
        return values, flags

    def _meter_deltas(
        self,
        previous: dict[str, float | None],
        current: dict[str, float | None],
    ) -> tuple[dict[str, float | None], int]:
        """Return cumulative meter deltas and validation flags."""
        deltas: dict[str, float | None] = {}
        flags = 0
        for key in ("grid_import", "grid_export", "usage", "pv"):
            previous_value = previous.get(key)
            current_value = current.get(key)
            if previous_value is None or current_value is None:
                deltas[key] = None
                flags |= FLAG_MISSING_METER
                continue
            delta = float(current_value) - float(previous_value)
            if not math.isfinite(delta):
                deltas[key] = None
                flags |= FLAG_MISSING_METER
                continue
            if delta < 0.0:
                deltas[key] = None
                flags |= FLAG_METER_RESET
                continue
            deltas[key] = delta
        return deltas, flags

    async def _async_price(self, source_key: str, slot_start: datetime) -> float | None:
        """Fetch one configured price value for a slot."""
        cache_kind = {
            CONF_SOURCE_IMPORT_PRICE: "import",
            CONF_SOURCE_EXPORT_PRICE: "export",
        }.get(source_key)
        if cache_kind is not None:
            cached = self.store.cached_price(slot_start, cache_kind)
            if cached is not None:
                return cached

        sources = self.entry.data.get(CONF_SOURCES, {})
        source_config = sources.get(source_key, {}) if isinstance(sources, dict) else {}
        if not isinstance(source_config, dict):
            return None
        if source_config.get(CONF_SOURCE_MODE) in {None, SOURCE_MODE_NOT_USED}:
            return None
        try:
            values = await self._source_provider(source_key, source_config).async_values(
                SourceWindow(
                    start_at=slot_start,
                    slot_minutes=self.slot_minutes,
                    slots=1,
                )
            )
        except SourceProviderError:
            return None
        if not values:
            return None
        try:
            value = float(values[0])
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    async def _async_export_price(self, slot_start: datetime) -> float | None:
        """Return export price, defaulting disabled export value to zero."""
        if not self._meter_config().get("grid_export"):
            return 0.0
        sources = self.entry.data.get(CONF_SOURCES, {})
        source_config = (
            sources.get(CONF_SOURCE_EXPORT_PRICE, {}) if isinstance(sources, dict) else {}
        )
        if not isinstance(source_config, dict):
            return 0.0
        if source_config.get(CONF_SOURCE_MODE) in {None, SOURCE_MODE_NOT_USED}:
            return 0.0
        return await self._async_price(CONF_SOURCE_EXPORT_PRICE, slot_start)

    def _source_provider(
        self,
        source_key: str,
        source_config: dict[str, Any],
    ) -> SourceProvider:
        """Return a cached source provider for price lookups."""
        provider_key = f"{source_key}:{source_config!r}"
        if provider := self._source_providers.get(provider_key):
            return provider
        provider = build_source_value_provider(
            self.hass,
            source_key=source_key,
            source_config=source_config,
        )
        self._source_providers[provider_key] = provider
        return provider

    def _simulate_self_consumption(
        self,
        deltas: dict[str, float | None],
        slot_start: datetime,
    ) -> tuple[float, float] | None:
        """Run and persist one self-consumption simulation slot."""
        usage = deltas.get("usage")
        pv = deltas.get("pv")
        if usage is None or pv is None:
            self.store.invalidate_simulation(
                at=slot_start, reason="missing_usage_or_pv"
            )
            return None
        batteries = self._battery_configs()
        if batteries is None:
            self.store.invalidate_simulation(
                at=slot_start, reason="invalid_battery_configuration"
            )
            return None
        if not self.store.simulation_continuity()["valid"]:
            return None
        soc = self.store.simulation_soc()
        if {battery.subentry_id for battery in batteries} != set(soc):
            self.store.invalidate_simulation(
                at=slot_start, reason="invalid_persisted_state"
            )
            return None
        try:
            result = simulate_self_consumption_slot(
                usage=float(usage),
                pv=float(pv),
                batteries=batteries,
                soc_by_battery=soc,
            )
        except ValueError:
            self.store.invalidate_simulation(
                at=slot_start, reason="invalid_simulation_state"
            )
            return None
        self.store.update_simulation_soc(result.soc_by_battery)
        return result.grid_import, result.grid_export

    def _start_self_consumption_segment(
        self, started_at: datetime, *, reason: str
    ) -> bool:
        """Start a visible comparison segment from real SoC at one boundary."""
        soc: dict[str, float] = {}
        batteries = self._battery_configs()
        if batteries is None:
            self.store.invalidate_simulation(
                at=started_at, reason="invalid_battery_configuration"
            )
            return False
        for battery in batteries:
            subentry = self.entry.subentries.get(battery.subentry_id)
            if subentry is None:
                continue
            raw = self._float_state(str(subentry.data.get(CONF_SOC_SOURCE)))
            if raw is None:
                continue
            state = self.hass.states.get(str(subentry.data.get(CONF_SOC_SOURCE)))
            if state is not None and state.attributes.get("unit_of_measurement") == "%":
                raw = (raw / 100.0) * battery.capacity_kwh
            soc[battery.subentry_id] = max(
                battery.minimum_kwh,
                min(battery.capacity_kwh, raw),
            )
        if len(soc) != len(batteries):
            self.store.invalidate_simulation(
                at=started_at, reason="missing_initial_battery_soc"
            )
            return False
        return self.store.start_simulation_segment(
            soc,
            started_at=started_at,
            reason=reason,
            configuration=self._simulation_configuration(batteries),
        )

    def _simulation_configuration(
        self, batteries: list[BatterySimulationConfig] | None = None
    ) -> list[dict[str, Any]]:
        """Return stable inputs whose changes require a new reference segment."""
        batteries = self._battery_configs() if batteries is None else batteries
        if batteries is None:
            return []
        return [
            {
                "subentry_id": battery.subentry_id,
                "minimum_kwh": battery.minimum_kwh,
                "capacity_kwh": battery.capacity_kwh,
                "max_charge_kwh": battery.max_charge_kwh,
                "max_discharge_kwh": battery.max_discharge_kwh,
                "charge_efficiency": battery.charge_efficiency,
                "discharge_efficiency": battery.discharge_efficiency,
                "can_charge_from_pv": battery.can_charge_from_pv,
            }
            for battery in batteries
        ]

    def _battery_configs(self) -> list[BatterySimulationConfig] | None:
        """Return configured batteries in config-entry order."""
        batteries: list[BatterySimulationConfig] = []
        for subentry in self.entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_TYPE_BATTERY:
                continue
            try:
                batteries.append(
                    BatterySimulationConfig(
                        subentry_id=subentry.subentry_id,
                        minimum_kwh=float(subentry.data[CONF_MINIMUM_KWH]),
                        capacity_kwh=float(subentry.data[CONF_CAPACITY_KWH]),
                        max_charge_kwh=self._kw_to_slot_kwh(
                            float(subentry.data[CONF_MAX_CHARGE_KW])
                        ),
                        max_discharge_kwh=self._kw_to_slot_kwh(
                            float(subentry.data[CONF_MAX_DISCHARGE_KW])
                        ),
                        charge_efficiency=float(
                            subentry.data.get(CONF_CHARGE_EFFICIENCY, 0.9)
                        ),
                        discharge_efficiency=float(
                            subentry.data.get(CONF_DISCHARGE_EFFICIENCY, 0.9)
                        ),
                        can_charge_from_pv=bool(
                            subentry.data.get(CONF_CAN_CHARGE_FROM_PV, True)
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return None
        return batteries

    def _meter_config(self) -> dict[str, str | None]:
        """Return normalized meter config from entry options."""
        return {
            "grid_import": self._option_entity(CONF_HISTORICAL_GRID_IMPORT_SENSOR),
            "grid_export": self._option_entity(CONF_HISTORICAL_GRID_EXPORT_SENSOR),
            "usage": self._option_entity(CONF_HISTORICAL_USAGE_SENSOR),
            "pv": self._option_entity(CONF_HISTORICAL_PV_SENSOR),
        }

    def _option_entity(self, key: str) -> str | None:
        value = self.entry.options.get(key)
        if not value:
            return None
        return str(value)

    def _float_state(self, entity_id: str) -> float | None:
        """Return a numeric state value."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _kw_to_slot_kwh(self, power_kw: float) -> float:
        return power_kw * (self.slot_minutes / 60.0)

    def _floor_to_slot(self, value: datetime) -> datetime:
        seconds = int(value.astimezone(UTC).timestamp())
        slot_seconds = self.slot_minutes * 60
        floored = (seconds // slot_seconds) * slot_seconds
        return datetime.fromtimestamp(floored, tz=UTC)

    def _notify(self) -> None:
        """Notify historical sensors."""
        for listener in list(self._listeners):
            listener()


def validate_energy_sensor(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Return whether an entity looks like a cumulative kWh energy sensor."""
    if not entity_id:
        return False
    if not str(entity_id).startswith("sensor."):
        return False
    state = hass.states.get(str(entity_id))
    if state is None:
        return True
    device_class = state.attributes.get("device_class")
    if device_class not in {None, SensorDeviceClass.ENERGY, "energy"}:
        return False
    unit = state.attributes.get("unit_of_measurement")
    return unit in {None, UnitOfEnergy.KILO_WATT_HOUR, "kWh"}
