"""Planning request assembly helpers for the coordinator."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_CAN_CHARGE_FROM_GRID,
    CONF_CAN_CHARGE_FROM_PV,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DURATION_MINUTES,
    CONF_ENERGY_KWH,
    CONF_EXPECTED_POWER_KW,
    CONF_HOURS_TO_PLAN,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MAX_DISCHARGE_KW,
    CONF_MEASURED_POWER_SOURCE,
    CONF_MIN_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_ON_MINUTES,
    CONF_MIN_OPTION_GAP_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_ON_OFF_SOURCE,
    CONF_OPTIMIZER_PROFILE,
    CONF_OPTIONS_COUNT,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_SLOT_MINUTES,
    CONF_SOC_SOURCE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_PREFER_PV_SURPLUS_CHARGING,
    OPTIMIZER_PROFILE_BALANCED,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_NOT_USED,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from ..coordinator_parts import PlanningStageError, StageErrorKind, TimingEntry
from ..historical_on_off_provider import HistoricalOnOffProvider
from ..source_pipeline import build_source_value_provider
from ..source_types import SourceProvider, SourceProviderError, SourceWindow
from ..target_runtime import clear_expired_battery_targets, get_active_battery_target

PROFILE_SETTINGS = {
    "aggressive": {
        "throughput_cost_per_kwh": 0.0,
        "action_deadband_kwh": 0.0,
        "mode_switch_cost": 0.0,
    },
    "balanced": {
        "throughput_cost_per_kwh": 0.02,
        "action_deadband_kwh": 0.05,
        "mode_switch_cost": 0.01,
    },
    "conservative": {
        "throughput_cost_per_kwh": 0.08,
        "action_deadband_kwh": 0.1,
        "mode_switch_cost": 0.03,
    },
}

type SourceIssueRecorder = Callable[..., None]


def _duration_ms(started_at: float) -> int:
    """Return elapsed monotonic time in whole milliseconds."""
    return int(round((time.monotonic() - started_at) * 1000))


def _configured_source(
    sources: dict[str, Any], source_key: str
) -> dict[str, Any] | None:
    """Return one source config when configured and enabled."""
    source = sources.get(source_key)
    if not isinstance(source, dict):
        return None
    if source.get(CONF_SOURCE_MODE) in {None, SOURCE_MODE_NOT_USED}:
        return None
    return source


class PlanningRequestBuilder:
    """Build optimizer requests from current integration state."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        source_providers: dict[str, SourceProvider],
        on_off_providers: dict[str, HistoricalOnOffProvider],
        record_source_issue: SourceIssueRecorder,
    ) -> None:
        self._hass = hass
        self._source_providers = source_providers
        self._on_off_providers = on_off_providers
        self._record_source_issue = record_source_issue

    async def async_build_request(
        self, entry: ConfigEntry
    ) -> tuple[dict[str, Any], list[TimingEntry]]:
        """Fetch and validate all inputs and build a planning request."""
        slot_minutes = int(entry.data[CONF_SLOT_MINUTES])
        hours_to_plan = int(entry.data[CONF_HOURS_TO_PLAN])
        expected_slots = int((hours_to_plan * 60) / slot_minutes)
        window = SourceWindow(
            start_at=self._floor_to_slot(datetime.now(tz=UTC), slot_minutes),
            slot_minutes=slot_minutes,
            slots=expected_slots,
        )

        sources = entry.data.get(CONF_SOURCES, {})
        if not isinstance(sources, dict):
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                "Source configuration is missing or invalid",
            )

        timings: list[TimingEntry] = []

        started_at = time.monotonic()
        price_values = await self._async_resolve_source(
            entry=entry,
            source_key=CONF_SOURCE_IMPORT_PRICE,
            source_config=sources.get(CONF_SOURCE_IMPORT_PRICE, {}),
            window=window,
        )
        timings.append(("Import price source fetch", _duration_ms(started_at)))

        export_price_source = _configured_source(sources, CONF_SOURCE_EXPORT_PRICE)
        export_price_values = await self._async_optional_source_values(
            entry=entry,
            source_key=CONF_SOURCE_EXPORT_PRICE,
            source_config=export_price_source,
            window=window,
            blocks_planning=False,
            timing_label="Export price source fetch",
            timings=timings,
        )

        usage_source = _configured_source(sources, CONF_SOURCE_USAGE)
        usage_values = await self._async_optional_source_values(
            entry=entry,
            source_key=CONF_SOURCE_USAGE,
            source_config=usage_source,
            window=window,
            blocks_planning=True,
            timing_label="Usage source fetch",
            timings=timings,
        )

        pv_source = _configured_source(sources, CONF_SOURCE_PV)
        pv_values = await self._async_optional_source_values(
            entry=entry,
            source_key=CONF_SOURCE_PV,
            source_config=pv_source,
            window=window,
            blocks_planning=False,
            timing_label="PV source fetch",
            timings=timings,
        )

        usage_forecast_points: list[dict[str, Any]] | None = None
        if (
            usage_values is not None
            and usage_source is not None
            and usage_source.get(CONF_SOURCE_MODE) == SOURCE_MODE_BUILT_IN
        ):
            usage_forecast_points = self._values_to_points(usage_values, window)

        runtime_data = entry.runtime_data
        battery_entities: list[dict[str, Any]] = []
        comfort_entities: list[dict[str, Any]] = []
        optional_entities: list[dict[str, Any]] = []

        battery_name_to_subentry: dict[str, str] = {}
        comfort_name_to_subentry: dict[str, str] = {}
        optional_name_to_subentry: dict[str, str] = {}

        clear_expired_battery_targets(runtime_data)
        rolling_window_slots_set: set[int] = set()
        for subentry in entry.subentries.values():
            if subentry.subentry_type == SUBENTRY_TYPE_BATTERY:
                name = str(subentry.data.get(CONF_NAME, subentry.title))
                subentry_id = subentry.subentry_id
                capacity_kwh = float(subentry.data[CONF_CAPACITY_KWH])
                initial_kwh = self._battery_initial_kwh(
                    str(subentry.data[CONF_SOC_SOURCE]),
                    f"battery `{name}` SoC source",
                    capacity_kwh=capacity_kwh,
                )
                can_charge_from = (
                    (1 if bool(subentry.data.get(CONF_CAN_CHARGE_FROM_GRID, False)) else 0)
                    | (2 if bool(subentry.data.get(CONF_CAN_CHARGE_FROM_PV, True)) else 0)
                )
                battery_payload: dict[str, Any] = {
                    "name": name,
                    "initial_kwh": initial_kwh,
                    "minimum_kwh": float(subentry.data[CONF_MINIMUM_KWH]),
                    "capacity_kwh": capacity_kwh,
                    "charge_efficiency": float(
                        subentry.data.get(CONF_CHARGE_EFFICIENCY, 0.9)
                    ),
                    "discharge_efficiency": float(
                        subentry.data.get(CONF_DISCHARGE_EFFICIENCY, 0.9)
                    ),
                    "charge_curve_kwh": [
                        self._kw_to_slot_kwh(
                            float(subentry.data[CONF_MAX_CHARGE_KW]),
                            slot_minutes=slot_minutes,
                        )
                    ],
                    "discharge_curve_kwh": [
                        self._kw_to_slot_kwh(
                            float(subentry.data[CONF_MAX_DISCHARGE_KW]),
                            slot_minutes=slot_minutes,
                        )
                    ],
                    "can_charge_from": can_charge_from,
                    "prefer_pv_surplus_charging": bool(
                        subentry.data.get(CONF_PREFER_PV_SURPLUS_CHARGING, False)
                    ),
                }
                if target := get_active_battery_target(runtime_data, subentry_id):
                    target_slot = self._target_timeslot_from_timestamp(
                        target.reach_at,
                        start_at=window.start_at,
                        slot_minutes=slot_minutes,
                        horizon_slots=expected_slots,
                    )
                    if target_slot is not None:
                        battery_payload["target"] = {
                            "timeslot": target_slot,
                            "soc_kwh": float(target.soc_kwh),
                        }
                battery_entities.append(battery_payload)
                battery_name_to_subentry[name] = subentry_id
                continue

            if subentry.subentry_type == SUBENTRY_TYPE_COMFORT:
                name = str(subentry.data.get(CONF_NAME, subentry.title))
                subentry_id = subentry.subentry_id
                rolling_window_slots = max(
                    1, int(round((float(subentry.data[CONF_ROLLING_WINDOW_HOURS]) * 60) / slot_minutes))
                )
                rolling_window_slots_set.add(rolling_window_slots)
                on_off_source = str(subentry.data[CONF_ON_OFF_SOURCE])
                try:
                    is_on_now, on_slots_last_window, off_streak_slots_now = (
                        await self._on_off_provider(on_off_source).async_runtime_state(
                            rolling_window_slots=rolling_window_slots,
                            slot_minutes=slot_minutes,
                        )
                    )
                except ValueError as err:
                    raise PlanningStageError(
                        StageErrorKind.PLANNER_INPUT,
                        str(err),
                        details={"source": "comfort_history", "entity_id": on_off_source},
                    ) from err
                except Exception as err:
                    raise PlanningStageError(
                        StageErrorKind.SOURCE_FETCH,
                        f"Failed to read comfort history for `{on_off_source}`: {err}",
                        details={"source": "comfort_history", "entity_id": on_off_source},
                    ) from err
                comfort_entities.append(
                    {
                        "name": name,
                        "target_on_slots_per_rolling_window": max(
                            1,
                            int(
                                round(
                                    (
                                        float(subentry.data[CONF_TARGET_ON_HOURS_PER_WINDOW]) * 60
                                    )
                                    / slot_minutes
                                )
                            ),
                        ),
                        "min_consecutive_on_slots": max(
                            1,
                            int(round(int(subentry.data[CONF_MIN_CONSECUTIVE_ON_MINUTES]) / slot_minutes)),
                        ),
                        "min_consecutive_off_slots": max(
                            1,
                            int(round(int(subentry.data[CONF_MIN_CONSECUTIVE_OFF_MINUTES]) / slot_minutes)),
                        ),
                        "max_consecutive_off_slots": max(
                            1,
                            int(round(int(subentry.data[CONF_MAX_CONSECUTIVE_OFF_MINUTES]) / slot_minutes)),
                        ),
                        "power_usage_kwh": float(subentry.data[CONF_EXPECTED_POWER_KW])
                        * (slot_minutes / 60),
                        "is_on_now": is_on_now,
                        "on_slots_last_rolling_window": on_slots_last_window,
                        "off_streak_slots_now": off_streak_slots_now,
                        "measured_power_source": subentry.data.get(CONF_MEASURED_POWER_SOURCE),
                    }
                )
                comfort_name_to_subentry[name] = subentry_id
                continue

            if subentry.subentry_type == SUBENTRY_TYPE_OPTIONAL:
                name = str(subentry.data.get(CONF_NAME, subentry.title))
                subentry_id = subentry.subentry_id
                duration_slots = max(
                    1, int(round(int(subentry.data[CONF_DURATION_MINUTES]) / slot_minutes))
                )
                start_before_slots = min(
                    expected_slots,
                    max(
                        1,
                        int(round((float(subentry.data[CONF_RUN_WITHIN_HOURS]) * 60) / slot_minutes)),
                    ),
                )
                optional_entities.append(
                    {
                        "name": name,
                        "duration_timeslots": duration_slots,
                        "start_after_timeslot": 0,
                        "start_before_timeslot": start_before_slots,
                        "energy_kwh": float(subentry.data[CONF_ENERGY_KWH]),
                        "options": int(subentry.data[CONF_OPTIONS_COUNT]),
                        "min_option_gap_timeslots": max(
                            0,
                            int(round(int(subentry.data[CONF_MIN_OPTION_GAP_MINUTES]) / slot_minutes)),
                        ),
                    }
                )
                optional_name_to_subentry[name] = subentry_id

        if len(rolling_window_slots_set) > 1:
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                "All comfort loads must use the same rolling window duration",
            )

        return (
            {
                "entry_id": entry.entry_id,
                "slot_minutes": slot_minutes,
                "hours_to_plan": hours_to_plan,
                "window": window,
                "optimizer_params": {
                    "grid_import_price_per_kwh": price_values,
                    "grid_export_price_per_kwh": (
                        export_price_values if export_price_values is not None else [0.0] * expected_slots
                    ),
                    "solar_input_kwh": pv_values if pv_values is not None else [0.0] * expected_slots,
                    "usage_kwh": usage_values if usage_values is not None else [0.0] * expected_slots,
                    "rolling_window_slots": (
                        next(iter(rolling_window_slots_set)) if rolling_window_slots_set else 24
                    ),
                    **PROFILE_SETTINGS.get(
                        str(entry.options.get(CONF_OPTIMIZER_PROFILE, OPTIMIZER_PROFILE_BALANCED)),
                        PROFILE_SETTINGS[OPTIMIZER_PROFILE_BALANCED],
                    ),
                    "battery_entities": battery_entities,
                    "comfort_entities": comfort_entities,
                    "optional_entities": optional_entities,
                    "state": runtime_data.optimizer_state,
                },
                "name_to_subentry": {
                    "batteries": battery_name_to_subentry,
                    "comforts": comfort_name_to_subentry,
                    "optionals": optional_name_to_subentry,
                },
                "usage_forecast_points": usage_forecast_points,
            },
            timings,
        )

    async def _async_optional_source_values(
        self,
        *,
        entry: ConfigEntry,
        source_key: str,
        source_config: dict[str, Any] | None,
        window: SourceWindow,
        blocks_planning: bool,
        timing_label: str,
        timings: list[TimingEntry],
    ) -> list[float] | None:
        """Resolve one optional source and append its timing when enabled."""
        if source_config is None:
            return None
        started_at = time.monotonic()
        values = await self._async_resolve_optional_source(
            entry=entry,
            source_key=source_key,
            source_config=source_config,
            window=window,
            blocks_planning=blocks_planning,
        )
        timings.append((timing_label, _duration_ms(started_at)))
        return values

    async def _async_resolve_source(
        self,
        *,
        entry: ConfigEntry,
        source_key: str,
        source_config: dict[str, Any],
        window: SourceWindow,
    ) -> list[float]:
        """Resolve one required source through shared provider logic."""
        mode = source_config.get(CONF_SOURCE_MODE)
        if not mode or mode == SOURCE_MODE_NOT_USED:
            raise PlanningStageError(
                StageErrorKind.SOURCE_VALIDATION,
                f"{source_key} source is required and must be configured",
                details={"source": source_key},
            )
        try:
            provider = self._source_provider(source_key, source_config)
            values = await provider.async_values(window)
            self._record_source_issue(
                entry=entry,
                source_key=source_key,
                source_config=source_config,
                provider=provider,
            )
        except SourceProviderError as err:
            self._record_source_issue(
                entry=entry,
                source_key=source_key,
                source_config=source_config,
                provider=self._source_provider(source_key, source_config),
            )
            raise PlanningStageError(
                self._source_kind_from_error_code(err.code),
                str(err),
                details=err.details,
            ) from err
        return values

    async def _async_resolve_optional_source(
        self,
        *,
        entry: ConfigEntry,
        source_key: str,
        source_config: dict[str, Any],
        window: SourceWindow,
        blocks_planning: bool,
    ) -> list[float] | None:
        """Resolve one optional source when explicitly configured."""
        mode = source_config.get(CONF_SOURCE_MODE)
        if not mode or mode == SOURCE_MODE_NOT_USED:
            return None
        try:
            return await self._async_resolve_source(
                entry=entry,
                source_key=source_key,
                source_config=source_config,
                window=window,
            )
        except PlanningStageError:
            if blocks_planning:
                raise
            return None

    def _source_kind_from_error_code(self, code: str) -> StageErrorKind:
        """Map source provider error code to coordinator stage kind."""
        if code == StageErrorKind.SOURCE_FETCH.value:
            return StageErrorKind.SOURCE_FETCH
        if code == StageErrorKind.SOURCE_PARSE.value:
            return StageErrorKind.SOURCE_PARSE
        return StageErrorKind.SOURCE_VALIDATION

    def _source_provider(
        self, source_key: str, source_config: dict[str, Any]
    ) -> SourceProvider:
        """Return cached source provider instance for the given config."""
        provider_key = f"{source_key}:{source_config!r}"
        if provider := self._source_providers.get(provider_key):
            return provider
        provider = build_source_value_provider(
            self._hass,
            source_key=source_key,
            source_config=source_config,
        )
        self._source_providers[provider_key] = provider
        return provider

    def _values_to_points(
        self, values: list[float], window: SourceWindow
    ) -> list[dict[str, Any]]:
        """Project a values array to `{start, value}` points."""
        return [
            {
                "start": (
                    window.start_at + timedelta(minutes=index * window.slot_minutes)
                ).isoformat(),
                "value": float(value),
            }
            for index, value in enumerate(values)
        ]

    def _floor_to_slot(self, value: datetime, slot_minutes: int) -> datetime:
        """Floor datetime down to the nearest slot boundary."""
        seconds = int(value.timestamp())
        slot_seconds = slot_minutes * 60
        floored = (seconds // slot_seconds) * slot_seconds
        return datetime.fromtimestamp(floored, tz=UTC)

    def _float_state(self, entity_id: str, label: str) -> float:
        """Return a numeric entity state or raise planner input error."""
        state = self._hass.states.get(entity_id)
        if state is None:
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                f"{label} entity `{entity_id}` was not found",
            )
        try:
            return float(state.state)
        except (TypeError, ValueError) as err:
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                f"{label} state for `{entity_id}` is not numeric",
            ) from err

    def _battery_initial_kwh(
        self, entity_id: str, label: str, *, capacity_kwh: float
    ) -> float:
        """Return battery SoC normalized to kWh for the optimizer."""
        state = self._hass.states.get(entity_id)
        if state is None:
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                f"{label} entity `{entity_id}` was not found",
            )
        try:
            numeric_value = float(state.state)
        except (TypeError, ValueError) as err:
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                f"{label} state for `{entity_id}` is not numeric",
            ) from err
        if state.attributes.get("unit_of_measurement") == "%":
            return max(0.0, min(capacity_kwh, (numeric_value / 100.0) * capacity_kwh))
        return numeric_value

    def _target_timeslot_from_timestamp(
        self,
        reach_at: datetime,
        *,
        start_at: datetime,
        slot_minutes: int,
        horizon_slots: int,
    ) -> int | None:
        """Map a target timestamp to optimizer target timeslot."""
        delta_seconds = (reach_at.astimezone(UTC) - start_at).total_seconds()
        if delta_seconds <= 0:
            return 0
        slot_seconds = slot_minutes * 60
        target_slot = max(0, int(math.ceil(delta_seconds / slot_seconds) - 1))
        if target_slot >= horizon_slots:
            return None
        return target_slot

    def _on_off_provider(self, entity_id: str) -> HistoricalOnOffProvider:
        """Return or create cached historical on/off provider for one entity."""
        provider = self._on_off_providers.get(entity_id)
        if provider is not None:
            return provider
        provider = HistoricalOnOffProvider(self._hass, entity_id)
        self._on_off_providers[entity_id] = provider
        return provider

    def _kw_to_slot_kwh(self, power_kw: float, *, slot_minutes: int) -> float:
        """Convert a power limit in kW to energy per solver slot in kWh."""
        return power_kw * (slot_minutes / 60.0)
