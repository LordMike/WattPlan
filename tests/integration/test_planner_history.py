"""Disk-backed, local-day planner reproduction coverage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace

from custom_components.wattplan.planner_history import PlannerHistory


def _history(tmp_path, *, enabled=True, retention_days=14):
    hass = SimpleNamespace(
        config=SimpleNamespace(
            time_zone="Europe/Copenhagen",
            path=lambda *parts: str(tmp_path.joinpath(*parts)),
        ),
        async_add_executor_job=asyncio.to_thread,
    )
    return PlannerHistory(
        hass,
        entry_id="test-entry",
        enabled=enabled,
        retention_days=retention_days,
        integration_version="0.6.2-alpha",
    )


def _request(start_at: datetime):
    return {
        "window": SimpleNamespace(start_at=start_at, slot_minutes=15, slots=4),
        "slot_minutes": 15,
        "_recorded_optimizer_params": {
            "grid_import_price_per_kwh": [1.23456789, 2.0, 3.0, 4.0],
            "battery_entities": [{"name": "Batteri", "initial_kwh": 5.125}],
        },
    }


async def test_local_day_jsonl_retention_and_unfiltered_date_export(tmp_path):
    """Files follow local dates, are self-contained, and prune only old days."""
    history = _history(tmp_path, retention_days=2)
    root = tmp_path / "wattplan_reproductions" / "test-entry"
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("leave me alone", encoding="utf-8")
    for day in (20, 21, 22):
        created = datetime(2026, 9, day, 22, 30, tzinfo=UTC)
        await history.async_record(
            created, _request(created), {"entities": [{"name": "Batteri"}]}
        )

    assert not (root / "2026-09-21.jsonl").exists()
    assert (root / "2026-09-22.jsonl").exists()
    assert (root / "2026-09-23.jsonl").exists()
    assert (root / "notes.txt").read_text(encoding="utf-8") == "leave me alone"

    day = await history.async_read_date(datetime(2026, 9, 22).date())
    assert day["date"] == "2026-09-22"
    assert day["min_available_date"] == "2026-09-22"
    assert day["max_available_date"] == "2026-09-23"
    assert day["data"].endswith("\n")
    record = json.loads(day["data"])
    assert record["created_at"] == "2026-09-21T22:30:00+00:00"
    assert record["request"]["optimizer_params"]["grid_import_price_per_kwh"][0] == 1.23456789
    assert record["result"]["entities"][0]["name"] == "Batteri"

    absent = await history.async_read_date(datetime(2026, 9, 24).date())
    assert absent["data"] == ""
    assert absent["min_available_date"] == "2026-09-22"

    stopped = _history(tmp_path, enabled=False)
    stopped_day = await stopped.async_read_date(datetime(2026, 9, 22).date())
    assert stopped_day["data"] == day["data"]


async def test_recovers_interrupted_append_and_does_not_publish_partial_line(tmp_path):
    """A crash-truncated trailing line is excluded until the next append."""
    history = _history(tmp_path)
    created = datetime(2026, 9, 29, 10, tzinfo=UTC)
    await history.async_record(created, _request(created), {"entities": []})
    path = tmp_path / "wattplan_reproductions" / "test-entry" / "2026-09-29.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"broken":')
    before = await history.async_read_date(created.date())
    assert before["incomplete_tail"] is True
    assert len(before["data"].splitlines()) == 1

    await history.async_record(created, _request(created), {"entities": []})
    after = await history.async_read_date(created.date())
    assert after["incomplete_tail"] is False
    assert len(after["data"].splitlines()) == 2
    assert all(json.loads(line)["schema_version"] == 1 for line in after["data"].splitlines())


async def test_recording_disabled_or_disk_error_never_breaks_plan(tmp_path, caplog):
    """Recording is strictly opt-in and errors cannot invalidate optimization."""
    created = datetime(2026, 9, 29, tzinfo=UTC)
    disabled = _history(tmp_path, enabled=False)
    await disabled.async_record(created, _request(created), {"entities": []})
    assert not (tmp_path / "wattplan_reproductions").exists()

    enabled = _history(tmp_path)

    def fail_write(_date, _record):
        raise OSError("disk full")

    enabled._append_and_prune = fail_write
    await enabled.async_record(created, _request(created), {"entities": []})
    assert "Could not record WattPlan planner reproduction" in caplog.text
    bad_result = _history(tmp_path)
    await bad_result.async_record(created, _request(created), {"bad": object()})
    assert not (tmp_path / "wattplan_reproductions").exists()
