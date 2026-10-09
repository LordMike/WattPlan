"""Serialize completed meter intervals for offline planner evaluation."""

from datetime import timedelta
from typing import Any

from .models import (
    FLAG_GAP,
    FLAG_METER_RESET,
    FLAG_MULTI_SLOT_DELTA,
    SlotRecord,
)


def export_actual(
    record: SlotRecord, slot_minutes: int, legacy_roles: dict | None = None
) -> dict[str, Any]:
    metadata = record.actuals_metadata or {}
    slot_minutes = metadata.get("slot_minutes", slot_minutes)
    roles = metadata.get("source_roles", legacy_roles or {})
    quality = []
    for flag, name in (
        (FLAG_GAP, "gap"),
        (FLAG_METER_RESET, "meter_reset"),
        (FLAG_MULTI_SLOT_DELTA, "multi_slot_delta"),
    ):
        if record.flags & flag:
            quality.append(name)
    if metadata.get("partial"):
        quality.append("partial_interval")
    if not record.actuals_metadata:
        quality.append("legacy_provenance")
    # Historical sensors treat absent PV/export meters as zero. A replay must
    # distinguish that convenience from a measured zero.
    usage = record.usage if roles.get("usage") else None
    pv = record.pv if roles.get("pv") else None
    export_price = record.export_price
    if not record.actuals_metadata and not roles.get("grid_export"):
        # Older trackers forced this tariff to zero without an export meter.
        # Its counterfactual value cannot be recovered from that stored zero.
        export_price = None
    values = {
        "usage_kwh": usage,
        "pv_kwh": pv,
        "import_price": record.import_price,
        "export_price": export_price,
    }
    quality.extend(f"missing_{key}" for key, value in values.items() if value is None)
    return {
        "start": record.start.isoformat(),
        "end": (record.start + timedelta(minutes=slot_minutes)).isoformat(),
        **values,
        "grid_import_kwh": record.grid_import if roles.get("grid_import") else None,
        "grid_export_kwh": record.grid_export if roles.get("grid_export") else None,
        "flags": record.flags,
        "currency": metadata.get("currency"),
        "quality": quality,
        "source_roles": roles,
        "tariff_provenance": metadata.get(
            "tariff_sources", "legacy_historical_slot_price"
        ),
    }
