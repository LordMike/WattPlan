"""Snapshot persistence helpers for the coordinator."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.storage import Store

from ..coordinator_parts import CoordinatorSnapshot, TimingEntry, parse_snapshot_datetime


@dataclass(slots=True)
class RestoredCoordinatorState:
    """Restored coordinator state from persisted snapshot storage."""

    snapshot: CoordinatorSnapshot
    last_success_at: Any
    last_duration_ms: int | None
    last_run_timings: list[TimingEntry] | None


class CoordinatorSnapshotStore:
    """Persist and restore coordinator snapshots and timing metadata."""

    def __init__(
        self,
        store: Store[dict[str, Any]],
        *,
        entry_id: str,
        schema_id: str,
        logger: logging.Logger,
    ) -> None:
        self._store = store
        self._entry_id = entry_id
        self._schema_id = schema_id
        self._logger = logger

    def restore_payload(
        self,
        *,
        snapshot: CoordinatorSnapshot | None,
        last_success_at: Any,
        last_duration_ms: int | None,
        last_run_timings: list[TimingEntry] | None,
    ) -> dict[str, Any] | None:
        """Return serialized coordinator state suitable for restore."""
        if snapshot is None:
            return None
        return {
            "schema_id": self._schema_id,
            "snapshot": snapshot.to_dict(),
            "last_success_at": (
                last_success_at.isoformat() if last_success_at is not None else None
            ),
            "last_duration_ms": last_duration_ms,
            "last_run_timings": self._serialize_timings(last_run_timings),
        }

    @callback
    def async_restore_payload(
        self, payload: dict[str, Any]
    ) -> RestoredCoordinatorState | None:
        """Parse restored coordinator state from serialized payload."""
        if payload.get("schema_id") != self._schema_id:
            self._logger.debug(
                "Discarding cached snapshot with mismatched schema "
                "(entry_id=%s, cached=%s, current=%s)",
                self._entry_id,
                payload.get("schema_id"),
                self._schema_id,
            )
            return None

        snapshot_payload = payload.get("snapshot")
        if not isinstance(snapshot_payload, dict):
            return None

        snapshot = CoordinatorSnapshot.from_dict(snapshot_payload)
        if snapshot is None:
            return None

        last_duration_ms = (
            int(payload["last_duration_ms"])
            if payload.get("last_duration_ms") is not None
            else None
        )
        return RestoredCoordinatorState(
            snapshot=snapshot,
            last_success_at=parse_snapshot_datetime(payload.get("last_success_at")),
            last_duration_ms=last_duration_ms,
            last_run_timings=self._deserialize_timings(payload.get("last_run_timings")),
        )

    async def async_restore_snapshot(self) -> RestoredCoordinatorState | None:
        """Restore cached snapshot from storage for this config entry."""
        if (payload := await self._store.async_load()) is None:
            return None
        if not isinstance(payload, dict):
            return None

        restored = self.async_restore_payload(payload)
        if restored is not None:
            self._logger.debug("Restored cached snapshot for entry_id=%s", self._entry_id)
            return restored

        await self._store.async_remove()
        self._logger.debug("Discarded invalid cached snapshot for entry_id=%s", self._entry_id)
        return None

    async def async_persist_snapshot(
        self,
        *,
        snapshot: CoordinatorSnapshot | None,
        last_success_at: Any,
        last_duration_ms: int | None,
        last_run_timings: list[TimingEntry] | None,
    ) -> None:
        """Persist current snapshot cache for this config entry."""
        if (
            entry_payload := self.restore_payload(
                snapshot=snapshot,
                last_success_at=last_success_at,
                last_duration_ms=last_duration_ms,
                last_run_timings=last_run_timings,
            )
        ) is None:
            return
        await self._store.async_save(entry_payload)

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
