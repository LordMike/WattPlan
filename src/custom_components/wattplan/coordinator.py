"""Coordinator for the WattPlan integration."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import logging
import time
from typing import Any

from pydantic import ValidationError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify

from .const import (
    OPTIMIZER_PROFILE_AGGRESSIVE,
    OPTIMIZER_PROFILE_BALANCED,
    OPTIMIZER_PROFILE_CONSERVATIVE,
    DOMAIN,
)
from .coordinator_parts import (
    CoordinatorSnapshot,
    CycleTrigger,
    EmitStageError,
    PlanningStageError,
    Stage,
    StageErrorKind,
    StageErrorState,
    TimingEntry,
    parse_snapshot_datetime,
    snapshot_schema_id,
)
from .coordinator_logic import PlanningRequestBuilder, SourceStatusManager
from .historical_on_off_provider import HistoricalOnOffProvider
from .optimizer import OptimizationParams, optimize
from .source_issues import (
    clear_entry_source_issues,
)
from .source_types import SourceProvider

_LOGGER = logging.getLogger(__name__)
HEARTBEAT_OFFSET = timedelta(minutes=3)
STORAGE_VERSION = 1


def _duration_ms(started_at: float) -> int:
    """Return elapsed monotonic time in whole milliseconds."""
    return int(round((time.monotonic() - started_at) * 1000))


def _snapshot_schema_id() -> str:
    """Return schema identity for serialized snapshot cache."""
    return snapshot_schema_id()


SCHEDULE_OFFSET = timedelta(seconds=2)


class WattPlanCoordinator(DataUpdateCoordinator[CoordinatorSnapshot | None]):
    """Run planner and emission stages on one fixed scheduler."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        update_interval: timedelta,
        planning_enabled: bool,
        action_emission_enabled: bool,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry_id}",
            update_interval=update_interval,
        )
        self._entry_id = entry_id
        self._base_update_interval = update_interval
        self._planning_enabled = planning_enabled
        self._action_emission_enabled = action_emission_enabled

        self._snapshot: CoordinatorSnapshot | None = None
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_duration_ms: int | None = None
        self._last_run_timings: list[TimingEntry] | None = None
        self._next_refresh_at: datetime | None = None

        self._plan_error = StageErrorState()
        self._emit_error = StageErrorState()

        self._plan_lock = asyncio.Lock()
        self._emit_lock = asyncio.Lock()
        self._heartbeat_start_unsub: CALLBACK_TYPE | None = None
        self._heartbeat_interval_unsub: CALLBACK_TYPE | None = None
        self._last_heartbeat_stale: bool | None = None
        self._on_off_providers: dict[str, HistoricalOnOffProvider] = {}
        self._source_providers: dict[str, SourceProvider] = {}
        self._source_status = SourceStatusManager(hass)
        self._snapshot_store = Store[dict[str, Any]](
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.snapshot.{entry_id}",
            private=True,
        )
        self._snapshot_schema_id = snapshot_schema_id()
        self._planning = PlanningRequestBuilder(
            hass,
            source_providers=self._source_providers,
            on_off_providers=self._on_off_providers,
            record_source_issue=self._source_status.record_source_issue_if_needed,
        )

        if not self.scheduler_enabled:
            self._set_update_interval(None)
        else:
            self._async_start_heartbeat()

    @property
    def scheduler_enabled(self) -> bool:
        """Return whether scheduled ticks should run."""
        return self._planning_enabled or self._action_emission_enabled

    @property
    def snapshot(self) -> CoordinatorSnapshot | None:
        """Return the latest immutable snapshot."""
        return self._snapshot

    @property
    def has_usable_plan(self) -> bool:
        """Return whether the current coordinator state exposes a usable plan."""
        return bool(self.overall_status.get("has_usable_plan", False))

    @property
    def overall_status(self) -> dict[str, Any]:
        """Return current top-level health payload."""
        return self._source_status.overall_status(is_stale=self.is_stale)

    def source_status(self, source_key: str) -> dict[str, Any] | None:
        """Return current source health payload for one source."""
        return self._source_status.source_status(source_key)

    @property
    def last_attempt_at(self) -> datetime | None:
        """Return when the last cycle attempt started."""
        return self._last_attempt_at

    @property
    def last_success_at(self) -> datetime | None:
        """Return when the last stage completed successfully."""
        return self._last_success_at

    @property
    def last_duration_ms(self) -> int | None:
        """Return duration in milliseconds for the last cycle attempt."""
        return self._last_duration_ms

    @property
    def last_run_timings(self) -> list[TimingEntry] | None:
        """Return timing entries for the last planning run."""
        if self._last_run_timings is None:
            return None
        return list(self._last_run_timings)

    @property
    def next_refresh_at(self) -> datetime | None:
        """Return the next scheduled refresh timestamp."""
        return self._next_refresh_at

    @property
    def has_error(self) -> bool:
        """Return aggregate coordinator error state."""
        return self._plan_error.has_error or self._emit_error.has_error or self.is_stale

    @property
    def is_stale(self) -> bool:
        """Return whether the fixed scheduler appears stalled."""
        if (expires_at := self.expires_at) is None:
            return False
        return datetime.now(tz=UTC) > expires_at

    @property
    def expires_at(self) -> datetime | None:
        """Return when coordinator state should be considered expired."""
        if self.update_interval is None or self._last_attempt_at is None:
            return None
        return self._last_attempt_at + (self.update_interval * 2)

    async def async_set_runtime_flags(
        self, *, planning_enabled: bool, action_emission_enabled: bool
    ) -> None:
        """Update planner and emission flags at runtime."""
        self._planning_enabled = planning_enabled
        self._action_emission_enabled = action_emission_enabled
        self._set_update_interval(
            self._base_update_interval if self.scheduler_enabled else None
        )
        if self.scheduler_enabled:
            self._async_start_heartbeat()
        else:
            self._async_stop_heartbeat()

    def _set_update_interval(self, interval: timedelta | None) -> None:
        """Set coordinator update interval and re-schedule when needed."""
        self.update_interval = interval
        if not self._listeners:
            return
        if interval is None:
            self._unschedule_refresh()
            return
        self._schedule_refresh()

    @callback
    def _schedule_refresh(self) -> None:
        """Schedule the next refresh aligned to the planner interval."""
        if self._update_interval_seconds is None:
            return

        if self.config_entry and self.config_entry.pref_disable_polling:
            return

        self._async_unsub_refresh()
        self._next_refresh_at = None

        update_interval = self._update_interval_seconds
        if self._retry_after is not None:
            update_interval = self._retry_after
            self._retry_after = None

        now = datetime.now(tz=UTC)
        if float(update_interval).is_integer():
            refresh_at = self._aligned_refresh_time(
                now,
                interval=timedelta(seconds=int(update_interval)),
            )
        else:
            refresh_at = now + timedelta(seconds=update_interval)

        self._next_refresh_at = refresh_at
        self._unsub_refresh = async_track_point_in_utc_time(
            self.hass,
            self._async_handle_refresh_interval,
            refresh_at,
        )

    @callback
    def _async_handle_refresh_interval(self, _now: datetime) -> None:
        """Run the coordinator refresh callback on the HA event loop."""
        self._DataUpdateCoordinator__wrap_handle_refresh_interval()

    async def async_shutdown(self) -> None:
        """Stop coordinator background callbacks."""
        self._async_stop_heartbeat()
        clear_entry_source_issues(self.hass, self._entry_id)

    async def _async_update_data(self) -> CoordinatorSnapshot | None:
        """Handle one scheduled tick."""
        self._next_refresh_at = None
        await self.async_tick(trigger=CycleTrigger.SCHEDULE)
        return self._snapshot

    async def async_tick(self, *, trigger: CycleTrigger) -> None:
        """Run one fixed-interval tick with conditional stage execution."""
        if not self.scheduler_enabled and trigger is CycleTrigger.SCHEDULE:
            return

        self._last_attempt_at = datetime.now(tz=UTC)
        started = datetime.now(tz=UTC)

        if self._planning_enabled:
            try:
                await self.async_plan(trigger=trigger)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Plan stage failed (entry_id=%s, trigger=%s): %s",
                    self._entry_id,
                    trigger,
                    err,
                )

        if self._action_emission_enabled:
            try:
                await self.async_emit(trigger=trigger)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Emit stage failed (entry_id=%s, trigger=%s): %s",
                    self._entry_id,
                    trigger,
                    err,
                )

        self._last_duration_ms = int(
            (datetime.now(tz=UTC) - started).total_seconds() * 1000
        )

    async def async_plan(self, *, trigger: CycleTrigger) -> None:
        """Run the planning stage and replace the immutable snapshot."""
        if self._plan_lock.locked():
            self._mark_locked(Stage.PLAN, trigger)
            if trigger is CycleTrigger.SERVICE:
                raise ServiceValidationError("Planning run already in progress")
            return

        started = datetime.now(tz=UTC)
        total_started = time.monotonic()
        self._last_attempt_at = started
        self._last_run_timings = None

        async with self._plan_lock:
            try:
                self._source_status.reset()
                entry = self._require_entry()
                request, timings = await self._async_build_planning_request(entry)
                planner_result = await self._async_run_optimizer(
                    request, entry.runtime_data, timings=timings
                )
                planner_output = self._planner_output_from_result(
                    request, planner_result, timings=timings
                )
                self._append_total_timing(
                    timings=timings,
                    total_ms=_duration_ms(total_started),
                )
                self._last_run_timings = list(timings)
                new_snapshot = self._project_snapshot(planner_output)
                self._snapshot = new_snapshot
                self.data = new_snapshot
                await self.async_persist_snapshot()
                self._clear_stage_error(Stage.PLAN)
                self._last_success_at = datetime.now(tz=UTC)
                self._source_status.recompute_overall_status(
                    planner_output=planner_output,
                    snapshot=self._snapshot,
                )
                self._sync_source_issues(entry)
                if trigger is CycleTrigger.SERVICE:
                    self.async_update_listeners()
            except PlanningStageError as err:
                self._source_status.mark_failed_status(err)
                self._sync_source_issues(self._require_entry())
                self._set_stage_error(
                    Stage.PLAN,
                    err.kind,
                    str(err),
                    details=err.details,
                )
                # Push state immediately so entities can reflect failures and
                # availability changes without waiting for the next tick.
                self.async_update_listeners()
                raise
            except ServiceValidationError:
                raise
            except Exception as err:
                self._source_status.mark_failed_status(err)
                self._sync_source_issues(self._require_entry())
                self._set_stage_error(
                    Stage.PLAN,
                    self._classify_plan_error(err),
                    str(err),
                )
                # Push state immediately so entities can reflect failures and
                # availability changes without waiting for the next tick.
                self.async_update_listeners()
                raise
            finally:
                self._last_duration_ms = int(
                    (datetime.now(tz=UTC) - started).total_seconds() * 1000
                )

    async def async_emit(self, *, trigger: CycleTrigger) -> None:
        """Run the emission stage against the latest immutable snapshot."""
        if self._emit_lock.locked():
            self._mark_locked(Stage.EMIT, trigger)
            if trigger is CycleTrigger.SERVICE:
                raise ServiceValidationError("Emit run already in progress")
            return

        started = datetime.now(tz=UTC)
        self._last_attempt_at = started

        async with self._emit_lock:
            try:
                snapshot = self._require_snapshot()

                diagnostics = dict(snapshot.diagnostics or {})
                diagnostics["emit"] = {
                    "at": datetime.now(tz=UTC).isoformat(),
                    "trigger": str(trigger),
                }
                self._snapshot = replace(snapshot, diagnostics=diagnostics)
                self.data = self._snapshot

                self._clear_stage_error(Stage.EMIT)
                self._last_success_at = datetime.now(tz=UTC)
                if trigger is CycleTrigger.SERVICE:
                    self.async_update_listeners()
            except EmitStageError as err:
                self._set_stage_error(
                    Stage.EMIT,
                    err.kind,
                    str(err),
                    details=err.details,
                )
                # Push state immediately so entities can reflect failures and
                # availability changes without waiting for the next tick.
                self.async_update_listeners()
                if trigger is CycleTrigger.SERVICE:
                    raise ServiceValidationError(str(err)) from err
                raise
            except ServiceValidationError:
                raise
            except Exception as err:
                self._set_stage_error(
                    Stage.EMIT,
                    self._classify_emit_error(err),
                    str(err),
                )
                # Push state immediately so entities can reflect failures and
                # availability changes without waiting for the next tick.
                self.async_update_listeners()
                if trigger is CycleTrigger.SERVICE:
                    raise ServiceValidationError(str(err)) from err
                raise
            finally:
                self._last_duration_ms = int(
                    (datetime.now(tz=UTC) - started).total_seconds() * 1000
                )

    def _require_snapshot(self) -> CoordinatorSnapshot:
        """Return current snapshot or raise if none is available."""
        if self._snapshot is None:
            raise EmitStageError(
                StageErrorKind.EMIT_NO_SNAPSHOT,
                "No plan snapshot is available",
            )
        return self._snapshot

    def _require_entry(self) -> ConfigEntry:
        """Return config entry for this coordinator."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                "Config entry is not available",
                details={"entry_id": self._entry_id},
            )
        return entry

    async def _async_build_planning_request(
        self, entry: ConfigEntry
    ) -> tuple[dict[str, Any], list[TimingEntry]]:
        """Fetch and validate all inputs and build a planning request."""
        return await self._planning.async_build_request(entry)

    async def async_build_planner_input_export(self) -> dict[str, Any]:
        """Rebuild and return the current planning request for export."""
        entry = self._require_entry()
        request, _timings = await self._async_build_planning_request(entry)
        return request


    def _sync_source_issues(self, entry: ConfigEntry) -> None:
        """Publish the current source issue set to the repairs dashboard."""
        self._source_status.sync_source_issues(entry)

    def _aligned_refresh_time(self, now: datetime, *, interval: timedelta) -> datetime:
        """Return the next aligned refresh time with a small safety offset."""
        interval_seconds = int(interval.total_seconds())
        target = now + interval
        aligned_seconds = (int(target.timestamp()) // interval_seconds) * interval_seconds
        refresh_at = datetime.fromtimestamp(aligned_seconds, tz=UTC) + SCHEDULE_OFFSET
        if refresh_at <= now:
            refresh_at += interval
        return refresh_at

    async def _async_run_optimizer(
        self,
        request: dict[str, Any],
        runtime_data: Any,
        *,
        timings: list[TimingEntry],
    ) -> dict[str, Any]:
        """Run poweroptim in the executor and normalize planner exceptions."""
        optimizer_params = request["optimizer_params"]
        _LOGGER.debug(
            (
                "Running optimizer (entry_id=%s, start_at=%s, slot_minutes=%s, "
                "hours_to_plan=%s, import_price_sample=%s, export_price_sample=%s, "
                "usage_sample=%s, pv_sample=%s, "
                "batteries=%s, comforts=%s, optionals=%s)"
            ),
            request["entry_id"],
            request["window"].start_at.isoformat(),
            request["slot_minutes"],
            request["hours_to_plan"],
            self._sample_values(optimizer_params["grid_import_price_per_kwh"]),
            self._sample_values(optimizer_params["grid_export_price_per_kwh"]),
            self._sample_values(optimizer_params["usage_kwh"]),
            self._sample_values(optimizer_params["solar_input_kwh"]),
            len(optimizer_params["battery_entities"]),
            len(optimizer_params["comfort_entities"]),
            len(optimizer_params["optional_entities"]),
        )
        _LOGGER.debug(
            "Optimizer battery payloads (entry_id=%s): %s",
            request["entry_id"],
            optimizer_params["battery_entities"],
        )
        _LOGGER.debug(
            "Optimizer comfort payloads (entry_id=%s): %s",
            request["entry_id"],
            optimizer_params["comfort_entities"],
        )
        _LOGGER.debug(
            "Optimizer optional payloads (entry_id=%s): %s",
            request["entry_id"],
            optimizer_params["optional_entities"],
        )
        try:
            params = OptimizationParams(**optimizer_params)
        except ValidationError as err:
            raise PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                f"Planner input validation failed: {err}",
            ) from err

        try:
            started_at = time.monotonic()
            result = await self.hass.async_add_executor_job(optimize, params)
        except Exception as err:
            raise PlanningStageError(
                StageErrorKind.PLANNER_EXECUTION,
                f"Optimizer execution failed: {err}",
            ) from err

        timings.append(
            (
                "Optimizer plan calculation",
                int(
                    round(
                        float(
                            result.get(
                                "execution_time", _duration_ms(started_at) / 1000
                            )
                        )
                        * 1000
                    )
                ),
            )
        )
        runtime_data.optimizer_state = result.get("state")
        return result

    def _sample_values(self, values: list[float], sample_size: int = 6) -> list[float]:
        """Return a short rounded sample for debug logging."""
        return [round(value, 4) for value in values[:sample_size]]

    def _kw_to_slot_kwh(self, power_kw: float, *, slot_minutes: int) -> float:
        """Convert a power limit in kW to energy per solver slot in kWh."""
        return power_kw * (slot_minutes / 60.0)

    def _next_change(
        self,
        schedule: list[dict[str, Any]],
        *,
        key: str,
        start_at: datetime,
        slot_minutes: int,
    ) -> tuple[datetime, Any] | None:
        """Return when a schedule key next changes, and the value it changes to.

        This feeds the action sensors. For a battery schedule like:
        charge -> hold -> hold -> discharge
        the next change from the current slot is the timestamp of the first
        "hold" slot, and the next action value should be "hold".
        """
        if not schedule:
            return None
        current = schedule[0].get(key)
        for index, point in enumerate(schedule[1:], start=1):
            if point.get(key) != current:
                return (
                    start_at + timedelta(minutes=index * slot_minutes),
                    point.get(key),
                )
        return None

    def _next_change_point(
        self,
        schedule: list[dict[str, Any]],
        *,
        key: str,
        start_at: datetime,
        slot_minutes: int,
    ) -> tuple[datetime, dict[str, Any]] | None:
        """Return when a schedule key next changes, and the full point at that time."""
        if not schedule:
            return None
        current = schedule[0].get(key)
        for index, point in enumerate(schedule[1:], start=1):
            if point.get(key) != current:
                return (
                    start_at + timedelta(minutes=index * slot_minutes),
                    point,
                )
        return None

    def _map_charge_source(self, charge_source: int) -> str:
        """Map charge source bitmask to readable label."""
        if charge_source == 1:
            return "g"
        if charge_source == 2:
            return "p"
        if charge_source == 3:
            return "gp"
        return "n"

    def _map_action_code(self, action: str) -> str:
        """Map verbose battery actions to compact graph payload codes."""
        return {
            "charge": "c",
            "discharge": "d",
            "hold": "h",
        }.get(action, "h")

    def _append_total_timing(self, *, timings: list[TimingEntry], total_ms: int) -> None:
        """Append the final total timing entry to one timing list."""
        while (
            timings
            and isinstance(timings[-1], tuple | list)
            and len(timings[-1]) == 2
            and timings[-1][0] == "total"
        ):
            timings.pop()
        timings.append(("total", int(total_ms)))

    def _planner_output_from_result(
        self,
        request: dict[str, Any],
        result: dict[str, Any],
        *,
        timings: list[TimingEntry],
    ) -> dict[str, Any]:
        """Map poweroptim output to coordinator planner output shape."""
        start_at = request["window"].start_at
        slot_minutes = int(request["slot_minutes"])
        horizon_slots = len(request["optimizer_params"]["grid_import_price_per_kwh"])

        batteries: dict[str, dict[str, Any]] = {}
        comforts: dict[str, dict[str, Any]] = {}
        optionals: dict[str, dict[str, Any]] = {}
        battery_charge_source: dict[str, str] = {}

        name_maps = request["name_to_subentry"]
        for entity in result.get("entities", []):
            entity_name = str(entity.get("name"))
            entity_type = str(entity.get("type"))
            schedule = list(entity.get("schedule", []))
            if not schedule:
                continue

            if entity_type == "battery":
                subentry_id = name_maps["batteries"].get(entity_name)
                if subentry_id is None:
                    continue
                current = schedule[0]
                current_source = self._map_charge_source(int(current.get("charge_source", 0)))
                battery_charge_source[subentry_id] = current_source
                next_change = self._next_change_point(
                    schedule,
                    key="state",
                    start_at=start_at,
                    slot_minutes=slot_minutes,
                )
                next_action_timestamp = next_change[0] if next_change is not None else None
                next_point = next_change[1] if next_change is not None else None
                next_action = (
                    str(next_point.get("state", "hold"))
                    if isinstance(next_point, dict)
                    else None
                )
                next_action_source = (
                    self._map_charge_source(int(next_point.get("charge_source", 0)))
                    if isinstance(next_point, dict)
                    and str(next_point.get("state", "hold")) == "charge"
                    else None
                )
                batteries[subentry_id] = {
                    "action": str(current.get("state", "hold")),
                    "charge_source": current_source,
                    "next_action_timestamp": (
                        next_action_timestamp.isoformat()
                        if next_action_timestamp is not None
                        else None
                    ),
                    "next_action": next_action,
                    "next_charge_source": next_action_source,
                }
                continue

            if entity_type == "comfort":
                subentry_id = name_maps["comforts"].get(entity_name)
                if subentry_id is None:
                    continue
                current = schedule[0]
                next_change = self._next_change_point(
                    schedule,
                    key="enabled",
                    start_at=start_at,
                    slot_minutes=slot_minutes,
                )
                next_action_timestamp = next_change[0] if next_change is not None else None
                next_point = next_change[1] if next_change is not None else None
                comforts[subentry_id] = {
                    "action": "on" if bool(current.get("enabled")) else "off",
                    "next_action_timestamp": (
                        next_action_timestamp.isoformat()
                        if next_action_timestamp is not None
                        else None
                    ),
                    "next_action": (
                        "on" if bool(next_point.get("enabled")) else "off"
                        if isinstance(next_point, dict)
                        else None
                    ),
                }

        for optional in result.get("optional_entity_options", []):
            entity_name = str(optional.get("name"))
            subentry_id = name_maps["optionals"].get(entity_name)
            if subentry_id is None:
                continue
            options = list(optional.get("options", []))
            optional_diag: dict[str, Any] = {}
            if options:
                first_start_slot = int(options[0]["start_timeslot"])
                first_end_slot = int(options[0]["end_timeslot"])
                optional_diag["next_start_option"] = (
                    start_at
                    + timedelta(minutes=first_start_slot * slot_minutes)
                ).isoformat()
                optional_diag["next_end_option"] = (
                    start_at
                    + timedelta(minutes=first_end_slot * slot_minutes)
                ).isoformat()
            for index, option in enumerate(options, start=1):
                start_slot = int(option["start_timeslot"])
                end_slot = int(option["end_timeslot"])
                optional_diag[f"option_{index}_start"] = (
                    start_at + timedelta(minutes=start_slot * slot_minutes)
                ).isoformat()
                optional_diag[f"option_{index}_end"] = (
                    start_at + timedelta(minutes=end_slot * slot_minutes)
                ).isoformat()
            optionals[subentry_id] = optional_diag

        is_suboptimal = bool(result.get("suboptimal", False))
        reasons = list(result.get("suboptimal_reasons", []))
        status = "degraded" if is_suboptimal else "ok"
        message = (
            f"Plan solved with suboptimal constraints: {', '.join(reasons)}"
            if is_suboptimal
            else "Plan solved"
        )
        return {
            "status": status,
            "message": message,
            "battery_charge_source": battery_charge_source,
            "diagnostics": {
                "batteries": batteries,
                "comforts": comforts,
                "optionals": optionals,
                "sources": {
                    "usage_forecast": request.get("usage_forecast_points"),
                },
                "optimizer": {
                    "execution_time_s": result.get("execution_time"),
                    "fitness": result.get("fitness"),
                    "avg_price": result.get("avg_price"),
                    "projections": result.get("projections"),
                    "suboptimal": is_suboptimal,
                    "suboptimal_reasons": reasons,
                    "problems": result.get("problems", []),
                    "successful_solves": result.get("successful_solves"),
                    "reused_steps": result.get("reused_steps"),
                    "span_start": start_at.isoformat(),
                    "span_end": (
                        start_at + timedelta(minutes=horizon_slots * slot_minutes)
                    ).isoformat(),
                },
                **self._build_enabled_plan_details(request, result, timings=timings),
            },
        }

    def _build_enabled_plan_details(
        self,
        request: dict[str, Any],
        result: dict[str, Any],
        *,
        timings: list[TimingEntry],
    ) -> dict[str, Any]:
        """Build only the plan detail payloads that are currently enabled."""
        diagnostics: dict[str, Any] = {}
        raw_details: dict[str, Any] | None = None
        timing_entries = timings

        if self._plan_details_enabled("plan_details"):
            started_at = time.monotonic()
            raw_details = self._build_plan_details_payload(request, result)
            timing_entries.append(("Plan details payload build", _duration_ms(started_at)))
            diagnostics["plan_details"] = raw_details

        if self._plan_details_enabled("plan_details_hourly"):
            if raw_details is None:
                started_at = time.monotonic()
                raw_details = self._build_plan_details_payload(request, result)
                timing_entries.append(
                    ("Plan details payload build", _duration_ms(started_at))
                )
            diagnostics["plan_details_hourly"] = self._aggregate_plan_details(
                raw_details,
                target_slot_minutes=60,
            )

        return diagnostics

    def _plan_details_enabled(self, details_key: str) -> bool:
        """Return whether one disabled-by-default details entity is enabled."""
        entity_registry = er.async_get(self.hass)
        unique_id = f"{self._entry_id}:entry:{details_key}"
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            return False
        if (entry := entity_registry.async_get(entity_id)) is None:
            return False
        return entry.disabled_by is None

    def _build_plan_details_payload(
        self, request: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        """Build a graph-friendly, flat plan payload for the current horizon."""
        optimizer_params = request["optimizer_params"]
        horizon_slots = len(optimizer_params["grid_import_price_per_kwh"])
        plan_details: dict[str, Any] = {
            "start_at": request["window"].start_at.isoformat(),
            "slot_minutes": int(request["slot_minutes"]),
            "slots": horizon_slots,
            "grid_import_price_per_kwh": self._rounded_series(
                optimizer_params["grid_import_price_per_kwh"]
            ),
            "grid_export_price_per_kwh": self._rounded_series(
                optimizer_params["grid_export_price_per_kwh"]
            ),
            "usage_kwh": self._rounded_series(optimizer_params["usage_kwh"]),
            "solar_input_kwh": self._rounded_series(
                optimizer_params["solar_input_kwh"]
            ),
        }

        projections = result.get("projections")
        if isinstance(projections, dict):
            per_slot = projections.get("per_slot")
            if isinstance(per_slot, list) and len(per_slot) == horizon_slots:
                plan_details["projected_cost"] = self._projection_series(
                    per_slot, "projected_cost"
                )
                plan_details["projected_savings_cost"] = self._projection_series(
                    per_slot, "projected_savings_cost"
                )
                plan_details["projected_savings_pct"] = self._projection_series(
                    per_slot, "projected_savings_pct"
                )

        for entity in result.get("entities", []):
            entity_name = str(entity.get("name"))
            entity_type = str(entity.get("type"))
            schedule = list(entity.get("schedule", []))
            if entity_type == "battery":
                key_base = f"battery_{slugify(entity_name) or 'asset'}"
                plan_details[f"{key_base}_action"] = [
                    self._map_action_code(str(action))
                    for action in self._series_from_schedule(
                        schedule, horizon_slots, "state", default="hold", stringify=True
                    )
                ]
                plan_details[f"{key_base}_level_kwh"] = self._rounded_series(
                    self._series_from_schedule(
                        schedule, horizon_slots, "level", default=0.0
                    )
                )
                plan_details[f"{key_base}_charge_source"] = [
                    self._map_charge_source(int(value))
                    for value in self._series_from_schedule(
                        schedule, horizon_slots, "charge_source", default=0.0
                    )
                ]
                continue

            if entity_type == "comfort":
                key_base = f"comfort_{slugify(entity_name) or 'asset'}"
                plan_details[f"{key_base}_enabled"] = [
                    bool(value)
                    for value in self._series_from_schedule(
                        schedule, horizon_slots, "enabled", default=False
                    )
                ]

        for optional in result.get("optional_entity_options", []):
            entity_name = str(optional.get("name"))
            key = f"optional_{slugify(entity_name) or 'asset'}_enabled"
            values = [False] * horizon_slots
            for option in list(optional.get("options", [])):
                try:
                    start_slot = int(option["start_timeslot"])
                    end_slot = int(option["end_timeslot"])
                except (KeyError, TypeError, ValueError):
                    continue
                bounded_start = max(0, min(horizon_slots, start_slot))
                bounded_end = max(bounded_start, min(horizon_slots, end_slot))
                for index in range(bounded_start, bounded_end):
                    values[index] = True
            plan_details[key] = values

        return plan_details

    def _aggregate_plan_details(
        self,
        plan_details: dict[str, Any],
        *,
        target_slot_minutes: int,
    ) -> dict[str, Any]:
        """Return a second plan details payload aggregated to another cadence.

        We keep the same attribute names so consumers can switch entities
        without rewriting attribute access. Numeric values are per-slot
        absolutes like kWh or currency, so they aggregate by summing buckets.
        Percentages are recomputed after their base series are aggregated.
        """
        slot_minutes = int(plan_details.get("slot_minutes", 0))
        if (
            slot_minutes <= 0
            or slot_minutes >= target_slot_minutes
            or target_slot_minutes % slot_minutes != 0
        ):
            return dict(plan_details)

        slots_per_bucket = target_slot_minutes // slot_minutes
        aggregated: dict[str, Any] = {
            "start_at": plan_details["start_at"],
            "slot_minutes": target_slot_minutes,
            "slots": (int(plan_details["slots"]) + slots_per_bucket - 1)
            // slots_per_bucket,
        }

        for key, value in plan_details.items():
            if key in {"start_at", "slot_minutes", "slots"}:
                continue
            if not isinstance(value, list):
                aggregated[key] = value
                continue
            aggregated[key] = self._aggregate_plan_details_series(
                key, value, slots_per_bucket
            )

        if (
            isinstance(aggregated.get("projected_cost"), list)
            and isinstance(aggregated.get("projected_savings_cost"), list)
            and len(aggregated["projected_cost"])
            == len(aggregated["projected_savings_cost"])
        ):
            aggregated["projected_savings_pct"] = (
                self._recompute_aggregated_percentages(
                    aggregated["projected_cost"],
                    aggregated["projected_savings_cost"],
                )
            )

        return aggregated

    def _aggregate_plan_details_series(
        self, key: str, values: list[Any], slots_per_bucket: int
    ) -> list[Any]:
        """Aggregate one plan details series across fixed-size buckets."""
        aggregated: list[Any] = []
        for start in range(0, len(values), slots_per_bucket):
            chunk = values[start : start + slots_per_bucket]
            if not chunk:
                continue
            first = chunk[0]
            if isinstance(first, bool):
                aggregated.append(any(bool(value) for value in chunk))
            elif isinstance(first, (int, float)) and not isinstance(first, bool):
                if key.endswith("_pct"):
                    aggregated.append(0.0)
                elif key in {
                    "grid_import_price_per_kwh",
                    "grid_export_price_per_kwh",
                }:
                    aggregated.append(
                        round(
                            sum(float(value) for value in chunk) / len(chunk),
                            4,
                        )
                    )
                elif key.endswith("_level_kwh"):
                    aggregated.append(
                        round(
                            sum(float(value) for value in chunk) / len(chunk),
                            2,
                        )
                    )
                else:
                    aggregated.append(round(sum(float(value) for value in chunk), 2))
            elif key.endswith("_charge_source"):
                sources = {str(value) for value in chunk if isinstance(value, str)}
                active_sources = sorted(source for source in sources if source != "n")
                aggregated.append("".join(active_sources) if active_sources else "n")
            elif key.endswith("_action"):
                actions = {str(value) for value in chunk if isinstance(value, str)}
                active_actions = sorted(action for action in actions if action != "h")
                aggregated.append("".join(active_actions) if active_actions else "h")
            else:
                aggregated.append(chunk[-1])
        return aggregated

    def _recompute_aggregated_percentages(
        self,
        costs: list[Any],
        savings: list[Any],
    ) -> list[float]:
        """Recompute percentages from aggregated absolute cost series."""
        percentages: list[float] = []
        for cost, saving in zip(costs, savings, strict=False):
            try:
                cost_value = float(cost)
                saving_value = float(saving)
            except (TypeError, ValueError):
                percentages.append(0.0)
                continue
            baseline_value = cost_value + saving_value
            if baseline_value == 0:
                percentages.append(0.0)
                continue
            percentages.append(round((saving_value / baseline_value) * 100.0, 2))
        return percentages

    def _projection_series(
        self, per_slot: list[Any], key: str
    ) -> list[float]:
        """Return one flat numeric projection series from poweroptim output."""
        values: list[float] = []
        for slot in per_slot:
            if not isinstance(slot, dict):
                values.append(0.0)
                continue
            try:
                values.append(round(float(slot[key]), 2))
            except (KeyError, TypeError, ValueError):
                values.append(0.0)
        return values

    def _rounded_series(self, values: list[Any]) -> list[float]:
        """Return a numeric series rounded to at most two decimals."""
        rounded: list[float] = []
        for value in values:
            try:
                rounded.append(round(float(value), 2))
            except (TypeError, ValueError):
                rounded.append(0.0)
        return rounded

    def _series_from_schedule(
        self,
        schedule: list[dict[str, Any]],
        horizon_slots: int,
        key: str,
        *,
        default: Any,
        stringify: bool = False,
    ) -> list[Any]:
        """Return one horizon-length series from an optimizer schedule."""
        values: list[Any] = []
        for index in range(horizon_slots):
            slot = schedule[index] if index < len(schedule) else None
            if not isinstance(slot, dict):
                values.append(default)
                continue
            value = slot.get(key, default)
            values.append(str(value) if stringify else value)
        return values

    def _project_snapshot(self, planner_output: dict[str, Any]) -> CoordinatorSnapshot:
        """Project planner output to immutable coordinator snapshot."""
        return CoordinatorSnapshot(
            created_at=datetime.now(tz=UTC),
            planner_status=str(planner_output.get("status", "unknown")),
            planner_message=(
                str(planner_output["message"])
                if planner_output.get("message") is not None
                else None
            ),
            battery_charge_source=planner_output.get("battery_charge_source"),
            diagnostics=planner_output.get("diagnostics"),
        )

    def _mark_locked(self, stage: Stage, trigger: CycleTrigger) -> None:
        """Mark stage as skipped because it is already running."""
        state = self._stage_state(stage)
        state.has_error = True
        state.kind = StageErrorKind.LOCKED
        state.message = "Skipped because another run is in progress"
        state.at = datetime.now(tz=UTC)
        state.details = {"trigger": str(trigger)}
        state.consecutive_failures += 1
        state.skipped_locked_count += 1
        _LOGGER.warning(
            "%s stage skipped because lock is held (entry_id=%s, trigger=%s)",
            stage,
            self._entry_id,
            trigger,
        )

    def _set_stage_error(
        self,
        stage: Stage,
        kind: StageErrorKind,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Set stage error details."""
        state = self._stage_state(stage)
        state.has_error = True
        state.kind = kind
        state.message = message
        state.at = datetime.now(tz=UTC)
        state.details = details
        state.consecutive_failures += 1

    def _clear_stage_error(self, stage: Stage) -> None:
        """Clear stage error details after a successful run."""
        state = self._stage_state(stage)
        state.has_error = False
        state.kind = None
        state.message = None
        state.at = None
        state.details = None
        state.consecutive_failures = 0

    def _stage_state(self, stage: Stage) -> StageErrorState:
        """Return mutable stage state for a coordinator stage."""
        if stage is Stage.PLAN:
            return self._plan_error
        return self._emit_error

    def _classify_plan_error(self, err: Exception) -> StageErrorKind:
        """Map planning exceptions to a stable error kind."""
        if isinstance(err, ServiceValidationError):
            return StageErrorKind.PLANNER_INPUT
        return StageErrorKind.PLANNER_EXECUTION

    def _classify_emit_error(self, err: Exception) -> StageErrorKind:
        """Map emission exceptions to a stable error kind."""
        if isinstance(err, ServiceValidationError):
            return StageErrorKind.EMIT_NO_SNAPSHOT
        if isinstance(err, RuntimeError):
            return StageErrorKind.EMIT_NO_SNAPSHOT
        return StageErrorKind.EMIT_PROJECTION

    @callback
    def _async_start_heartbeat(self) -> None:
        """Schedule a lenient heartbeat to refresh listener availability."""
        if self.update_interval is None:
            return
        if self._heartbeat_start_unsub or self._heartbeat_interval_unsub:
            return

        # Delay heartbeat so normal coordinator updates have time to run first.
        # This heartbeat exists to keep entity availability accurate when
        # scheduled updates stall, not to duplicate normal state writes.
        first_heartbeat = datetime.now(tz=UTC) + self.update_interval + HEARTBEAT_OFFSET
        self._heartbeat_start_unsub = async_track_point_in_utc_time(
            self.hass, self._async_handle_first_heartbeat, first_heartbeat
        )

    @callback
    def _async_stop_heartbeat(self) -> None:
        """Stop heartbeat callbacks."""
        if self._heartbeat_start_unsub is not None:
            self._heartbeat_start_unsub()
            self._heartbeat_start_unsub = None
        if self._heartbeat_interval_unsub is not None:
            self._heartbeat_interval_unsub()
            self._heartbeat_interval_unsub = None
        self._last_heartbeat_stale = None

    @callback
    def _async_handle_first_heartbeat(self, _now: datetime) -> None:
        """Start periodic heartbeats after the delayed first heartbeat."""
        self._heartbeat_start_unsub = None
        self._async_heartbeat(_now)
        if self.update_interval is None:
            return
        self._heartbeat_interval_unsub = async_track_time_interval(
            self.hass, self._async_heartbeat, self.update_interval
        )

    @callback
    def _async_heartbeat(self, _now: datetime) -> None:
        """Push listener updates only when staleness state changes."""
        if not self.scheduler_enabled:
            return

        # Only notify entities when stale status flips so `available` can move
        # between available/unavailable without forcing duplicate writes each
        # interval when the coordinator is healthy.
        is_stale = self.is_stale
        if self._last_heartbeat_stale is None:
            self._last_heartbeat_stale = is_stale
            if is_stale:
                self.async_update_listeners()
            return

        if is_stale == self._last_heartbeat_stale:
            return

        self._last_heartbeat_stale = is_stale
        self.async_update_listeners()

    def error_attributes(self) -> dict[str, Any]:
        """Return diagnostic state for error entities."""
        plan_details = self._plan_error.details or {}
        emit_details = self._emit_error.details or {}
        return {
            "has_error": self.has_error,
            "last_attempt_at": self._last_attempt_at,
            "last_success_at": self._last_success_at,
            "expires_at": self.expires_at,
            "last_duration_ms": self._last_duration_ms,
            "plan_error_kind": self._plan_error.kind,
            "plan_error_message": self._plan_error.message,
            "plan_error_at": self._plan_error.at,
            "plan_error_source": plan_details.get("source"),
            "plan_error_available_count": plan_details.get("available_count"),
            "plan_error_required_count": plan_details.get("required_count"),
            "source_issues": self._source_status.source_health_diagnostics(),
            "plan_error_failures": self._plan_error.consecutive_failures,
            "plan_skipped_locked_count": self._plan_error.skipped_locked_count,
            "emit_error_kind": self._emit_error.kind,
            "emit_error_message": self._emit_error.message,
            "emit_error_at": self._emit_error.at,
            "emit_error_source": emit_details.get("source"),
            "emit_error_failures": self._emit_error.consecutive_failures,
            "emit_skipped_locked_count": self._emit_error.skipped_locked_count,
            "is_stale": self.is_stale,
            "planning_enabled": self._planning_enabled,
            "action_emission_enabled": self._action_emission_enabled,
        }

    def restore_payload(self) -> dict[str, Any] | None:
        """Return serialized coordinator state suitable for restore."""
        if self._snapshot is None:
            return None
        return {
            "schema_id": self._snapshot_schema_id,
            "snapshot": self._snapshot.to_dict(),
            "last_success_at": (
                self._last_success_at.isoformat()
                if self._last_success_at is not None
                else None
            ),
            "last_duration_ms": self._last_duration_ms,
            "last_run_timings": self._serialize_timings(self._last_run_timings),
        }

    @callback
    def async_restore_payload(self, payload: dict[str, Any]) -> bool:
        """Restore coordinator state from serialized payload."""
        if payload.get("schema_id") != self._snapshot_schema_id:
            _LOGGER.debug(
                "Discarding cached snapshot with mismatched schema "
                "(entry_id=%s, cached=%s, current=%s)",
                self._entry_id,
                payload.get("schema_id"),
                self._snapshot_schema_id,
            )
            return False
        snapshot_payload = payload.get("snapshot")
        if not isinstance(snapshot_payload, dict):
            return False

        snapshot = CoordinatorSnapshot.from_dict(snapshot_payload)
        if snapshot is None:
            return False

        self._snapshot = snapshot
        self.data = self._snapshot
        self._last_success_at = parse_snapshot_datetime(payload.get("last_success_at"))
        self._last_duration_ms = (
            int(payload["last_duration_ms"])
            if payload.get("last_duration_ms") is not None
            else None
        )
        self._last_run_timings = self._deserialize_timings(payload.get("last_run_timings"))
        self._source_status.apply_restored_snapshot(snapshot)
        # Keep restored entities available until the next scheduled cycle updates
        # the real execution timestamps.
        self._last_attempt_at = datetime.now(tz=UTC)
        self.async_update_listeners()
        return True

    def _serialize_timings(
        self, timings: list[TimingEntry] | None
    ) -> list[list[str | int]] | None:
        """Serialize timing entries for storage."""
        if timings is None:
            return None
        return [[str(label), int(duration_ms)] for label, duration_ms in timings]

    def _deserialize_timings(self, payload: Any) -> list[TimingEntry] | None:
        """Deserialize stored timing entries."""
        if not isinstance(payload, list):
            return None
        timings: list[TimingEntry] = []
        for entry in payload:
            if not isinstance(entry, list | tuple) or len(entry) != 2:
                continue
            label, duration_ms = entry
            try:
                timings.append((str(label), int(duration_ms)))
            except (TypeError, ValueError):
                continue
        return timings or None

    async def async_restore_snapshot(self) -> bool:
        """Restore cached snapshot from storage for this config entry."""
        if (payload := await self._snapshot_store.async_load()) is None:
            return False
        if not isinstance(payload, dict):
            return False
        restored = self.async_restore_payload(payload)
        if restored:
            _LOGGER.debug("Restored cached snapshot for entry_id=%s", self._entry_id)
        else:
            await self._snapshot_store.async_remove()
            _LOGGER.debug("Discarded invalid cached snapshot for entry_id=%s", self._entry_id)
        return restored

    async def async_persist_snapshot(self) -> None:
        """Persist current snapshot cache for this config entry."""
        if (entry_payload := self.restore_payload()) is None:
            return
        await self._snapshot_store.async_save(entry_payload)
