"""Home Assistant Store wrapper for historical cost tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from .models import (
    DAY_ARRAY_KEYS,
    FLAG_GAP,
    FLAG_MISSING_EXPORT_PRICE,
    FLAG_MISSING_IMPORT_PRICE,
    FLAG_MISSING_METER,
    FLAG_METER_RESET,
    FLAG_SELF_CONSUMPTION_UNAVAILABLE,
    HistoricalMetric,
    PERIOD_THIS_MONTH,
    PERIOD_TODAY,
    RETENTION_DAYS,
    SAVE_DELAY_SECONDS,
    SCENARIO_ACTUAL,
    SCENARIO_NO_BATTERY,
    SCENARIO_SELF_CONSUMPTION,
    STORE_VERSION,
    SlotRecord,
    default_store_payload,
    empty_day_payload,
)
from .simulations import actual_cost, no_battery_cost

BAD_SLOT_FLAGS = (
    FLAG_GAP
    | FLAG_MISSING_IMPORT_PRICE
    | FLAG_MISSING_EXPORT_PRICE
    | FLAG_MISSING_METER
    | FLAG_METER_RESET
)


@dataclass(frozen=True, slots=True)
class HistoricalPeriodSummary:
    """Aggregated value and attributes for one historical sensor."""

    value: float | None
    tracking_started_at: str | None
    last_complete_slot: str | None
    slots: int
    missing_slots: int
    period_start: str
    period_end: str
    scenario: str | None


class HistoricalCostStore:
    """Thin wrapper around Home Assistant Store for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        slot_minutes: int,
        currency: str,
    ) -> None:
        """Initialize the store wrapper."""
        self.hass = hass
        self.entry_id = entry_id
        self.slot_minutes = int(slot_minutes)
        self.currency = currency
        self._store = Store[dict[str, Any]](
            hass,
            STORE_VERSION,
            f"{DOMAIN}.history.{entry_id}",
            private=True,
        )
        self.data = default_store_payload(
            slot_minutes=slot_minutes,
            currency=currency,
            started_at=datetime.now(tz=UTC),
        )
        self._loaded = False

    async def async_load(self) -> None:
        """Load, migrate, and prune stored historical data."""
        payload = await self._store.async_load()
        if not isinstance(payload, dict):
            payload = default_store_payload(
                slot_minutes=self.slot_minutes,
                currency=self.currency,
                started_at=datetime.now(tz=UTC),
            )
        self.data = self._migrate(payload)
        self.prune(datetime.now(tz=UTC))
        self._loaded = True

    def mark_dirty(self) -> None:
        """Schedule a delayed coalesced save."""
        self.prune(datetime.now(tz=UTC))
        self._store.async_delay_save(lambda: self.data, SAVE_DELAY_SECONDS)

    async def async_flush(self) -> None:
        """Persist the current in-memory payload immediately."""
        if self._loaded:
            self.prune(datetime.now(tz=UTC))
            await self._store.async_save(self.data)

    def update_metadata(
        self,
        *,
        last_processed_slot: datetime | None = None,
        last_meter_values: dict[str, float | None] | None = None,
        meter_config: dict[str, Any] | None = None,
    ) -> None:
        """Update persisted cursors and configuration metadata."""
        if last_processed_slot is not None:
            self.data["last_processed_slot"] = _utc_iso(last_processed_slot)
        if last_meter_values is not None:
            self.data["last_meter_values"] = dict(last_meter_values)
        if meter_config is not None:
            self.data["meter_config"] = dict(meter_config)
        self.mark_dirty()

    def last_processed_slot(self) -> datetime | None:
        """Return the last processed slot cursor."""
        value = self.data.get("last_processed_slot")
        if not isinstance(value, str) or not value:
            return None
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        return parsed.astimezone(UTC)

    def last_meter_values(self) -> dict[str, float | None]:
        """Return the last meter cursor payload."""
        raw = self.data.get("last_meter_values")
        if not isinstance(raw, dict):
            return {}
        values: dict[str, float | None] = {}
        for key, value in raw.items():
            if value is None:
                values[str(key)] = None
                continue
            try:
                values[str(key)] = float(value)
            except (TypeError, ValueError):
                values[str(key)] = None
        return values

    def simulation_soc(self) -> dict[str, float]:
        """Return persisted self-consumption battery SoC values."""
        state = self.data.setdefault("simulation_state", {}).setdefault(
            "self_consumption",
            {},
        )
        batteries = state.setdefault("batteries", {})
        if not isinstance(batteries, dict):
            state["batteries"] = {}
            batteries = state["batteries"]
        result: dict[str, float] = {}
        for subentry_id, payload in batteries.items():
            if not isinstance(payload, dict):
                continue
            try:
                result[str(subentry_id)] = float(payload["soc_kwh"])
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def update_simulation_soc(self, soc_by_battery: dict[str, float]) -> None:
        """Persist self-consumption battery SoC state."""
        state = self.data.setdefault("simulation_state", {}).setdefault(
            "self_consumption",
            {},
        )
        state["batteries"] = {
            subentry_id: {"soc_kwh": float(soc)}
            for subentry_id, soc in soc_by_battery.items()
        }

    def append_slot(self, record: SlotRecord) -> None:
        """Append one slot fact record to retained history."""
        local_day = self._local_date(record.start).isoformat()
        days = self.data.setdefault("days", {})
        day_payload = days.setdefault(local_day, empty_day_payload())
        for key in DAY_ARRAY_KEYS:
            day_payload.setdefault(key, [])
        day_payload["starts"].append(_utc_iso(record.start))
        day_payload["import_price"].append(record.import_price)
        day_payload["export_price"].append(record.export_price)
        day_payload["grid_import"].append(record.grid_import)
        day_payload["grid_export"].append(record.grid_export)
        day_payload["usage"].append(record.usage)
        day_payload["pv"].append(record.pv)
        day_payload["self_consumption_grid_import"].append(
            record.self_consumption_grid_import
        )
        day_payload["self_consumption_grid_export"].append(
            record.self_consumption_grid_export
        )
        day_payload["flags"].append(int(record.flags))
        self.data["last_processed_slot"] = _utc_iso(record.start)
        self.mark_dirty()

    def prune(self, now: datetime) -> None:
        """Drop retained local days outside the fixed retention window."""
        days = self.data.setdefault("days", {})
        if not isinstance(days, dict):
            self.data["days"] = {}
            return
        cutoff = self._local_date(now) - timedelta(days=RETENTION_DAYS - 1)
        for key in list(days):
            try:
                day = date.fromisoformat(str(key))
            except ValueError:
                del days[key]
                continue
            if day < cutoff:
                del days[key]

    def summary(
        self,
        *,
        metric: HistoricalMetric,
        period: str,
        scenario: str | None,
        now: datetime | None = None,
    ) -> HistoricalPeriodSummary:
        """Return an aggregate summary for one sensor."""
        now = now or datetime.now(tz=UTC)
        period_start, period_end = self._period_bounds(period, now)
        records = list(self._records_between(period_start, period_end))
        missing_slots = sum(1 for record in records if int(record.flags) != 0)
        values: list[float] = []
        for record in records:
            value = self._record_value(record, metric=metric, scenario=scenario)
            if value is not None:
                values.append(value)
        total = round(sum(values), 4) if values else None
        return HistoricalPeriodSummary(
            value=total,
            tracking_started_at=self._tracking_started_at(),
            last_complete_slot=self.data.get("last_processed_slot"),
            slots=len(records),
            missing_slots=missing_slots,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            scenario=scenario,
        )

    def _record_value(
        self,
        record: SlotRecord,
        *,
        metric: HistoricalMetric,
        scenario: str | None,
    ) -> float | None:
        if metric is HistoricalMetric.COST:
            if scenario is None:
                return None
            return self._scenario_cost(record, scenario)
        actual = self._scenario_cost(record, SCENARIO_ACTUAL)
        if actual is None:
            return None
        if metric is HistoricalMetric.SAVINGS_VS_NO_BATTERY:
            baseline = self._scenario_cost(record, SCENARIO_NO_BATTERY)
        else:
            baseline = self._scenario_cost(record, SCENARIO_SELF_CONSUMPTION)
        if baseline is None:
            return None
        return baseline - actual

    def _scenario_cost(self, record: SlotRecord, scenario: str) -> float | None:
        if record.flags & BAD_SLOT_FLAGS:
            return None
        if record.import_price is None or record.export_price is None:
            return None
        if scenario == SCENARIO_ACTUAL:
            if record.grid_import is None or record.grid_export is None:
                return None
            return actual_cost(
                grid_import=record.grid_import,
                grid_export=record.grid_export,
                import_price=record.import_price,
                export_price=record.export_price,
            )
        if scenario == SCENARIO_NO_BATTERY:
            if record.usage is None or record.pv is None:
                return None
            return no_battery_cost(
                usage=record.usage,
                pv=record.pv,
                import_price=record.import_price,
                export_price=record.export_price,
            )
        if scenario == SCENARIO_SELF_CONSUMPTION:
            if record.flags & FLAG_SELF_CONSUMPTION_UNAVAILABLE:
                return None
            if (
                record.self_consumption_grid_import is None
                or record.self_consumption_grid_export is None
            ):
                return None
            return actual_cost(
                grid_import=record.self_consumption_grid_import,
                grid_export=record.self_consumption_grid_export,
                import_price=record.import_price,
                export_price=record.export_price,
            )
        return None

    def _records_between(
        self, start: datetime, end: datetime
    ) -> list[SlotRecord]:
        records: list[SlotRecord] = []
        days = self.data.get("days", {})
        if not isinstance(days, dict):
            return records
        for day_payload in days.values():
            if not isinstance(day_payload, dict):
                continue
            starts = day_payload.get("starts", [])
            if not isinstance(starts, list):
                continue
            for index, raw_start in enumerate(starts):
                parsed = dt_util.parse_datetime(str(raw_start))
                if parsed is None:
                    continue
                slot_start = parsed.astimezone(UTC)
                if slot_start < start or slot_start >= end:
                    continue
                records.append(self._record_from_day(day_payload, index, slot_start))
        records.sort(key=lambda record: record.start)
        return records

    def _record_from_day(
        self, day_payload: dict[str, Any], index: int, start: datetime
    ) -> SlotRecord:
        return SlotRecord(
            start=start,
            import_price=_optional_float_at(day_payload, "import_price", index),
            export_price=_optional_float_at(day_payload, "export_price", index),
            grid_import=_optional_float_at(day_payload, "grid_import", index),
            grid_export=_optional_float_at(day_payload, "grid_export", index),
            usage=_optional_float_at(day_payload, "usage", index),
            pv=_optional_float_at(day_payload, "pv", index),
            self_consumption_grid_import=_optional_float_at(
                day_payload,
                "self_consumption_grid_import",
                index,
            ),
            self_consumption_grid_export=_optional_float_at(
                day_payload,
                "self_consumption_grid_export",
                index,
            ),
            flags=int(_optional_float_at(day_payload, "flags", index) or 0),
        )

    def _period_bounds(
        self, period: str, now: datetime
    ) -> tuple[datetime, datetime]:
        local_now = dt_util.as_local(now)
        if period == PERIOD_THIS_MONTH:
            local_start = local_now.replace(
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            if local_start.month == 12:
                local_end = local_start.replace(
                    year=local_start.year + 1,
                    month=1,
                )
            else:
                local_end = local_start.replace(month=local_start.month + 1)
        else:
            local_start = local_now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            local_end = local_start + timedelta(days=1)
        return local_start.astimezone(UTC), local_end.astimezone(UTC)

    def _local_date(self, value: datetime) -> date:
        return dt_util.as_local(value).date()

    def _tracking_started_at(self) -> str | None:
        value = self.data.get("tracking_started_at")
        return str(value) if value else None

    def _migrate(self, payload: dict[str, Any]) -> dict[str, Any]:
        migrated = default_store_payload(
            slot_minutes=self.slot_minutes,
            currency=self.currency,
            started_at=datetime.now(tz=UTC),
        )
        if int(payload.get("version", 0) or 0) <= STORE_VERSION:
            migrated.update(payload)
        migrated["version"] = STORE_VERSION
        migrated["slot_minutes"] = int(migrated.get("slot_minutes") or self.slot_minutes)
        migrated["currency"] = str(migrated.get("currency") or self.currency)
        if not isinstance(migrated.get("last_meter_values"), dict):
            migrated["last_meter_values"] = {}
        if not isinstance(migrated.get("meter_config"), dict):
            migrated["meter_config"] = {}
        if not isinstance(migrated.get("days"), dict):
            migrated["days"] = {}
        if not isinstance(migrated.get("simulation_state"), dict):
            migrated["simulation_state"] = {}
        for day_key, day_payload in list(migrated["days"].items()):
            if not isinstance(day_payload, dict):
                del migrated["days"][day_key]
                continue
            for array_key in DAY_ARRAY_KEYS:
                if not isinstance(day_payload.get(array_key), list):
                    day_payload[array_key] = []
        return migrated


def _optional_float_at(payload: dict[str, Any], key: str, index: int) -> float | None:
    values = payload.get(key, [])
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
