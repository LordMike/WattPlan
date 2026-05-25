#!/usr/bin/env python3
"""Backfill recorder data for all entities in one WattPlan testbed entry."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
HASS_CORE = REPO_ROOT.parent / "hass-core"
sys.path.insert(0, str(HASS_CORE))
sys.path.insert(0, str(REPO_ROOT / "testbed/custom_components"))

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from homeassistant.components.recorder import db_schema
from homeassistant.components.recorder.db_schema import (
    SchemaChanges,
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.helpers.json import json_bytes
from homeassistant.util import slugify

from wattplan_testbed.const import (
    CONF_CAPACITY_KWH,
    CONF_DEFAULT_SOC_PCT,
    CONF_SEED,
    CONF_SLOT_MINUTES,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    SUBENTRY_BATTERY,
    SUBENTRY_LOAD,
    SUBENTRY_PRICE,
    SUBENTRY_PV,
)
from wattplan_testbed.generators import (
    GenerationContext,
    cumulative_load_states,
    floor_to_slot,
    load_power_points,
    price_points,
    pv_power_points,
)


def _load_entries(config_dir: Path) -> list[dict[str, Any]]:
    path = config_dir / ".storage/core.config_entries"
    payload = json.loads(path.read_text("utf-8"))
    return list(payload.get("data", {}).get("entries", []))


def _find_entry(config_dir: Path, selector: str | None) -> dict[str, Any]:
    entries = [entry for entry in _load_entries(config_dir) if entry.get("domain") == DOMAIN]
    if selector is None:
        if len(entries) != 1:
            raise SystemExit(f"Expected exactly one {DOMAIN} entry, found {len(entries)}")
        return entries[0]
    for entry in entries:
        if selector in {entry.get("entry_id"), entry.get("title")}:
            return entry
    raise SystemExit(f"No {DOMAIN} entry matched {selector!r}")


def _entry_slug(entry: dict[str, Any]) -> str:
    return slugify(str(entry.get("title") or "testbed")) or "testbed"


def _subentry_slug(subentry: dict[str, Any]) -> str:
    data = subentry.get("data", {})
    return slugify(str(data.get("name") or subentry.get("title") or "asset")) or "asset"


def _entity_ids(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entry_slug = _entry_slug(entry)
    entities: dict[str, dict[str, Any]] = {}
    for subentry in entry.get("subentries", []):
        sub_slug = _subentry_slug(subentry)
        sub_type = subentry["subentry_type"]
        if sub_type == SUBENTRY_PRICE:
            entities[f"sensor.{entry_slug}_{sub_slug}_price"] = {
                "subentry": subentry,
                "kind": "price",
            }
        elif sub_type == SUBENTRY_PV:
            entities[f"sensor.{entry_slug}_{sub_slug}_pv_power"] = {
                "subentry": subentry,
                "kind": "pv_power",
            }
        elif sub_type == SUBENTRY_LOAD:
            entities[f"sensor.{entry_slug}_{sub_slug}_load_power"] = {
                "subentry": subentry,
                "kind": "load_power",
            }
            entities[f"sensor.{entry_slug}_{sub_slug}_load_energy"] = {
                "subentry": subentry,
                "kind": "load_energy",
            }
        elif sub_type == SUBENTRY_BATTERY:
            entities[f"sensor.{entry_slug}_{sub_slug}_soc"] = {
                "subentry": subentry,
                "kind": "battery_soc",
            }
            entities[f"binary_sensor.{entry_slug}_{sub_slug}_available"] = {
                "subentry": subentry,
                "kind": "battery_available",
            }
    return entities


def _currency(config_dir: Path) -> str:
    """Return the HA configured currency when available."""
    path = config_dir / ".storage/core.config"
    if not path.exists():
        return "USD"
    payload = json.loads(path.read_text("utf-8"))
    return str(payload.get("data", {}).get("currency", "USD"))


def _state_attrs(kind: str, *, currency: str) -> dict[str, Any]:
    if kind in {"load_energy"}:
        return {
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
        }
    if kind == "battery_soc":
        return {"device_class": "battery", "unit_of_measurement": "%"}
    if kind == "battery_available":
        return {}
    if kind == "pv_power":
        return {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        }
    if kind == "load_power":
        return {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        }
    return {"state_class": "measurement", "unit_of_measurement": f"{currency}/kWh"}


def _attrs_id(session: Session, attrs: dict[str, Any]) -> int:
    raw = json_bytes(attrs)
    shared = raw.decode("utf-8")
    hashed = StateAttributes.hash_shared_attrs_bytes(raw)
    existing = session.execute(
        select(StateAttributes).where(
            StateAttributes.hash == hashed,
            StateAttributes.shared_attrs == shared,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing.attributes_id)
    row = StateAttributes(hash=hashed, shared_attrs=shared)
    session.add(row)
    session.flush()
    return int(row.attributes_id)


def _metadata_id(session: Session, entity_id: str) -> int:
    existing = session.execute(
        select(StatesMeta).where(StatesMeta.entity_id == entity_id)
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing.metadata_id)
    row = StatesMeta(entity_id=entity_id)
    session.add(row)
    session.flush()
    return int(row.metadata_id)


def _statistics_metadata_id(session: Session, entity_id: str) -> int:
    existing = session.execute(
        select(StatisticsMeta).where(StatisticsMeta.statistic_id == entity_id)
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing.id)
    row = StatisticsMeta(
        statistic_id=entity_id,
        source="recorder",
        unit_of_measurement="kWh",
        unit_class="energy",
        has_mean=False,
        has_sum=True,
        name=entity_id,
        mean_type=StatisticMeanType.NONE,
    )
    session.add(row)
    session.flush()
    return int(row.id)


def _insert_state(
    session: Session,
    *,
    entity_id: str,
    state: str,
    at: datetime,
    attrs_id: int,
    metadata_id: int,
) -> None:
    timestamp = at.timestamp()
    session.add(
        States(
            state=state,
            attributes_id=attrs_id,
            metadata_id=metadata_id,
            last_updated_ts=timestamp,
            last_changed_ts=None,
            last_reported_ts=None,
            origin_idx=0,
        )
    )


def _points_for(kind: str, config: dict[str, Any], ctx: GenerationContext) -> list[dict[str, Any]]:
    if kind == "price":
        return price_points(config, ctx)
    if kind == "pv_power":
        return pv_power_points(config, ctx)
    if kind == "load_power":
        return load_power_points(config, ctx)
    return []


def _delete_existing(
    session: Session,
    *,
    entity_ids: list[str],
    start_ts: float,
    end_ts: float,
) -> None:
    metadata_ids = list(
        session.execute(
            select(StatesMeta.metadata_id).where(StatesMeta.entity_id.in_(entity_ids))
        ).scalars()
    )
    if metadata_ids:
        session.execute(
            delete(States).where(
                States.metadata_id.in_(metadata_ids),
                States.last_updated_ts >= start_ts,
                States.last_updated_ts <= end_ts,
            )
        )
    statistics_ids = list(
        session.execute(
            select(StatisticsMeta.id).where(StatisticsMeta.statistic_id.in_(entity_ids))
        ).scalars()
    )
    if statistics_ids:
        session.execute(
            delete(Statistics).where(
                Statistics.metadata_id.in_(statistics_ids),
                Statistics.start_ts >= start_ts,
                Statistics.start_ts <= end_ts,
            )
        )


def _validate_schema(session: Session) -> None:
    latest = session.execute(select(func.max(SchemaChanges.schema_version))).scalar()
    if latest is not None and int(latest) != db_schema.SCHEMA_VERSION:
        raise SystemExit(
            f"Recorder schema is {latest}, expected {db_schema.SCHEMA_VERSION}. "
            "Start HA once to migrate, then stop it and retry."
        )


def backfill(args: argparse.Namespace) -> None:
    """Run recorder backfill."""
    config_dir = args.config
    entry = _find_entry(config_dir, args.entry)
    currency = _currency(config_dir)
    entities = _entity_ids(entry)
    end_at = floor_to_slot(
        datetime.now(tz=UTC),
        int(
            entry["data"].get(
                CONF_UPDATE_INTERVAL_MINUTES,
                entry["data"].get(CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES),
            )
        ),
    )
    start_at = end_at - timedelta(days=args.days)
    if args.dry_run:
        print(f"Entry: {entry['title']} ({entry['entry_id']})")
        print(f"Window: {start_at.isoformat()} -> {end_at.isoformat()}")
        for entity_id, meta in entities.items():
            print(f"{entity_id}: {meta['kind']}")
        return

    db_path = config_dir / "home-assistant_v2.db"
    if not db_path.exists():
        raise SystemExit(f"Recorder database not found: {db_path}")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        _validate_schema(session)
        if args.replace_window:
            _delete_existing(
                session,
                entity_ids=list(entities),
                start_ts=start_at.timestamp(),
                end_ts=end_at.timestamp(),
            )
        for entity_id, meta in entities.items():
            subentry = meta["subentry"]
            sub_data = dict(entry.get("data", {})) | dict(subentry.get("data", {}))
            seed = int(sub_data.get(CONF_SEED, 1))
            slot_minutes = int(
                sub_data.get(
                    CONF_UPDATE_INTERVAL_MINUTES,
                    sub_data.get(
                        CONF_SLOT_MINUTES,
                        entry["data"].get(
                            CONF_UPDATE_INTERVAL_MINUTES,
                            entry["data"].get(
                                CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
                            ),
                        ),
                    ),
                )
            )
            slots = max(1, int((end_at - start_at).total_seconds() // (slot_minutes * 60)))
            ctx = GenerationContext(start_at=start_at, slot_minutes=slot_minutes, slots=slots, seed=seed)
            attrs_id = _attrs_id(session, _state_attrs(meta["kind"], currency=currency))
            metadata_id = _metadata_id(session, entity_id)
            if meta["kind"] == "load_energy":
                states = cumulative_load_states(
                    sub_data,
                    start_at=start_at,
                    end_at=end_at,
                    slot_minutes=slot_minutes,
                    seed=seed,
                )
                for at, value in states:
                    _insert_state(
                        session,
                        entity_id=entity_id,
                        state=f"{value:.6f}",
                        at=at,
                        attrs_id=attrs_id,
                        metadata_id=metadata_id,
                    )
                stat_id = _statistics_metadata_id(session, entity_id)
                for at, value in states:
                    if at.minute != 0:
                        continue
                    session.add(
                        Statistics(
                            metadata_id=stat_id,
                            start_ts=at.timestamp(),
                            state=float(value),
                            sum=float(value),
                        )
                    )
                continue
            if meta["kind"] == "battery_soc":
                base = float(sub_data.get(CONF_DEFAULT_SOC_PCT, 50.0))
                capacity = float(sub_data.get(CONF_CAPACITY_KWH, 10.0))
                for index in range(slots):
                    at = start_at + timedelta(minutes=index * slot_minutes)
                    value = max(0.0, min(100.0, base + (((index + seed) % 17) - 8) * (capacity / 100.0)))
                    _insert_state(
                        session,
                        entity_id=entity_id,
                        state=f"{value:.2f}",
                        at=at,
                        attrs_id=attrs_id,
                        metadata_id=metadata_id,
                    )
                continue
            if meta["kind"] == "battery_available":
                for index in range(slots):
                    at = start_at + timedelta(minutes=index * slot_minutes)
                    _insert_state(
                        session,
                        entity_id=entity_id,
                        state="on",
                        at=at,
                        attrs_id=attrs_id,
                        metadata_id=metadata_id,
                    )
                continue
            for point in _points_for(meta["kind"], sub_data, ctx):
                _insert_state(
                    session,
                    entity_id=entity_id,
                    state=f"{float(point['value']):.6f}",
                    at=datetime.fromisoformat(str(point["start"])),
                    attrs_id=attrs_id,
                    metadata_id=metadata_id,
                )
        session.commit()
    print(f"Backfilled {len(entities)} entities for {entry['title']} from {start_at} to {end_at}")


def main() -> None:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/tmp/wattplan-ha-testbed/default"))
    parser.add_argument("--entry", help="Testbed entry id or title")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--replace-window", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(args)


if __name__ == "__main__":
    main()
