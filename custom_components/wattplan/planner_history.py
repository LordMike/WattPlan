"""Append-only, local-day optimizer reproductions outside Home Assistant Recorder."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from typing import Any
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

    async def async_record(
        self, created_at: datetime, request: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Record an accepted plan without letting disk failures break planning."""
        if not self._enabled:
            return
        try:
            window = request["window"]
            record = {
                "schema_version": 1,
                "integration_version": self._integration_version,
                "created_at": created_at.isoformat(),
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
            return await self._hass.async_add_executor_job(self._read_date, local_date)

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
        line = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
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
                    _LOGGER.warning("Removed incomplete trailing planner record from %s", path)
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

        oldest_retained = local_date - timedelta(days=self._retention_days - 1)
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
