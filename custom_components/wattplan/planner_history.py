"""Append-only, local-day optimizer reproductions outside Home Assistant Recorder."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def reproduction_directory(hass: HomeAssistant, entry_id: str) -> Path:
    """Return the directory holding one entry's reproduction files."""
    return Path(hass.config.path(f"{DOMAIN}_reproductions", entry_id))


class PlannerHistory:
    """Store complete optimizer runs in one JSONL file per local calendar day."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        enabled: bool,
        retention_days: int,
        integration_version: str,
    ) -> None:
        self._hass = hass
        self._directory = reproduction_directory(hass, entry_id)
        self._timezone = ZoneInfo(hass.config.time_zone)
        self._enabled = enabled
        self._retention_days = retention_days
        self._integration_version = integration_version
        self._lock = asyncio.Lock()
        self._entry_id = entry_id
        self._currency = getattr(hass.config, "currency", None)
        self._actuals_provider: (
            Callable[[datetime, datetime], list[dict[str, Any]]] | None
        ) = None

    def set_actuals_provider(
        self, provider: Callable[[datetime, datetime], list[dict[str, Any]]]
    ) -> None:
        """Attach the existing history store; no Recorder queries or backfill."""
        self._actuals_provider = provider

    def _metadata(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "setup_id": self._entry_id,
            "timezone": str(self._timezone),
            "currency": self._currency,
            "integration_version": self._integration_version,
        }

    def _actuals(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self._actuals_provider(start, end) if self._actuals_provider else []

    async def async_record_actuals(
        self, completed_at: datetime, actuals: list[dict[str, Any]]
    ) -> None:
        """Append independently of planning, partitioned by interval local date."""
        if not self._enabled or not actuals:
            return
        try:
            by_day: dict[date, list[dict[str, Any]]] = {}
            for interval in actuals:
                day = (
                    datetime.fromisoformat(interval["start"])
                    .astimezone(self._timezone)
                    .date()
                )
                by_day.setdefault(day, []).append(interval)
            async with self._lock:
                for day, intervals in by_day.items():
                    await self._hass.async_add_executor_job(
                        self._append_and_prune,
                        day,
                        {
                            **self._metadata(),
                            "record_type": "actuals",
                            "created_at": completed_at.isoformat(),
                            "actuals": intervals,
                        },
                    )
        except Exception:
            _LOGGER.exception("Could not record WattPlan completed actuals")

    async def async_record(
        self, created_at: datetime, request: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Record an accepted plan without letting disk failures break planning."""
        if not self._enabled:
            return
        try:
            window = request["window"]
            captured = request.get("_input_captured_at", created_at.isoformat())
            captured_at = datetime.fromisoformat(captured)
            record = {
                **self._metadata(),
                "record_type": "plan",
                "created_at": created_at.isoformat(),
                "input_captured_at": captured,
                "actuals_status": "tracking_enabled"
                if self._actuals_provider
                else "tracking_disabled",
                "actuals": self._actuals(
                    captured_at - timedelta(minutes=2 * request["slot_minutes"]),
                    captured_at,
                ),
                "request": {
                    "window": {
                        "start_at": window.start_at.isoformat(),
                        "end_at": (
                            window.start_at
                            + timedelta(minutes=window.slot_minutes * window.slots)
                        ).isoformat(),
                    },
                    "slot_minutes": request["slot_minutes"],
                    "optimizer_params": request["_recorded_optimizer_params"],
                },
                "result": result,
            }
            async with self._lock:
                await self._hass.async_add_executor_job(
                    self._append_and_prune,
                    created_at.astimezone(self._timezone).date(),
                    record,
                )
        except Exception:
            _LOGGER.exception("Could not record WattPlan planner reproduction")

    async def async_read_date(self, local_date: date) -> dict[str, Any]:
        """Return one complete local-day file and available date boundaries."""
        async with self._lock:
            response = await self._hass.async_add_executor_job(
                self._read_date, local_date
            )
        # Enrich old archives in the response only. Work from an event-loop copy
        # of the store, preserving forecast inputs and the original disk bytes.
        start = datetime.combine(local_date, time.min, self._timezone).astimezone(UTC)
        end = datetime.combine(
            local_date + timedelta(days=1), time.min, self._timezone
        ).astimezone(UTC)
        actuals = self._actuals(start, end)
        return await self._hass.async_add_executor_job(
            self._enrich_export, response, actuals
        )

    def _enrich_export(
        self, response: dict[str, Any], actuals: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Parse potentially large archives outside the HA event loop."""
        if actuals:
            known = {
                item["start"]
                for line in response["data"].splitlines()
                for item in json.loads(line).get("actuals", [])
            }
            missing = [item for item in actuals if item["start"] not in known]
            if missing:
                response["data"] += (
                    json.dumps(
                        {
                            **self._metadata(),
                            "record_type": "actuals",
                            "actuals": missing,
                            "provenance": "retained_history_export",
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    + "\n"
                )
        response["actuals_status"] = (
            "tracking_enabled" if self._actuals_provider else "tracking_disabled"
        )
        return response

    def _day_path(self, local_date: date) -> Path:
        return self._directory / f"{local_date.isoformat()}.jsonl"

    def _available_dates(self) -> list[date]:
        if not self._directory.is_dir():
            return []
        dates = []
        for path in self._directory.glob("*.jsonl"):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if path.name == f"{day.isoformat()}.jsonl" and path.is_file():
                dates.append(day)
        return sorted(dates)

    def _append_and_prune(self, local_date: date, record: dict[str, Any]) -> None:
        line = (
            json.dumps(
                record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._day_path(local_date)
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.seek(0)
                    complete_bytes = stream.read().rfind(b"\n") + 1
                    stream.truncate(complete_bytes)
                    stream.seek(0, os.SEEK_END)
                    _LOGGER.warning(
                        "Removed incomplete trailing planner record from %s", path
                    )
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

        # Completion can belong to yesterday; do not move the retention clock
        # backwards to the interval's date (especially for a batch of gaps).
        recorded_date = (
            datetime.fromisoformat(record["created_at"])
            .astimezone(self._timezone)
            .date()
        )
        oldest_retained = recorded_date - timedelta(days=self._retention_days - 1)
        for day in self._available_dates():
            if day < oldest_retained:
                self._day_path(day).unlink()

    def _read_date(self, local_date: date) -> dict[str, Any]:
        dates = self._available_dates()
        path = self._day_path(local_date)
        contents = path.read_bytes() if path.is_file() else b""
        incomplete_tail = bool(contents and not contents.endswith(b"\n"))
        if incomplete_tail:
            contents = contents[: contents.rfind(b"\n") + 1]
        return {
            "date": local_date.isoformat(),
            "data": contents.decode("utf-8"),
            "min_available_date": dates[0].isoformat() if dates else None,
            "max_available_date": dates[-1].isoformat() if dates else None,
            "incomplete_tail": incomplete_tail,
        }
