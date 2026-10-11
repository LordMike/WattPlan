"""Versioned reproduction loading and common, causally executable windows."""

import glob
import hashlib
import json
import math
from bisect import bisect_right
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..models import OptimizationParams


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Simulation timestamps must include a timezone")
    return result.astimezone(UTC)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Actual:
    start: datetime
    end: datetime
    values: dict[str, Any]

    @property
    def usable(self) -> bool:
        if self.values.get("flags", 0) & (1 | 16 | 64):
            return False
        if any(
            self.values.get(key) is None
            for key in ("usage_kwh", "pv_kwh", "import_price", "export_price")
        ):
            return False
        return not set(self.values.get("quality", [])) - {"legacy_provenance"}


@dataclass(frozen=True)
class Event:
    captured: datetime
    start: datetime
    params: dict[str, Any]
    legacy: bool

    @property
    def at(self) -> datetime:
        return max(self.captured, self.start)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(
            minutes=self.params["slot_minutes"] * len(self.params["usage_kwh"])
        )

    @property
    def hardware(self) -> str:
        # Order matters for PV allocation. Targets and initial observations do not
        # change physical equipment; curves are per slot, so cadence does.
        return canonical(
            [
                self.params["slot_minutes"],
                [
                    {
                        k: v
                        for k, v in b.items()
                        if k
                        not in {"initial_kwh", "target", "prefer_pv_surplus_charging",
                                "connected_slots", "external_energy_kwh", "departure_requirements"}
                    }
                    for b in self.params["battery_entities"]
                ],
            ]
        )


@dataclass(frozen=True)
class Window:
    event: Event
    start: datetime
    end: datetime
    segment: int
    reset_reason: str | None


@dataclass
class Dataset:
    setup_id: str
    currency: str | None
    timezone: str | None
    events: list[Event]
    actuals: list[Actual]
    files: list[dict[str, str]]
    warnings: list[str]
    dates: list[date] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)

    def range_info(self) -> dict[str, Any]:
        """Return the intended UTC range; dated archives imply whole local days."""
        if not self.events or not self.actuals:
            raise ValueError("Coverage requires planner inputs and actual intervals")
        cadences = {event.params["slot_minutes"] for event in self.events}
        if len(cadences) != 1:
            raise ValueError("Mixed planner slot cadence is not supported")
        minutes = next(iter(cadences))
        if self.dates:
            if not self.timezone:
                raise ValueError("Dated inputs require an explicit timezone")
            zone = ZoneInfo(self.timezone)
            start = datetime.combine(min(self.dates), time(), zone).astimezone(UTC)
            end = datetime.combine(
                max(self.dates) + timedelta(days=1), time(), zone
            ).astimezone(UTC)
            basis = "local_calendar_days"
        else:
            start = min(self.actuals[0].start, min(e.start for e in self.events))
            end = max(
                self.actuals[-1].end,
                max(e.start for e in self.events) + timedelta(minutes=minutes),
            )
            basis = "recorded_extent"
        seconds = (end - start).total_seconds()
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": seconds / 3600,
            "slots": seconds / (minutes * 60),
            "slot_minutes": minutes,
            "basis": basis,
            "recorded_actual_start": self.actuals[0].start.isoformat(),
            "recorded_actual_end": self.actuals[-1].end.isoformat(),
            "recorded_first_plan_start": min(e.start for e in self.events).isoformat(),
            "recorded_last_plan_start": max(e.start for e in self.events).isoformat(),
        }

    def validate_actuals(self, patch: str | None = None) -> dict[str, Any]:
        """Reject every coverage hole; optionally replace exactly one whole slot."""
        info = self.range_info()
        start, end = timestamp(info["start"]), timestamp(info["end"])
        slot = timedelta(minutes=info["slot_minutes"])
        holes = []
        cursor = start
        for actual in self.actuals:
            if actual.end <= start or actual.start >= end:
                continue
            left, right = max(start, actual.start), min(end, actual.end)
            if left < cursor:
                raise ValueError("Actual intervals overlap")
            if left > cursor:
                holes.append((cursor, left))
            if not actual.usable:
                holes.append((left, right))
            cursor = right
        if cursor < end:
            holes.append((cursor, end))
        merged = []
        for left, right in holes:
            if merged and merged[-1][1] == left:
                merged[-1] = (merged[-1][0], right)
            else:
                merged.append((left, right))
        if merged:
            details = ", ".join(
                f"{a.isoformat()} to {b.isoformat()}" for a, b in merged
            )
            if (
                patch not in {"previous", "next"}
                or len(merged) != 1
                or merged[0][1] - merged[0][0] != slot
            ):
                raise ValueError(
                    f"Actual coverage gaps in inferred range {info}: {details}"
                )
            left, right = merged[0]
            if self.patches and any(
                p["start"] != left.isoformat() for p in self.patches
            ):
                raise ValueError("Only one deficient slot total can be repaired")
            adjacent = [e for e in self.events if left - slot <= e.start <= right]
            if len({e.hardware for e in adjacent}) > 1:
                raise ValueError("Actual repair cannot cross an equipment boundary")
            if (left - start) % slot:
                raise ValueError("Only a whole aligned one-slot gap can be patched")
            neighbor_start = left - slot if patch == "previous" else right
            neighbor = next(
                (
                    a
                    for a in self.actuals
                    if a.start == neighbor_start
                    and a.end == neighbor_start + slot
                    and a.usable
                ),
                None,
            )
            if neighbor is None or neighbor.values.get("simulation_patch"):
                raise ValueError(
                    "Patch requires an immediately adjacent usable, unpatched whole slot"
                )
            provenance = {
                "kind": "actual_neighbor_copy",
                "direction": patch,
                "start": left.isoformat(),
                "end": right.isoformat(),
                "source_start": neighbor.start.isoformat(),
                "source_end": neighbor.end.isoformat(),
                "source_values_sha256": hashlib.sha256(
                    canonical(neighbor.values).encode()
                ).hexdigest(),
                "replaced_intervals": [
                    a.values for a in self.actuals if a.start < right and a.end > left
                ],
            }
            values = deepcopy(neighbor.values)
            values.update(
                start=left.isoformat(),
                end=right.isoformat(),
                simulation_patch=provenance,
            )
            # A copied meter flow is not a measured observation of the missing slot.
            values["grid_import_kwh"] = values["grid_export_kwh"] = None
            self.actuals = sorted(
                [a for a in self.actuals if a.end <= left or a.start >= right]
                + [Actual(left, right, values)],
                key=lambda a: a.start,
            )
            self.patches.append(provenance)
            self.warnings.append(
                f"Explicit one-slot actual repair: {details}; copied {patch} slot"
            )
        return info

    def windows(self) -> tuple[list[Window], list[dict[str, Any]]]:
        """Select the same physical coverage and resets for every variant."""
        info = self.validate_coverage()
        range_start, range_end = timestamp(info["start"]), timestamp(info["end"])
        good = [actual for actual in self.actuals if actual.usable]
        starts = [actual.start for actual in good]
        coverage_end = [actual.end for actual in good]
        for i in range(len(good) - 2, -1, -1):
            if good[i].end == good[i + 1].start:
                coverage_end[i] = coverage_end[i + 1]
        result, skipped = [], []
        segment = 0
        previous = None
        execution_events = []
        for event in self.events:
            if range_start <= event.start < range_end:
                if (
                    not event.start
                    <= event.captured
                    < event.start + timedelta(minutes=info["slot_minutes"])
                ):
                    skipped.append(
                        {
                            "at": event.at.isoformat(),
                            "reason": "capture_outside_plan_slot",
                        }
                    )
                else:
                    execution_events.append(event)
        for i, event in enumerate(execution_events):
            at = event.at
            if at >= range_end:
                continue
            index = bisect_right(starts, at) - 1
            if index < 0 or at >= good[index].end or at >= event.end:
                skipped.append(
                    {
                        "at": at.isoformat(),
                        "reason": "no_usable_actuals_or_expired_plan",
                    }
                )
                previous = None
                continue
            end = min(event.end, coverage_end[index], range_end)
            if i + 1 < len(execution_events):
                end = min(end, execution_events[i + 1].at)
            reason = None
            if previous is None or previous.end != at:
                reason = "initial_or_coverage_gap"
            elif previous.event.hardware != event.hardware:
                reason = "equipment_changed"
            if reason:
                segment += 1
            if end <= at:
                raise ValueError("Decision events are not strictly ordered")
            window = Window(event, at, end, segment, reason)
            result.append(window)
            previous = window
        # Capture/completion delay in the initial slot is disclosed, not backdated.
        if not result or result[0].start >= range_start + timedelta(
            minutes=info["slot_minutes"]
        ):
            raise ValueError(
                f"Planner coverage gap at beginning of inferred range {info}"
            )
        if (
            any(a.end != b.start for a, b in pairwise(result))
            or result[-1].end != range_end
        ):
            raise ValueError(
                f"Planner coverage gap (expired or missing inputs) in inferred range {info}"
            )
        return result, skipped

    def validate_coverage(self, patch: str | None = None) -> dict[str, Any]:
        info = self.validate_actuals(patch)
        start, end = timestamp(info["start"]), timestamp(info["end"])
        slot = timedelta(minutes=info["slot_minutes"])
        if (end - start) % slot:
            raise ValueError(f"Inferred range has partial boundary slots: {info}")
        by_slot: dict[datetime, list[Event]] = {}
        for event in self.events:
            if (event.start - start) % slot:
                raise ValueError("Planner slot grid differs from inferred range")
            if event.start <= event.captured < event.start + slot:
                by_slot.setdefault(event.start, []).append(event)
        missing = []
        cursor = start
        while cursor < end:
            if cursor not in by_slot:
                missing.append(cursor)
            cursor += slot
        if missing:
            details = ", ".join(at.isoformat() for at in missing)
            same_gap = not self.patches or all(
                p["start"] == missing[0].isoformat() for p in self.patches
            )
            if patch not in {"previous", "next"} or len(missing) != 1 or not same_gap:
                raise ValueError(
                    f"Missing planning cycles in inferred range {info}: {details}"
                )
            at = missing[0]
            source_at = at - slot if patch == "previous" else at + slot
            sources = by_slot.get(source_at, [])
            if len(sources) != 1:
                raise ValueError(
                    "Planner repair requires exactly one adjacent recorded input"
                )
            source = sources[0]
            if any(
                p["kind"] == "planner_neighbor_copy"
                and p["start"] == source.start.isoformat()
                for p in self.patches
            ):
                raise ValueError("Planner repair cannot copy a synthetic neighbor")
            adjacent = [e for e in self.events if e.start in {at - slot, at + slot}]
            if any(e.hardware != source.hardware for e in adjacent):
                raise ValueError("Planner repair cannot cross an equipment boundary")
            params = deepcopy(source.params)
            shift = int((at - source.start) / slot)
            params["plan_start"] = at.isoformat()
            for battery in params["battery_entities"]:
                target = battery.get("target")
                if target:
                    target["timeslot"] -= shift
                    if target["timeslot"] < 0:
                        battery["target"] = None
            optional = []
            for entity in params.get("optional_entities", []):
                entity["start_after_timeslot"] = max(
                    entity.get("start_after_timeslot", 0) - shift, 0
                )
                entity["start_before_timeslot"] -= shift
                if (
                    entity["start_before_timeslot"] - entity["duration_timeslots"]
                    >= entity["start_after_timeslot"]
                ):
                    optional.append(entity)
            params["optional_entities"] = optional
            try:
                params = OptimizationParams(**params).model_dump(mode="json")
            except ValueError as err:
                raise ValueError(
                    "Planner repair rejected: rebased deadlines/windows must fit the copied forecast horizon and remain valid"
                ) from err
            copied = Event(source.captured + shift * slot, at, params, source.legacy)
            self.events = sorted([*self.events, copied], key=lambda e: e.at)
            provenance = {
                "kind": "planner_neighbor_copy",
                "direction": patch,
                "start": at.isoformat(),
                "end": (at + slot).isoformat(),
                "source_start": source.start.isoformat(),
                "source_captured": source.captured.isoformat(),
                "copied_captured": copied.captured.isoformat(),
                "forecasts": "synthetic unchanged neighbor arrays; absolute deadlines rebased",
            }
            self.patches.append(provenance)
            self.warnings.append(
                f"Explicit one-slot planner repair: {details}; synthetic forecasts copied from {patch} slot"
            )
        return info


def input_files(source: str | Path | Sequence[str | Path]) -> list[Path]:
    """Expand files, quoted globs and recursive directories, deduplicating paths."""
    sources = [source] if isinstance(source, (str, Path)) else source
    files = set()
    for item in sources:
        path = Path(item)
        candidates = (
            path.rglob("*")
            if path.is_dir()
            else (Path(p) for p in glob.glob(str(item), recursive=True))
        )
        matches = {
            p.resolve()
            for p in candidates
            if p.is_file() and p.suffix.lower() in {".json", ".jsonl"}
        }
        if not matches:
            raise ValueError(f"No JSON/JSONL input files matched: {item}")
        files.update(matches)
    if not files:
        raise ValueError("No JSON/JSONL input files supplied")
    return sorted(files)


def load_dataset(
    directory: str | Path | Sequence[str | Path], *, setup_id: str | None = None
) -> Dataset:
    """Read daily JSONL, individual snapshots, or saved service response JSON.

    Duplicate downloads are accepted; conflicting facts or decisions fail closed.
    An incomplete JSONL tail is omitted with a warning, as in the HA export.
    """
    files, records, warnings, dates = [], [], [], []
    for path in input_files(directory):
        try:
            day = date.fromisoformat(path.stem)
            if path.stem == day.isoformat():
                dates.append(day)
        except ValueError:
            pass
        raw = path.read_bytes()
        files.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        if path.suffix.lower() == ".jsonl":
            lines = raw.decode("utf-8-sig").splitlines()
            # Valid JSON without a newline is also a useful hand-saved snapshot.
            if lines and not raw.endswith(b"\n"):
                try:
                    json.loads(lines[-1])
                except json.JSONDecodeError:
                    warnings.append(f"Ignored incomplete trailing record: {path.name}")
                    lines.pop()
            items = [json.loads(line) for line in lines if line.strip()]
        else:
            items = [json.loads(raw)]
        for item in items:
            if isinstance(item, dict) and "service_response" in item:
                item = item["service_response"]
                if not isinstance(item, dict) or not isinstance(item.get("data"), str):
                    raise ValueError("Expected a planner reproduction service response")
            if isinstance(item, dict) and isinstance(item.get("data"), str):
                if item.get("date"):
                    dates.append(date.fromisoformat(item["date"]))
                if item.get("incomplete_tail"):
                    warnings.append(
                        f"Export omitted an incomplete trailing record: {path.name}"
                    )
                records.extend(
                    json.loads(line)
                    for line in item["data"].splitlines()
                    if line.strip()
                )
            else:
                records.append(item)
    if not records:
        raise ValueError("No reproduction records found")
    if any(
        not isinstance(r, dict) or r.get("schema_version") not in {1, 2}
        for r in records
    ):
        raise ValueError("Expected version 1 or 2 reproduction records")
    identities = {r["setup_id"] for r in records if r.get("setup_id")}
    if setup_id is None:
        if len(identities) > 1:
            raise ValueError("Multiple setups: select --setup-id")
        setup_id = next(iter(identities), "legacy")
    if any(not r.get("setup_id") for r in records) and len(identities) > 1:
        raise ValueError("Legacy records cannot be attributed among multiple setups")
    records = [r for r in records if r.get("setup_id", setup_id) == setup_id]
    if any(r.get("synthetic") for r in records):
        warnings.append(
            "Contains explicitly synthetic inputs or actuals; this is not an unmodified historical backtest."
        )
    currencies = {r["currency"] for r in records if r.get("currency")}
    zones = {r["timezone"] for r in records if r.get("timezone")}
    if len(currencies) > 1 or len(zones) > 1:
        raise ValueError("Currency or timezone changed within this dataset")
    plans, actuals = {}, {}
    for record in records:
        if "request" in record:
            params = OptimizationParams(
                **record["request"]["optimizer_params"]
            ).model_dump(mode="json")
            if params["comfort_entities"]:
                raise ValueError(
                    "Comfort loads are not supported by historical simulation v1"
                )
            start = timestamp(
                params.get("plan_start") or record["request"]["window"]["start_at"]
            )
            params["plan_start"] = start.isoformat()
            captured = timestamp(record.get("input_captured_at", record["created_at"]))
            event = Event(captured, start, params, "input_captured_at" not in record)
            if event.at in plans and plans[event.at] != event:
                raise ValueError(f"Conflicting planner inputs at {event.at}")
            plans[event.at] = event
        for item in record.get("actuals", []):
            if item.get("currency"):
                currencies.add(item["currency"])
                if len(currencies) > 1:
                    raise ValueError("Currency changed within actual intervals")
            start, end = timestamp(item["start"]), timestamp(item["end"])
            if end <= start:
                raise ValueError("Actual interval must have positive duration")
            values = dict(item, start=start.isoformat(), end=end.isoformat())
            if (
                not isinstance(values.get("quality", []), list)
                or not all(isinstance(flag, str) for flag in values.get("quality", []))
                or type(values.get("flags", 0)) is not int
            ):
                raise ValueError("Invalid actual quality flags")
            for key in (
                "usage_kwh",
                "pv_kwh",
                "import_price",
                "export_price",
                "grid_import_kwh",
                "grid_export_kwh",
            ):
                value = values.get(key)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or (key.endswith("kwh") and value < 0)
                ):
                    raise ValueError(f"Invalid actual {key} at {start}")
            actual = Actual(start, end, values)
            if start in actuals and actuals[start] != actual:
                raise ValueError(f"Conflicting actuals at {start}")
            actuals[start] = actual
    events = sorted(plans.values(), key=lambda event: event.at)
    ordered = sorted(actuals.values(), key=lambda actual: actual.start)
    if any(a.end > b.start for a, b in pairwise(ordered)):
        raise ValueError("Actual intervals overlap")
    if not events:
        raise ValueError("No planner inputs for the selected setup")
    if any(event.legacy for event in events):
        warnings.append(
            "Legacy decisions use plan completion time; input capture time was not recorded."
        )
    if any(
        "legacy_provenance" in actual.values.get("quality", []) for actual in ordered
    ):
        warnings.append(
            "Legacy actuals lack interval-specific meter provenance and partial-interval detection."
        )
    return Dataset(
        setup_id,
        next(iter(currencies), None),
        next(iter(zones), None),
        events,
        ordered,
        files,
        warnings,
        dates,
    )
