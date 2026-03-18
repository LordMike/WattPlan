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
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

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
    snapshot_schema_id,
)
from .coordinator_logic import (
    CoordinatorSnapshotStore,
    PlannerProjectionBuilder,
    PlanningRequestBuilder,
    SourceStatusManager,
)
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
        self._persistence = CoordinatorSnapshotStore(
            self._snapshot_store,
            entry_id=entry_id,
            schema_id=self._snapshot_schema_id,
            logger=_LOGGER,
        )
        self._projection = PlannerProjectionBuilder(hass, entry_id=entry_id)
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
        return self._projection.planner_output_from_result(
            request,
            result,
            timings=timings,
        )

    def _project_snapshot(self, planner_output: dict[str, Any]) -> CoordinatorSnapshot:
        """Project planner output to immutable coordinator snapshot."""
        return self._projection.project_snapshot(planner_output)

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
        return self._persistence.restore_payload(
            snapshot=self._snapshot,
            last_success_at=self._last_success_at,
            last_duration_ms=self._last_duration_ms,
            last_run_timings=self._last_run_timings,
        )

    @callback
    def async_restore_payload(self, payload: dict[str, Any]) -> bool:
        """Restore coordinator state from serialized payload."""
        restored = self._persistence.async_restore_payload(payload)
        if restored is None:
            return False

        self._snapshot = restored.snapshot
        self.data = self._snapshot
        self._last_success_at = restored.last_success_at
        self._last_duration_ms = restored.last_duration_ms
        self._last_run_timings = restored.last_run_timings
        self._source_status.apply_restored_snapshot(restored.snapshot)
        # Keep restored entities available until the next scheduled cycle updates
        # the real execution timestamps.
        self._last_attempt_at = datetime.now(tz=UTC)
        self.async_update_listeners()
        return True

    async def async_restore_snapshot(self) -> bool:
        """Restore cached snapshot from storage for this config entry."""
        if (restored := await self._persistence.async_restore_snapshot()) is None:
            return False
        self._snapshot = restored.snapshot
        self.data = self._snapshot
        self._last_success_at = restored.last_success_at
        self._last_duration_ms = restored.last_duration_ms
        self._last_run_timings = restored.last_run_timings
        self._source_status.apply_restored_snapshot(restored.snapshot)
        self._last_attempt_at = datetime.now(tz=UTC)
        self.async_update_listeners()
        return True

    async def async_persist_snapshot(self) -> None:
        """Persist current snapshot cache for this config entry."""
        await self._persistence.async_persist_snapshot(
            snapshot=self._snapshot,
            last_success_at=self._last_success_at,
            last_duration_ms=self._last_duration_ms,
            last_run_timings=self._last_run_timings,
        )
