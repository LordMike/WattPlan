"""Home Assistant Store wrapper for historical cost tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import math
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
    FLAG_SELF_CONSUMPTION_UNAVAILABLE,
    HistoricalMetric,
    PERIOD_THIS_MONTH,
    PERIOD_TODAY,
    RETENTION_DAYS,
    SAVE_DELAY_SECONDS,
    SCENARIO_ACTUAL,
    SCENARIO_GRID_ONLY,
    SCENARIO_SELF_CONSUMPTION,
    STORE_VERSION,
    SlotRecord,
    default_store_payload,
    empty_day_payload,
)
from .simulations import actual_cost, grid_only_cost

IMPORT_DEPENDENT_BAD_SLOT_FLAGS = FLAG_GAP | FLAG_MISSING_IMPORT_PRICE


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
    reference_segment_ids: tuple[str, ...]


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
            self.data.pop("meter_cursor_seeded", None)
        if last_meter_values is not None:
            self.data["last_meter_values"] = {
                str(key): _finite_float(value) if value is not None else None
                for key, value in last_meter_values.items()
            }
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
                values[str(key)] = _finite_float(value)
            except (TypeError, ValueError):
                values[str(key)] = None
        return values

    def has_slot(self, slot_start: datetime) -> bool:
        """Return whether the local day already contains the slot."""
        days = self.data.get("days", {})
        if not isinstance(days, dict):
            return False
        day_payload = days.get(self._local_date(slot_start).isoformat(), {})
        if not isinstance(day_payload, dict):
            return False
        starts = day_payload.get("starts", [])
        return isinstance(starts, list) and _utc_iso(slot_start) in starts

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
                value = _finite_float(payload["soc_kwh"])
            except (KeyError, TypeError, ValueError):
                continue
            if value is not None:
                result[str(subentry_id)] = value
        return result

    def simulation_continuity(self) -> dict[str, Any]:
        """Return persisted self-consumption continuity metadata."""
        state = self.data.setdefault("simulation_state", {}).setdefault(
            "self_consumption", {}
        )
        return {
            "valid": state.get("valid") is True,
            "segment_id": state.get("segment_id"),
            "segment_started_at": state.get("segment_started_at"),
            "segment_reason": state.get("segment_reason"),
            "untrusted_since": state.get("untrusted_since"),
            "untrusted_reason": state.get("untrusted_reason"),
            "configuration": state.get("configuration"),
        }

    def start_simulation_segment(
        self,
        soc_by_battery: dict[str, float],
        *,
        started_at: datetime,
        reason: str,
        configuration: list[dict[str, Any]],
    ) -> bool:
        """Initialize a new explicitly identified comparison segment."""
        sanitized = {
            str(subentry_id): {"soc_kwh": value}
            for subentry_id, soc in soc_by_battery.items()
            if (value := _finite_float(soc)) is not None
        }
        if len(sanitized) != len(soc_by_battery):
            return False
        state = self.data.setdefault("simulation_state", {}).setdefault(
            "self_consumption", {}
        )
        counter = int(_finite_float(state.get("segment_counter")) or 0) + 1
        state.update(
            {
                "batteries": sanitized,
                "valid": True,
                "segment_counter": counter,
                "segment_id": f"segment-{counter}",
                "segment_started_at": _utc_iso(started_at),
                "segment_reason": reason,
                "untrusted_since": None,
                "untrusted_reason": None,
                "configuration": configuration,
            }
        )
        self.mark_dirty()
        return True

    def invalidate_simulation(self, *, at: datetime, reason: str) -> None:
        """Mark reference state untrusted without altering earlier slot totals."""
        state = self.data.setdefault("simulation_state", {}).setdefault(
            "self_consumption", {}
        )
        if state.get("valid") is not True and state.get("untrusted_reason") == reason:
            return
        state.update(
            {
                "batteries": {},
                "valid": False,
                "untrusted_since": _utc_iso(at),
                "untrusted_reason": reason,
            }
        )
        self.mark_dirty()

    def update_simulation_soc(self, soc_by_battery: dict[str, float]) -> None:
        """Persist self-consumption battery SoC state."""
        state = self.data.setdefault("simulation_state", {}).setdefault(
            "self_consumption",
            {},
        )
        state["batteries"] = {
            subentry_id: {"soc_kwh": value}
            for subentry_id, soc in soc_by_battery.items()
            if (value := _finite_float(soc)) is not None
        }

    def remember_price_series(
        self,
        *,
        start_at: datetime,
        slot_minutes: int,
        import_prices: list[float],
        export_prices: list[float],
    ) -> None:
        """Retain normalized planner price values by UTC slot start."""
        if int(slot_minutes) != self.slot_minutes:
            return
        cache = self.data.setdefault("price_cache", {})
        if not isinstance(cache, dict):
            cache = {}
            self.data["price_cache"] = cache

        interval = timedelta(minutes=self.slot_minutes)
        changed = False
        for index, raw_import_price in enumerate(import_prices):
            import_price = _finite_float(raw_import_price)
            if import_price is None:
                continue
            slot_start = start_at.astimezone(UTC) + (interval * index)
            export_price = _finite_float(
                export_prices[index] if index < len(export_prices) else 0.0
            )
            if export_price is None:
                continue
            cache[_utc_iso(slot_start)] = {
                "import_price": import_price,
                "export_price": export_price,
            }
            changed = True
        if changed:
            self.mark_dirty()

    def cached_price(self, slot_start: datetime, kind: str) -> float | None:
        """Return a retained planner price for one UTC slot, if available."""
        cache = self.data.get("price_cache")
        if not isinstance(cache, dict):
            return None
        entry = cache.get(_utc_iso(slot_start))
        if not isinstance(entry, dict):
            return None
        return _finite_float(entry.get(f"{kind}_price"))

    def append_slot(self, record: SlotRecord) -> None:
        """Append one slot fact record to retained history."""
        record = _sanitize_record(record)
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
        day_payload["self_consumption_segment_id"].append(
            record.self_consumption_segment_id
        )
        day_payload["flags"].append(int(record.flags))
        self.data["last_processed_slot"] = _utc_iso(record.start)
        self.mark_dirty()

    def prune(self, now: datetime) -> None:
        """Drop retained local days and cached prices outside the retention window."""
        days = self.data.setdefault("days", {})
        if not isinstance(days, dict):
            self.data["days"] = {}
        cutoff = self._local_date(now) - timedelta(days=RETENTION_DAYS - 1)
        if isinstance(days, dict):
            for key in list(days):
                try:
                    day = date.fromisoformat(str(key))
                except ValueError:
                    del days[key]
                    continue
                if day < cutoff:
                    del days[key]

        cache = self.data.setdefault("price_cache", {})
        if not isinstance(cache, dict):
            self.data["price_cache"] = {}
            return
        for key in list(cache):
            parsed = dt_util.parse_datetime(str(key))
            if parsed is None:
                del cache[key]
                continue
            if self._local_date(parsed.astimezone(UTC)) < cutoff:
                del cache[key]

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
        missing_slots = 0
        values: list[float] = []
        for record in records:
            value = self._record_value(record, metric=metric, scenario=scenario)
            if value is None:
                missing_slots += 1
            if value is not None and math.isfinite(value):
                values.append(value)
        try:
            summed = math.fsum(values)
        except OverflowError:
            summed = math.inf
        total = round(summed, 4) if values and math.isfinite(summed) else None
        if total is None and not records and self._tracking_intersects_period(
            period_start, period_end
        ):
            total = 0.0
        return HistoricalPeriodSummary(
            value=total,
            tracking_started_at=self._tracking_started_at(),
            last_complete_slot=self.data.get("last_processed_slot"),
            slots=len(records),
            missing_slots=missing_slots,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            scenario=scenario,
            reference_segment_ids=tuple(
                dict.fromkeys(
                    record.self_consumption_segment_id
                    for record in records
                    if record.self_consumption_segment_id is not None
                )
            ),
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
        if metric is HistoricalMetric.SAVINGS_VS_GRID_ONLY:
            baseline = self._scenario_cost(record, SCENARIO_GRID_ONLY)
        else:
            baseline = self._scenario_cost(record, SCENARIO_SELF_CONSUMPTION)
        if baseline is None:
            return None
        return baseline - actual

    def _scenario_cost(self, record: SlotRecord, scenario: str) -> float | None:
        if (
            record.flags & IMPORT_DEPENDENT_BAD_SLOT_FLAGS
            or record.import_price is None
        ):
            return None
        if scenario == SCENARIO_ACTUAL:
            if record.flags & FLAG_MISSING_EXPORT_PRICE:
                return None
            if record.export_price is None:
                return None
            if record.grid_import is None or record.grid_export is None:
                return None
            try:
                return actual_cost(
                    grid_import=record.grid_import,
                    grid_export=record.grid_export,
                    import_price=record.import_price,
                    export_price=record.export_price,
                )
            except ValueError:
                return None
        if scenario == SCENARIO_GRID_ONLY:
            if record.usage is None:
                return None
            try:
                return grid_only_cost(
                    usage=record.usage,
                    import_price=record.import_price,
                )
            except ValueError:
                return None
        if scenario == SCENARIO_SELF_CONSUMPTION:
            if record.flags & (
                FLAG_MISSING_EXPORT_PRICE | FLAG_SELF_CONSUMPTION_UNAVAILABLE
            ):
                return None
            if record.export_price is None:
                return None
            if (
                record.usage is None
                or record.pv is None
                or record.self_consumption_grid_import is None
                or record.self_consumption_grid_export is None
                or record.self_consumption_segment_id is None
            ):
                return None
            try:
                return actual_cost(
                    grid_import=record.self_consumption_grid_import,
                    grid_export=record.self_consumption_grid_export,
                    import_price=record.import_price,
                    export_price=record.export_price,
                )
            except ValueError:
                return None
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
        flags = int(_optional_float_at(day_payload, "flags", index) or 0)
        record = SlotRecord(
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
            self_consumption_segment_id=_optional_str_at(
                day_payload, "self_consumption_segment_id", index
            ),
            flags=flags,
        )
        return _sanitize_record(record, persisted_payload=day_payload, index=index)

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

    def _tracking_intersects_period(self, period_start: datetime, period_end: datetime) -> bool:
        """Return whether the store has started tracking within this aggregate period."""
        for raw in (
            self.data.get("tracking_started_at"),
            self.data.get("last_processed_slot"),
        ):
            if not isinstance(raw, str) or not raw:
                continue
            parsed = dt_util.parse_datetime(raw)
            if parsed is None:
                continue
            tracked_at = parsed.astimezone(UTC)
            if period_start <= tracked_at < period_end:
                return True
        return False

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
        if not isinstance(migrated.get("price_cache"), dict):
            migrated["price_cache"] = {}
        if not isinstance(migrated.get("days"), dict):
            migrated["days"] = {}
        if not isinstance(migrated.get("simulation_state"), dict):
            migrated["simulation_state"] = {}
        migrated["last_meter_values"] = {
            str(key): _finite_float(value) if value is not None else None
            for key, value in migrated["last_meter_values"].items()
        }
        simulation_state = migrated["simulation_state"].get("self_consumption")
        if not isinstance(simulation_state, dict):
            simulation_state = {}
            migrated["simulation_state"]["self_consumption"] = simulation_state
        legacy_reference_state = "valid" not in simulation_state
        batteries = simulation_state.get("batteries")
        if not isinstance(batteries, dict):
            batteries = {}
        sanitized_batteries = {
            str(key): {"soc_kwh": value}
            for key, item in batteries.items()
            if isinstance(item, dict)
            and (value := _finite_float(item.get("soc_kwh"))) is not None
        }
        simulation_state["batteries"] = sanitized_batteries
        if simulation_state.get("valid") is True and len(sanitized_batteries) != len(
            batteries
        ):
            simulation_state.update(
                {
                    "batteries": {},
                    "valid": False,
                    "untrusted_since": migrated.get("last_processed_slot"),
                    "untrusted_reason": "invalid_persisted_state",
                }
            )
        if "valid" not in simulation_state:
            simulation_state.update(
                {
                    "batteries": {},
                    "valid": False,
                    "segment_counter": 0,
                    "segment_id": None,
                    "segment_started_at": None,
                    "segment_reason": None,
                    "untrusted_since": migrated.get("last_processed_slot"),
                    "untrusted_reason": "legacy_state_unverifiable",
                    "configuration": None,
                }
            )
        for slot_key, price_payload in list(migrated["price_cache"].items()):
            if not isinstance(price_payload, dict):
                del migrated["price_cache"][slot_key]
                continue
            import_price = _finite_float(price_payload.get("import_price"))
            export_price = _finite_float(price_payload.get("export_price"))
            if import_price is None or export_price is None:
                del migrated["price_cache"][slot_key]
                continue
            price_payload["import_price"] = import_price
            price_payload["export_price"] = export_price
        for day_key, day_payload in list(migrated["days"].items()):
            if not isinstance(day_payload, dict):
                del migrated["days"][day_key]
                continue
            for array_key in DAY_ARRAY_KEYS:
                if not isinstance(day_payload.get(array_key), list):
                    day_payload[array_key] = []
            _sanitize_day_payload(day_payload)
        if legacy_reference_state:
            _invalidate_legacy_reference_after_energy_gap(migrated["days"])
        return migrated


def _optional_float_at(payload: dict[str, Any], key: str, index: int) -> float | None:
    values = payload.get(key, [])
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        return _finite_float(value)
    except (TypeError, ValueError):
        return None


def _optional_str_at(payload: dict[str, Any], key: str, index: int) -> str | None:
    values = payload.get(key, [])
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    return str(value) if value else None


def _sanitize_record(
    record: SlotRecord,
    *,
    persisted_payload: dict[str, Any] | None = None,
    index: int | None = None,
) -> SlotRecord:
    """Remove nonfinite slot values and mark their coverage explicitly missing."""
    values = {
        "import_price": _finite_float(record.import_price),
        "export_price": _finite_float(record.export_price),
        "grid_import": _finite_float(record.grid_import),
        "grid_export": _finite_float(record.grid_export),
        "usage": _finite_float(record.usage),
        "pv": _finite_float(record.pv),
        "self_consumption_grid_import": _finite_float(
            record.self_consumption_grid_import
        ),
        "self_consumption_grid_export": _finite_float(
            record.self_consumption_grid_export
        ),
    }
    flags = int(record.flags)

    def _was_nonfinite(key: str, original: float | None) -> bool:
        if persisted_payload is None or index is None:
            return original is not None and values[key] is None
        raw_values = persisted_payload.get(key)
        if not isinstance(raw_values, list) or index >= len(raw_values):
            return False
        raw = raw_values[index]
        return raw is not None and _finite_float(raw) is None

    if _was_nonfinite("import_price", record.import_price):
        flags |= FLAG_MISSING_IMPORT_PRICE
    if _was_nonfinite("export_price", record.export_price):
        flags |= FLAG_MISSING_EXPORT_PRICE
    if any(
        _was_nonfinite(key, getattr(record, key))
        for key in ("grid_import", "grid_export", "usage", "pv")
    ):
        flags |= FLAG_MISSING_METER
    if any(
        _was_nonfinite(key, getattr(record, key))
        for key in (
            "self_consumption_grid_import",
            "self_consumption_grid_export",
        )
    ):
        flags |= FLAG_SELF_CONSUMPTION_UNAVAILABLE
    return SlotRecord(
        start=record.start,
        flags=flags,
        self_consumption_segment_id=record.self_consumption_segment_id,
        **values,
    )


def _sanitize_day_payload(day_payload: dict[str, Any]) -> None:
    """Sanitize persisted slot arrays without dropping historical records."""
    starts = day_payload["starts"]
    flags = day_payload["flags"]
    while len(flags) < len(starts):
        flags.append(0)
    flag_by_key = {
        "import_price": FLAG_MISSING_IMPORT_PRICE,
        "export_price": FLAG_MISSING_EXPORT_PRICE,
        "grid_import": FLAG_MISSING_METER,
        "grid_export": FLAG_MISSING_METER,
        "usage": FLAG_MISSING_METER,
        "pv": FLAG_MISSING_METER,
        "self_consumption_grid_import": FLAG_SELF_CONSUMPTION_UNAVAILABLE,
        "self_consumption_grid_export": FLAG_SELF_CONSUMPTION_UNAVAILABLE,
    }
    for key, invalid_flag in flag_by_key.items():
        values = day_payload[key]
        for index, raw in enumerate(values):
            if raw is None:
                continue
            value = _finite_float(raw)
            values[index] = value
            if value is None and index < len(flags):
                flags[index] = int(_finite_float(flags[index]) or 0) | invalid_flag
    for index, raw in enumerate(flags):
        value = _finite_float(raw)
        flags[index] = int(value) if value is not None else FLAG_GAP


def _invalidate_legacy_reference_after_energy_gap(
    days: dict[str, dict[str, Any]],
) -> None:
    """Retain legacy pre-gap values but reject old post-gap reference state."""
    trusted = True
    records: list[tuple[str, int, dict[str, Any]]] = []
    for day_payload in days.values():
        for index, raw_start in enumerate(day_payload.get("starts", [])):
            records.append((str(raw_start), index, day_payload))
    for _start, index, day_payload in sorted(records, key=lambda item: item[0]):
        usage = _optional_float_at(day_payload, "usage", index)
        pv = _optional_float_at(day_payload, "pv", index)
        if usage is None or pv is None:
            trusted = False
        segment_ids = day_payload["self_consumption_segment_id"]
        while len(segment_ids) <= index:
            segment_ids.append(None)
        if trusted:
            if (
                _optional_float_at(day_payload, "self_consumption_grid_import", index)
                is not None
                and _optional_float_at(
                    day_payload, "self_consumption_grid_export", index
                )
                is not None
            ):
                segment_ids[index] = "legacy-segment-1"
            continue
        for key in (
            "self_consumption_grid_import",
            "self_consumption_grid_export",
        ):
            values = day_payload[key]
            while len(values) <= index:
                values.append(None)
            values[index] = None
        segment_ids[index] = None
        day_payload["flags"][index] |= FLAG_SELF_CONSUMPTION_UNAVAILABLE


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
