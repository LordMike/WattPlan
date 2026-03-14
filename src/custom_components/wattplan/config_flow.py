"""Config flow for the WattPlan integration."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlowWithReload,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector, translation

from .const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    ADAPTER_TYPE_ATTRIBUTE_VALUES,
    ADAPTER_TYPE_AUTO_DETECT,
    ADAPTER_TYPE_SERVICE_RESPONSE,
    AGGREGATION_MODE_FIRST,
    AGGREGATION_MODE_LAST,
    AGGREGATION_MODE_MAX,
    AGGREGATION_MODE_MEAN,
    AGGREGATION_MODE_MIN,
    CLAMP_MODE_NEAREST,
    CLAMP_MODE_NONE,
    CONF_ACTION_EMISSION_ENABLED,
    CONF_ADAPTER_TYPE,
    CONF_AGGREGATION_MODE,
    CONF_CAN_CHARGE_FROM_GRID,
    CONF_CAN_CHARGE_FROM_PV,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_CLAMP_MODE,
    CONF_CONFIG_ENTRY_ID,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DURATION_MINUTES,
    CONF_EDGE_FILL_MODE,
    CONF_ENERGY_KWH,
    CONF_EXPECTED_POWER_KW,
    CONF_FIXUP_PROFILE,
    CONF_HISTORY_DAYS,
    CONF_HOURS_TO_PLAN,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MAX_DISCHARGE_KW,
    CONF_MEASURED_POWER_SOURCE,
    CONF_MIN_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_ON_MINUTES,
    CONF_MIN_OPTION_GAP_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_ON_OFF_SOURCE,
    CONF_OPTIONS_COUNT,
    CONF_PLANNING_ENABLED,
    CONF_RESAMPLE_MODE,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_SERVICE,
    CONF_SLOT_MINUTES,
    CONF_SOC_SOURCE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_TEMPLATE,
    CONF_TIME_KEY,
    CONF_VALUE_KEY,
    DOMAIN,
    EDGE_FILL_MODE_HOLD,
    EDGE_FILL_MODE_NONE,
    FIXUP_PROFILE_EXTEND,
    FIXUP_PROFILE_REPAIR,
    FIXUP_PROFILE_STRICT,
    HOURS_TO_PLAN_OPTIONS,
    RESAMPLE_MODE_FORWARD_FILL,
    RESAMPLE_MODE_LINEAR,
    RESAMPLE_MODE_NONE,
    SLOT_MINUTE_OPTIONS,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_SERVICE_ADAPTER,
    SOURCE_MODE_TEMPLATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from .forecast_provider import ForecastProvider
from .source_pipeline import build_source_base_provider, build_source_value_provider
from .source_provider import (
    async_auto_detect_entity_adapter,
    async_auto_detect_service_adapter,
    async_get_energy_solar_forecast_entries,
)
from .source_types import SourceProviderError, SourceWindow

CONF_WATTPLAN_ENTITY_ID = "entity_id"

MAX_NAME_LENGTH = 64
MAX_SOURCE_KEY_LENGTH = 64
DEFAULT_SOURCE_TEMPLATE = "{{ [{'start': now().isoformat(), 'value': 0.25}] }}"
SECTION_SOURCE_ADVANCED = "advanced"
SECTION_SOURCE_MANUAL = "manual"
SECTION_BATTERY_ADVANCED = "advanced"
CONF_REVIEW_ACTION = "review_action"
REVIEW_ACTION_CONFIRM = "confirm"
REVIEW_ACTION_EDIT = "edit"
CONF_ACCEPT_SOURCE_SUMMARY = "accept_source_summary"


def _normalize_name(value: str) -> str:
    """Create a stable id from a name."""
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "item"


def _format_number(value: float) -> str:
    """Format a float-like value for compact display."""
    return f"{float(value):g}"


def _format_coverage_datetime(
    value: str | datetime, timezone_name: str | None
) -> str:
    """Format coverage datetimes in the Home Assistant local timezone."""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(parsed, datetime):
        return str(value)
    try:
        timezone = ZoneInfo(timezone_name) if timezone_name else UTC
    except ZoneInfoNotFoundError:
        timezone = UTC
    return parsed.astimezone(timezone).strftime("%Y-%m-%d %H:%M")


async def _coverage_placeholder_text(
    hass: HomeAssistant,
    *,
    summary: dict[str, Any],
    timezone_name: str | None,
    field: str,
) -> str:
    """Return a formatted coverage placeholder or a no-data label."""
    if int(summary.get("available_count", 0)) <= 0:
        return await _async_config_translation(hass, "review_no_data")
    return _format_coverage_datetime(summary.get(field, "Unknown"), timezone_name)


def _subentry_display_title(subentry_type: str, data: dict[str, Any]) -> str:
    """Build a concise display title for a subentry."""
    name = data[CONF_NAME]
    if subentry_type == SUBENTRY_TYPE_BATTERY:
        return (
            f"{name} ("
            f"{_format_number(data[CONF_CAPACITY_KWH])} kWh, "
            f"min {_format_number(data[CONF_MINIMUM_KWH])} kWh)"
        )
    if subentry_type == SUBENTRY_TYPE_COMFORT:
        return (
            f"{name} ("
            f"{_format_number(data[CONF_TARGET_ON_HOURS_PER_WINDOW])}h/"
            f"{_format_number(data[CONF_ROLLING_WINDOW_HOURS])}h, "
            f"max off {_format_number(data[CONF_MAX_CONSECUTIVE_OFF_MINUTES])} min)"
        )
    if subentry_type == SUBENTRY_TYPE_OPTIONAL:
        return (
            f"{name} ("
            f"{_format_number(data[CONF_DURATION_MINUTES])} min / "
            f"{_format_number(data[CONF_RUN_WITHIN_HOURS])}h)"
        )
    return name


def _subentry_name(subentry: Any) -> str:
    """Return the semantic name of a subentry."""
    return str(subentry.data.get(CONF_NAME, subentry.title))


def _expected_slots(config: dict[str, Any]) -> int:
    """Calculate expected slots for the configured horizon."""
    return int(config[CONF_HOURS_TO_PLAN] * 60 / config[CONF_SLOT_MINUTES])


def _source_modifier_fields(
    defaults: dict[str, Any],
    *,
    allow_edge_fill_none: bool = True,
) -> dict[Any, Any]:
    """Build shared source modifier selector fields."""
    edge_fill_options = [
        selector.SelectOptionDict(value=EDGE_FILL_MODE_HOLD, label="Hold edge")
    ]
    if allow_edge_fill_none:
        edge_fill_options.insert(
            0,
            selector.SelectOptionDict(value=EDGE_FILL_MODE_NONE, label="Disabled"),
        )

    return {
        vol.Required(
            CONF_AGGREGATION_MODE,
            default=defaults.get(CONF_AGGREGATION_MODE, AGGREGATION_MODE_MEAN),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=AGGREGATION_MODE_MEAN, label="Mean (average)"
                    ),
                    selector.SelectOptionDict(value=AGGREGATION_MODE_MIN, label="Minimum"),
                    selector.SelectOptionDict(value=AGGREGATION_MODE_MAX, label="Maximum"),
                    selector.SelectOptionDict(value=AGGREGATION_MODE_FIRST, label="First"),
                    selector.SelectOptionDict(value=AGGREGATION_MODE_LAST, label="Last"),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(
            CONF_CLAMP_MODE,
            default=defaults.get(CONF_CLAMP_MODE, CLAMP_MODE_NONE),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=CLAMP_MODE_NONE, label="Strict"),
                    selector.SelectOptionDict(
                        value=CLAMP_MODE_NEAREST, label="Nearest interval"
                    ),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(
            CONF_RESAMPLE_MODE,
            default=defaults.get(CONF_RESAMPLE_MODE, RESAMPLE_MODE_NONE),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=RESAMPLE_MODE_NONE, label="Disabled"),
                    selector.SelectOptionDict(
                        value=RESAMPLE_MODE_FORWARD_FILL, label="Forward fill"
                    ),
                    selector.SelectOptionDict(value=RESAMPLE_MODE_LINEAR, label="Linear"),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(
            CONF_EDGE_FILL_MODE,
            default=defaults.get(
                CONF_EDGE_FILL_MODE,
                EDGE_FILL_MODE_NONE if allow_edge_fill_none else EDGE_FILL_MODE_HOLD,
            ),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=edge_fill_options,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }


def _default_modifier_values() -> dict[str, str]:
    """Return default advanced fixup settings."""
    return {
        CONF_AGGREGATION_MODE: AGGREGATION_MODE_FIRST,
        CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
        CONF_RESAMPLE_MODE: RESAMPLE_MODE_LINEAR,
        CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
    }


def _source_base_defaults(source: dict[str, Any]) -> dict[str, Any]:
    """Return defaults for provider-specific input including fixup settings."""
    return dict(source)


def _preferred_source_mode(
    key: str,
    *,
    include_not_used: bool,
    include_built_in: bool = False,
    include_energy_provider: bool = False,
) -> str:
    """Return the recommended default mode for the requested source step.

    The mode selection step already tells users which path is preferred. This helper keeps
    the form default aligned with that guidance instead of falling back based only on
    whether "Not used" happens to be allowed on the step.
    """
    if key == CONF_SOURCE_IMPORT_PRICE:
        return SOURCE_MODE_ENTITY_ADAPTER
    if key == CONF_SOURCE_EXPORT_PRICE:
        if include_not_used:
            return SOURCE_MODE_NOT_USED
        return SOURCE_MODE_ENTITY_ADAPTER
    if key == CONF_SOURCE_USAGE:
        if include_built_in:
            return SOURCE_MODE_BUILT_IN
        if include_not_used:
            return SOURCE_MODE_NOT_USED
        return SOURCE_MODE_ENTITY_ADAPTER
    if key == CONF_SOURCE_PV:
        if include_energy_provider:
            return SOURCE_MODE_ENERGY_PROVIDER
        if include_not_used:
            return SOURCE_MODE_NOT_USED
        return SOURCE_MODE_ENTITY_ADAPTER
    return SOURCE_MODE_NOT_USED if include_not_used else SOURCE_MODE_TEMPLATE


def _auto_detect_step_defaults(
    user_input: dict[str, Any],
    resolved_source: dict[str, Any],
) -> dict[str, Any]:
    """Return input defaults that preserve auto mode while showing resolved keys.

    The review step stores the resolved source so validation and persistence use
    explicit fields. When the user goes back, we still want the form to reflect
    that auto-detect was the chosen workflow, while showing what it found.
    """
    defaults = dict(user_input)
    defaults[CONF_NAME] = resolved_source.get(CONF_NAME, "")
    defaults[CONF_TIME_KEY] = resolved_source.get(CONF_TIME_KEY, "")
    defaults[CONF_VALUE_KEY] = resolved_source.get(CONF_VALUE_KEY, "")
    defaults[CONF_ADAPTER_TYPE] = ADAPTER_TYPE_AUTO_DETECT
    return defaults


def _source_fixup_fields(
    defaults: dict[str, Any],
    *,
    include_advanced: bool,
    fixup_options: list[selector.SelectOptionDict] | None = None,
    allow_edge_fill_none: bool = True,
) -> dict[Any, Any]:
    """Build shared fixup fields for provider input steps."""
    if fixup_options is None:
        fixup_options = [
            selector.SelectOptionDict(value=FIXUP_PROFILE_STRICT, label="Direct only"),
            selector.SelectOptionDict(
                value=FIXUP_PROFILE_REPAIR, label="Repair local gaps"
            ),
            selector.SelectOptionDict(
                value=FIXUP_PROFILE_EXTEND, label="Extend daily pattern"
            ),
        ]

    schema: dict[Any, Any] = {
        vol.Required(
            CONF_FIXUP_PROFILE,
            default=defaults.get(CONF_FIXUP_PROFILE, FIXUP_PROFILE_REPAIR),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=fixup_options,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    }
    if include_advanced:
        advanced_defaults = {
            **_default_modifier_values(),
            **{
                key: value
                for key, value in defaults.items()
                if key in _default_modifier_values()
            },
        }
        schema[vol.Optional(SECTION_SOURCE_ADVANCED, default=advanced_defaults)] = section(
            vol.Schema(
                _source_modifier_fields(
                    advanced_defaults,
                    allow_edge_fill_none=allow_edge_fill_none,
                )
            ),
            {"collapsed": True},
        )
    return schema


def _source_review_schema(
    defaults: dict[str, Any] | None = None, *, include_accept: bool = True
) -> vol.Schema:
    """Build schema for the source review step."""
    defaults = defaults or {}
    schema: dict[Any, Any] = {}
    if include_accept:
        schema[
            vol.Required(
                CONF_ACCEPT_SOURCE_SUMMARY,
                default=bool(defaults.get(CONF_ACCEPT_SOURCE_SUMMARY, False)),
            )
        ] = selector.BooleanSelector()
    return vol.Schema(schema)


def _final_setup_schema() -> vol.Schema:
    """Build schema for the final setup summary step."""
    return vol.Schema({})


async def _async_validate_source_values(
    hass: HomeAssistant,
    *,
    core_data: dict[str, Any],
    key: str,
    source: dict[str, Any],
    floor_to_slot,
    validate_built_in_entity,
) -> int:
    """Validate one source against the current planner window.

    Config flow and options flow both need the same runtime-equivalent source
    validation. Keeping it here avoids another split between review-time and
    persisted behavior.
    """

    expected_slots = _expected_slots(core_data)
    slot_minutes = int(core_data[CONF_SLOT_MINUTES])
    window = SourceWindow(
        start_at=floor_to_slot(datetime.now(tz=UTC), slot_minutes),
        slot_minutes=slot_minutes,
        slots=expected_slots,
    )
    provider = build_source_value_provider(
        hass,
        source_key=key,
        source_config=source,
        validate_built_in_entity=(
            validate_built_in_entity
            if source.get(CONF_SOURCE_MODE) == SOURCE_MODE_BUILT_IN
            else None
        ),
    )
    values = await provider.async_values(window)
    return len(values)


def _built_in_history_coverage(
    debug: dict[str, Any], start_at: datetime
) -> tuple[datetime, datetime, float]:
    """Return built-in history coverage range and length in days."""

    rows: list[datetime] = []
    for row in debug.get("raw_history_states", []):
        last_changed = row.get("last_changed")
        if isinstance(last_changed, str):
            try:
                rows.append(datetime.fromisoformat(last_changed))
            except ValueError:
                continue
    if not rows:
        for row in debug.get("raw_statistics_rows", []):
            started = row.get("start")
            if isinstance(started, str):
                try:
                    rows.append(datetime.fromisoformat(started))
                except ValueError:
                    continue
    if not rows:
        return start_at, start_at, 0.0
    coverage_start = min(rows)
    coverage_end = max(rows)
    history_days = max(
        0.0, (coverage_end - coverage_start).total_seconds() / 86400.0
    )
    return coverage_start, coverage_end, history_days


def _summarize_payload_coverage(
    payload: list[Any],
    source: dict[str, Any],
    *,
    start_at: datetime,
    slot_minutes: int,
    floor_to_slot,
) -> tuple[int, datetime, datetime, bool]:
    """Summarize source coverage before fixups are applied."""

    slot_delta = timedelta(minutes=slot_minutes)
    if not payload:
        return 0, start_at, start_at, False
    if isinstance(payload[0], dict):
        time_key = str(source.get(CONF_TIME_KEY, "start"))
        slot_indexes: set[int] = set()
        timestamps: list[datetime] = []
        for point in payload:
            if not isinstance(point, dict):
                continue
            stamp = point.get(time_key)
            if not isinstance(stamp, str):
                continue
            try:
                point_dt = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            point_dt = floor_to_slot(
                point_dt.astimezone(UTC)
                if point_dt.tzinfo
                else point_dt.replace(tzinfo=UTC),
                slot_minutes,
            )
            timestamps.append(point_dt)
            slot_indexes.add(int((point_dt - start_at) // slot_delta))
        if not timestamps:
            return 0, start_at, start_at, False
        first = min(timestamps)
        last = max(timestamps) + slot_delta
        available = len(slot_indexes)
        expected_range = (
            int((max(timestamps) - min(timestamps)) // slot_delta) + 1
            if len(timestamps) > 1
            else 1
        )
        return available, first, last, available < expected_range

    available = len(payload)
    return available, start_at, start_at + (available * slot_delta), False


def _coverage_from_available_count(
    start_at: datetime,
    *,
    slot_minutes: int,
    available_count: int,
) -> tuple[datetime, datetime]:
    """Return coverage range for runtime-equivalent resolved values."""
    return start_at, start_at + (available_count * timedelta(minutes=slot_minutes))


def _format_coverage_summary(
    available_count: int, *, expected_slots: int, slot_minutes: int
) -> str:
    """Return one short human-readable coverage summary."""
    return (
        f"{available_count} usable intervals, {expected_slots} needed, "
        f"{slot_minutes}-minute resolution"
    )


async def _async_provider_available_count(
    hass: HomeAssistant,
    *,
    core_data: dict[str, Any],
    key: str,
    source: dict[str, Any],
    floor_to_slot,
    validate_built_in_entity,
) -> int:
    """Return the available interval count for one provider configuration."""
    slot_minutes = int(core_data[CONF_SLOT_MINUTES])
    expected_slots = _expected_slots(core_data)
    window = SourceWindow(
        start_at=floor_to_slot(datetime.now(tz=UTC), slot_minutes),
        slot_minutes=slot_minutes,
        slots=expected_slots,
    )
    provider = build_source_base_provider(
        hass,
        source_key=key,
        source_config=source,
        validate_built_in_entity=validate_built_in_entity,
    )
    values = await provider.async_values(window)
    return len(values)


def _invalid_key_from_source_error(err: SourceProviderError) -> str:
    """Map source provider errors to flow translation keys."""

    built_in_reason = err.details.get("built_in_reason")
    diagnostic_kind = err.details.get("diagnostic_kind")
    if err.code == "source_fetch":
        if "config_entry_id" in err.details:
            return "energy_provider_unavailable"
        if "entity_id" in err.details and "attribute" in err.details:
            return "attribute_name_required"
        if "entity_id" in err.details:
            return "entity_not_found"
        return "source_fetch_error"
    if err.code == "source_parse":
        if built_in_reason == "no_numeric_history":
            return "built_in_no_numeric_history"
        if "rendered a string" in str(err):
            return "template_invalid_structure"
        return "invalid_payload"
    if err.code == "source_validation":
        if diagnostic_kind == "auto_detect_no_match":
            return "auto_detect_no_match"
        if diagnostic_kind == "auto_detect_conflict":
            return "auto_detect_conflict"
        if "entity_ids" in err.details or "service" in err.details:
            return "invalid_payload"
    return "not_enough_values"


def _candidate_summary_line(candidate: dict[str, Any]) -> str:
    """Return one compact markdown bullet for an auto-detect candidate."""
    path = str(candidate.get("path", "<root>"))
    row_count = int(candidate.get("row_count", 0))
    reason = str(candidate.get("reason", "not compatible"))
    timestamp_keys = ", ".join(str(key) for key in candidate.get("timestamp_keys", []))
    numeric_keys = ", ".join(str(key) for key in candidate.get("numeric_keys", []))

    detail_parts = [f"{row_count} rows", reason]
    if timestamp_keys:
        detail_parts.append(f"timestamps: {timestamp_keys}")
    if numeric_keys:
        detail_parts.append(f"numeric: {numeric_keys}")
    return f"- `{path}`: {'; '.join(detail_parts)}"


def _auto_detect_action_text(source: dict[str, Any]) -> str:
    """Return a user-facing next action for the active source mode."""
    source_mode = source.get(CONF_SOURCE_MODE)
    if source_mode == SOURCE_MODE_ENTITY_ADAPTER:
        return (
            "Ensure you picked an entity with forecast data in its attributes, "
            "or switch to manual mapping."
        )
    if source_mode == SOURCE_MODE_SERVICE_ADAPTER:
        return (
            "Ensure the service returns forecast data in its response, "
            "or switch to manual mapping."
        )
    return (
        "Switch to manual mapping and confirm the root path, timestamp field, "
        "and value field."
    )


def _entity_candidate_status_line(
    source: dict[str, Any], entity_id: str, candidates: list[dict[str, Any]]
) -> str:
    """Return a user-facing diagnostic line for one selected entity."""
    if not candidates:
        return (
            f"- ❌ Not usable: `{entity_id}`. WattPlan did not find any list-like "
            f"forecast data to inspect. {_auto_detect_action_text(source)}"
        )

    compatible = [
        candidate for candidate in candidates if str(candidate.get("reason")) == "compatible"
    ]
    if compatible:
        best = max(compatible, key=lambda candidate: int(candidate.get("row_count", 0)))
        row_count = int(best.get("row_count", 0))
        path = str(best.get("path", "<root>"))
        return (
            f"- ✅ Looks usable: `{entity_id}`. Found {row_count} forecast entries "
            f"in `{path}`."
        )

    best = max(candidates, key=lambda candidate: int(candidate.get("row_count", 0)))
    path = str(best.get("path", "<root>"))
    reason = str(best.get("reason", "not compatible"))

    if reason == "rows have no numeric value fields":
        problem = f"Found timestamp-like data in `{path}`, but no price/value field."
    elif reason == "rows have no timestamp-like fields":
        problem = f"Found list data in `{path}`, but no timestamp field WattPlan can use."
    elif reason == "rows have multiple numeric fields, so one value key could not be chosen":
        problem = (
            f"Found data in `{path}`, but more than one numeric field looked like "
            "the value."
        )
    elif reason == "rows do not share one usable timestamp field":
        problem = (
            f"Found data in `{path}`, but the rows do not share one consistent "
            "timestamp field."
        )
    elif reason == "list is empty":
        problem = f"Found `{path}`, but the list is empty."
    elif reason.startswith("items are "):
        sample_type = reason.removeprefix("items are ").removesuffix(", not objects")
        problem = (
            f"Found `{path}`, but its items are `{sample_type}` values rather than "
            "timestamped forecast objects."
        )
    else:
        problem = (
            f"Found data in `{path}`, but WattPlan could not recognize it as "
            "forecast rows."
        )

    return f"- ❌ Not usable: `{entity_id}`. {problem} {_auto_detect_action_text(source)}"


def _conflict_action_text(source: dict[str, Any]) -> str:
    """Return guidance when multiple compatible mappings disagree."""
    source_mode = source.get(CONF_SOURCE_MODE)
    if source_mode == SOURCE_MODE_ENTITY_ADAPTER:
        return "Use only one usable entity for this source, or switch to manual mapping."
    if source_mode == SOURCE_MODE_SERVICE_ADAPTER:
        return (
            "Return one consistent forecast structure from the service, or switch "
            "to manual mapping."
        )
    return "Use one consistent forecast structure, or switch to manual mapping."


def _preview_source_from_auto_detect_error(
    source: dict[str, Any],
    err: SourceProviderError,
) -> dict[str, Any] | None:
    """Build a best-effort preview source from usable auto-detect matches."""
    if source.get(CONF_SOURCE_MODE) != SOURCE_MODE_ENTITY_ADAPTER:
        return None

    detected = err.details.get("detected_mappings")
    if not isinstance(detected, list) or not detected:
        return None

    groups: dict[tuple[str, str, str], list[str]] = {}
    for item in detected:
        if not isinstance(item, dict):
            continue
        root_key = str(item.get("root_key", ""))
        time_key = str(item.get("time_key", ""))
        value_key = str(item.get("value_key", ""))
        entity_id = str(item.get("entity_id", ""))
        if not entity_id or not root_key or not time_key or not value_key:
            continue
        groups.setdefault((root_key, time_key, value_key), []).append(entity_id)

    if not groups:
        return None

    (root_key, time_key, value_key), entity_ids = max(
        groups.items(),
        key=lambda item: (len(item[1]), sorted(item[1])),
    )
    return {
        **source,
        CONF_WATTPLAN_ENTITY_ID: entity_ids,
        CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
        CONF_NAME: root_key,
        CONF_TIME_KEY: time_key,
        CONF_VALUE_KEY: value_key,
    }


def _auto_detect_diagnostic_text(
    source: dict[str, Any],
    source_input: dict[str, Any] | None,
    resolved_source: dict[str, Any],
    err: SourceProviderError | None,
) -> str:
    """Return markdown describing auto-detect findings for the review page."""
    adapter_type = None
    if source_input is not None:
        adapter_type = source_input.get(CONF_ADAPTER_TYPE)
    if adapter_type != ADAPTER_TYPE_AUTO_DETECT:
        return ""

    lines = ["**Auto-detect**"]
    if err is None:
        lines.extend(
            [
                "",
                f"- Root path: `{resolved_source.get(CONF_NAME, '') or '<root>'}`",
                f"- Timestamp field: `{resolved_source.get(CONF_TIME_KEY, '')}`",
                f"- Value field: `{resolved_source.get(CONF_VALUE_KEY, '')}`",
            ]
        )
        return "\n".join(lines)

    lines.append("")
    if err.details.get("diagnostic_kind") == "auto_detect_conflict":
        lines.append(
            "- WattPlan found forecast-like data, but it could not build one consistent source from the selected input."
        )
        lines.append("")
        for detected in err.details.get("detected_mappings", []):
            entity_id = str(detected.get("entity_id", "entity"))
            root_key = str(detected.get("root_key", "<root>"))
            time_key = str(detected.get("time_key", ""))
            value_key = str(detected.get("value_key", ""))
            lines.append(
                f"- ✅ Looks usable: `{entity_id}`. Found forecast data in `{root_key}` "
                f"using `{time_key}` for time and `{value_key}` for value."
            )
        lines.append("")
        lines.append(f"- ⚠️ Next step: {_conflict_action_text(source)}")
        return "\n".join(lines)

    lines.append(
        "- WattPlan could not build a usable forecast source from the selected input."
    )
    entity_candidates = err.details.get("entity_candidates")
    if isinstance(entity_candidates, dict):
        lines.append("")
        for entity_id, candidates in entity_candidates.items():
            lines.append(
                _entity_candidate_status_line(
                    source,
                    str(entity_id),
                    list(candidates),
                )
            )
    else:
        candidates = err.details.get("candidates", [])
        lines.append("")
        if not candidates:
            lines.append(
                "- ❌ Not usable. WattPlan did not find any list-like forecast data "
                f"to inspect. {_auto_detect_action_text(source)}"
            )
        else:
            best = max(
                list(candidates),
                key=lambda candidate: int(candidate.get("row_count", 0)),
            )
            lines.append(
                f"  {_candidate_summary_line(best)}"
            )
            lines.append(
                f"- ⚠️ Next step: {_auto_detect_action_text(source)}"
            )
    return "\n".join(lines)


def _source_mode_summary(source: dict[str, Any] | None) -> str:
    """Return a short human-readable summary of the selected source mode."""
    if not isinstance(source, dict):
        return "Not configured"

    mode = source.get(CONF_SOURCE_MODE)
    if mode == SOURCE_MODE_ENTITY_ADAPTER:
        entity_ids = source.get(CONF_WATTPLAN_ENTITY_ID)
        if isinstance(entity_ids, list) and entity_ids:
            return f"Entity attribute: {', '.join(str(entity_id) for entity_id in entity_ids)}"
        if isinstance(entity_ids, str) and entity_ids:
            return f"Entity attribute: {entity_ids}"
        return "Entity attribute"
    if mode == SOURCE_MODE_SERVICE_ADAPTER:
        service = source.get(CONF_SERVICE)
        return f"Service call: {service}" if service else "Service call"
    if mode == SOURCE_MODE_TEMPLATE:
        return "Template"
    if mode == SOURCE_MODE_BUILT_IN:
        entity_id = source.get(CONF_WATTPLAN_ENTITY_ID)
        return f"Built in: {entity_id}" if entity_id else "Built in"
    if mode == SOURCE_MODE_ENERGY_PROVIDER:
        config_entry_id = source.get(CONF_CONFIG_ENTRY_ID)
        return (
            f"Energy provider: {config_entry_id}"
            if config_entry_id
            else "Energy provider"
        )
    if mode == SOURCE_MODE_NOT_USED:
        return "Not used"
    return "Not configured"

async def _async_source_summary(
    hass: HomeAssistant,
    *,
    core_data: dict[str, Any],
    key: str,
    source: dict[str, Any],
    source_input: dict[str, Any] | None,
    floor_to_slot,
    validate_built_in_entity,
) -> dict[str, Any]:
    """Return one lightweight source summary for the review page."""

    expected_slots = _expected_slots(core_data)
    slot_minutes = int(core_data[CONF_SLOT_MINUTES])
    start_at = floor_to_slot(datetime.now(tz=UTC), slot_minutes)
    available_count = 0
    coverage_start = start_at
    coverage_end = start_at
    raw_available_count = 0
    raw_coverage_start = start_at
    raw_coverage_end = start_at
    history_coverage_days = 0.0
    error_key: str | None = None
    source_error: SourceProviderError | None = None
    resolved_input = source_input
    has_preview_source = False

    try:
        resolved_source, resolved_input = await _async_resolve_source_for_review(
            hass, source=source, source_input=source_input
        )
    except SourceProviderError as err:
        resolved_source = source
        error_key = _invalid_key_from_source_error(err)
        is_valid = False
        source_error = err
        if preview_source := _preview_source_from_auto_detect_error(source, err):
            resolved_source = preview_source
            has_preview_source = True
            if source_input is not None:
                resolved_input = _auto_detect_step_defaults(source_input, preview_source)
    else:
        is_valid = True

    mode = resolved_source.get(CONF_SOURCE_MODE)

    try:
        if mode == SOURCE_MODE_BUILT_IN:
            provider = build_source_base_provider(
                hass,
                source_key=key,
                source_config=resolved_source,
                validate_built_in_entity=validate_built_in_entity,
            )
            if not isinstance(provider, ForecastProvider):
                raise AssertionError("Built-in usage source should use ForecastProvider")
            debug = await provider.async_debug_payload(
                SourceWindow(
                    start_at=start_at,
                    slot_minutes=slot_minutes,
                    slots=expected_slots,
                )
            )
            # Built-in usage review should still summarize the forward forecast window, not the
            # historical rows used to build it. The historical span is only used for the
            # limited-history warning text below.
            coverage_start, coverage_end, history_coverage_days = (
                _built_in_history_coverage(debug, start_at)
            )
    except (SourceProviderError, vol.Invalid):
        pass

    if error_key is None or has_preview_source:
        raw_source = {**resolved_source, CONF_FIXUP_PROFILE: FIXUP_PROFILE_STRICT}
        try:
            raw_available_count = await _async_provider_available_count(
                hass,
                core_data=core_data,
                key=key,
                source=raw_source,
                floor_to_slot=floor_to_slot,
                validate_built_in_entity=validate_built_in_entity,
            )
            raw_coverage_start, raw_coverage_end = _coverage_from_available_count(
                start_at,
                slot_minutes=slot_minutes,
                available_count=raw_available_count,
            )
        except SourceProviderError as err:
            if available_from_error := err.details.get("available_count"):
                raw_available_count = int(available_from_error)
                raw_coverage_start, raw_coverage_end = _coverage_from_available_count(
                    start_at,
                    slot_minutes=slot_minutes,
                    available_count=raw_available_count,
                )

        try:
            available_count = await _async_validate_source_values(
                hass,
                core_data=core_data,
                key=key,
                source=resolved_source,
                floor_to_slot=floor_to_slot,
                validate_built_in_entity=validate_built_in_entity,
            )
            coverage_start, coverage_end = _coverage_from_available_count(
                start_at,
                slot_minutes=slot_minutes,
                available_count=available_count,
            )
        except SourceProviderError as err:
            error_key = _invalid_key_from_source_error(err)
            is_valid = False
            source_error = err
            if available_from_error := err.details.get("available_count"):
                available_count = int(available_from_error)
                coverage_start, coverage_end = _coverage_from_available_count(
                    start_at,
                    slot_minutes=slot_minutes,
                    available_count=available_count,
                )

    history_warning = False
    review_text_key = "review_ready"
    review_text_placeholders: dict[str, str] | None = None
    if (
        is_valid
        and available_count >= expected_slots
        and raw_available_count < expected_slots
    ):
        review_text_key = "review_ready_extended"
        review_text_placeholders = {
            "raw_coverage_summary": _format_coverage_summary(
                raw_available_count,
                expected_slots=expected_slots,
                slot_minutes=slot_minutes,
            )
        }
    elif mode == SOURCE_MODE_BUILT_IN and history_coverage_days < 7:
        history_warning = True
        review_text_key = "review_limited_history"
        review_text_placeholders = {"history_days": f"{history_coverage_days:.1f}"}
    if not is_valid:
        review_text_key = "review_invalid"
    elif available_count < expected_slots:
        review_text_key = "review_incomplete"
        review_text_placeholders = None

    review_text = await _async_config_translation(
        hass,
        review_text_key,
        placeholders=review_text_placeholders,
    )
    diagnostic_text = _auto_detect_diagnostic_text(
        source,
        resolved_input,
        resolved_source,
        source_error,
    )

    return {
        "available_count": available_count,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "coverage_summary": _format_coverage_summary(
            available_count,
            expected_slots=expected_slots,
            slot_minutes=slot_minutes,
        ),
        "raw_available_count": raw_available_count,
        "raw_coverage_start": raw_coverage_start.isoformat(),
        "raw_coverage_end": raw_coverage_end.isoformat(),
        "raw_coverage_summary": _format_coverage_summary(
            raw_available_count,
            expected_slots=expected_slots,
            slot_minutes=slot_minutes,
        ),
        "review_text": review_text,
        "diagnostic_text": diagnostic_text,
        "is_valid": is_valid,
        "error_key": error_key,
        "history_warning": history_warning,
        "resolved_source": resolved_source,
        "resolved_source_input": resolved_input,
    }


async def _async_config_translation(
    hass: HomeAssistant,
    key: str,
    *,
    placeholders: dict[str, str] | None = None,
) -> str:
    """Return one localized config string for WattPlan.

    The review flow renders translated markdown text in placeholders, so these
    strings need to come from the config translation catalog instead of being
    hardcoded in Python.
    """

    translations = await translation.async_get_translations(
        hass,
        hass.config.language,
        "config",
        integrations=[DOMAIN],
    )
    message = translations.get(f"component.{DOMAIN}.config.error.{key}", key)
    if placeholders:
        message = message.format(**placeholders)
    return message


def _core_schema(
    defaults: dict[str, Any] | None = None, *, include_name: bool = False
) -> vol.Schema:
    """Build schema for the core planner settings."""
    defaults = defaults or {}
    slot_default = str(defaults.get(CONF_SLOT_MINUTES, 15))
    hours_default = str(defaults.get(CONF_HOURS_TO_PLAN, 48))
    schema: dict[Any, Any] = {
        vol.Required(CONF_SLOT_MINUTES, default=slot_default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[str(option) for option in SLOT_MINUTE_OPTIONS],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_HOURS_TO_PLAN, default=hours_default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[str(option) for option in HOURS_TO_PLAN_OPTIONS],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }
    if include_name:
        schema[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "WattPlan"))] = (
            selector.TextSelector()
        )
    return vol.Schema(schema)


def _normalize_core_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize core form values to numeric types."""
    normalized = dict(user_input)
    normalized[CONF_SLOT_MINUTES] = int(normalized[CONF_SLOT_MINUTES])
    normalized[CONF_HOURS_TO_PLAN] = int(normalized[CONF_HOURS_TO_PLAN])
    return normalized


def _source_mode_schema(
    default_mode: str,
    *,
    include_not_used: bool = False,
    include_built_in: bool = False,
    include_energy_provider: bool = False,
) -> vol.Schema:
    """Build source mode selection schema."""
    options: list[selector.SelectOptionDict] = []
    if include_built_in:
        options.append(selector.SelectOptionDict(value=SOURCE_MODE_BUILT_IN, label="Built in"))
    options.extend(
        [
            selector.SelectOptionDict(
                value=SOURCE_MODE_ENTITY_ADAPTER, label="Entity attribute"
            ),
            selector.SelectOptionDict(
                value=SOURCE_MODE_SERVICE_ADAPTER, label="Service call"
            ),
        ]
    )
    if include_energy_provider:
        options.append(
            selector.SelectOptionDict(
                value=SOURCE_MODE_ENERGY_PROVIDER, label="Energy provider"
            )
        )
    options.append(selector.SelectOptionDict(value=SOURCE_MODE_TEMPLATE, label="Template"))
    if include_not_used:
        options.append(selector.SelectOptionDict(value=SOURCE_MODE_NOT_USED, label="Not used"))

    return vol.Schema(
        {
            vol.Required(
                CONF_SOURCE_MODE,
                default=default_mode,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _source_template_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build template source schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_TEMPLATE,
                default=defaults.get(CONF_TEMPLATE, DEFAULT_SOURCE_TEMPLATE),
            ): selector.TemplateSelector(),
            **_source_fixup_fields(defaults, include_advanced=True),
        }
    )


def _source_adapter_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build entity adapter source schema."""
    defaults = defaults or {}
    adapter_options: list[selector.SelectOptionDict] = [
        selector.SelectOptionDict(
            value=ADAPTER_TYPE_AUTO_DETECT,
            label="Auto detect",
        ),
        selector.SelectOptionDict(
            value=ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
            label="Attribute objects",
        ),
        selector.SelectOptionDict(
            value=ADAPTER_TYPE_ATTRIBUTE_VALUES,
            label="Attribute values",
        ),
    ]

    entity_default = defaults.get(CONF_WATTPLAN_ENTITY_ID)
    if isinstance(entity_default, str):
        entity_default = [entity_default]
    entity_key = (
        vol.Required(CONF_WATTPLAN_ENTITY_ID, default=entity_default)
        if entity_default is not None
        else vol.Required(CONF_WATTPLAN_ENTITY_ID)
    )
    return vol.Schema(
        {
            entity_key: selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True)
            ),
            vol.Required(
                CONF_ADAPTER_TYPE,
                default=defaults.get(CONF_ADAPTER_TYPE, ADAPTER_TYPE_AUTO_DETECT),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=adapter_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                SECTION_SOURCE_MANUAL,
                default={
                    CONF_NAME: defaults.get(CONF_NAME, "forecast"),
                    CONF_TIME_KEY: defaults.get(CONF_TIME_KEY, "start"),
                    CONF_VALUE_KEY: defaults.get(CONF_VALUE_KEY, "value"),
                },
            ): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_NAME,
                            default=defaults.get(CONF_NAME, "forecast"),
                        ): selector.TextSelector(),
                        vol.Required(
                            CONF_TIME_KEY,
                            default=defaults.get(CONF_TIME_KEY, "start"),
                        ): selector.TextSelector(),
                        vol.Required(
                            CONF_VALUE_KEY,
                            default=defaults.get(CONF_VALUE_KEY, "value"),
                        ): selector.TextSelector(),
                    }
                ),
                {"collapsed": True},
            ),
            **_source_fixup_fields(defaults, include_advanced=True),
        }
    )


def _source_service_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build service adapter source schema."""
    defaults = defaults or {}
    adapter_options: list[selector.SelectOptionDict] = [
        selector.SelectOptionDict(
            value=ADAPTER_TYPE_AUTO_DETECT,
            label="Auto detect",
        ),
        selector.SelectOptionDict(
            value=ADAPTER_TYPE_SERVICE_RESPONSE,
            label="Service call",
        ),
    ]

    return vol.Schema(
        {
            vol.Required(
                CONF_SERVICE,
                default=defaults.get(CONF_SERVICE, ""),
            ): selector.TextSelector(),
            vol.Required(
                CONF_ADAPTER_TYPE,
                default=defaults.get(CONF_ADAPTER_TYPE, ADAPTER_TYPE_AUTO_DETECT),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=adapter_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                SECTION_SOURCE_MANUAL,
                default={
                    CONF_NAME: defaults.get(CONF_NAME, ""),
                    CONF_TIME_KEY: defaults.get(CONF_TIME_KEY, "start"),
                    CONF_VALUE_KEY: defaults.get(CONF_VALUE_KEY, "value"),
                },
            ): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_NAME,
                            default=defaults.get(CONF_NAME, ""),
                        ): selector.TextSelector(),
                        vol.Required(
                            CONF_TIME_KEY,
                            default=defaults.get(CONF_TIME_KEY, "start"),
                        ): selector.TextSelector(),
                        vol.Required(
                            CONF_VALUE_KEY,
                            default=defaults.get(CONF_VALUE_KEY, "value"),
                        ): selector.TextSelector(),
                    }
                ),
                {"collapsed": True},
            ),
            **_source_fixup_fields(defaults, include_advanced=True),
        }
    )


def _source_built_in_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build built-in forecast source schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_WATTPLAN_ENTITY_ID,
                default=defaults.get(CONF_WATTPLAN_ENTITY_ID),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"], device_class=["energy"])
            ),
            vol.Required(
                CONF_HISTORY_DAYS,
                default=int(defaults.get(CONF_HISTORY_DAYS, 14)),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=90, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


def _source_energy_provider_schema(
    defaults: dict[str, Any] | None,
    *,
    provider_options: list[selector.SelectOptionDict],
) -> vol.Schema:
    """Build Energy solar forecast provider source schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_CONFIG_ENTRY_ID,
                default=defaults.get(CONF_CONFIG_ENTRY_ID),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=provider_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            **_source_fixup_fields(
                {
                    CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
                    **defaults,
                },
                include_advanced=True,
                fixup_options=[
                    selector.SelectOptionDict(
                        value=FIXUP_PROFILE_EXTEND,
                        label="Extend daily pattern",
                    )
                ],
                allow_edge_fill_none=False,
            ),
        }
    )


def _battery_schema() -> vol.Schema:
    """Build schema for battery subentries."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Required(CONF_SOC_SOURCE): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor"], device_class=["battery"]
                )
            ),
            vol.Required(CONF_CAPACITY_KWH, default=10): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=1000, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_MINIMUM_KWH, default=1): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=1000, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_MAX_CHARGE_KW, default=5): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=500, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_MAX_DISCHARGE_KW, default=5): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=500, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                SECTION_BATTERY_ADVANCED,
                default={
                    CONF_CHARGE_EFFICIENCY: 0.9,
                    CONF_DISCHARGE_EFFICIENCY: 0.9,
                },
            ): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_CHARGE_EFFICIENCY,
                            default=0.9,
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0.01,
                                max=1,
                                step=0.01,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Required(
                            CONF_DISCHARGE_EFFICIENCY,
                            default=0.9,
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0.01,
                                max=1,
                                step=0.01,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required(CONF_CAN_CHARGE_FROM_GRID, default=False): (
                selector.BooleanSelector()
            ),
            vol.Required(CONF_CAN_CHARGE_FROM_PV, default=True): (
                selector.BooleanSelector()
            ),
        }
    )


def _comfort_schema() -> vol.Schema:
    """Build schema for comfort subentries."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Required(CONF_ROLLING_WINDOW_HOURS, default=24): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_TARGET_ON_HOURS_PER_WINDOW, default=8): (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=168, step=0.1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            ),
            vol.Required(CONF_MIN_CONSECUTIVE_ON_MINUTES, default=60): (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=10080, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            ),
            vol.Required(CONF_MIN_CONSECUTIVE_OFF_MINUTES, default=60): (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=10080, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            ),
            vol.Required(CONF_MAX_CONSECUTIVE_OFF_MINUTES, default=240): (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=10080, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            ),
            vol.Required(CONF_ON_OFF_SOURCE): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor"])
            ),
            vol.Required(CONF_EXPECTED_POWER_KW): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=200, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(CONF_MEASURED_POWER_SOURCE): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"])
            ),
        }
    )


def _optional_schema() -> vol.Schema:
    """Build schema for optional subentries."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Required(CONF_DURATION_MINUTES, default=120): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=1440, step=15, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_ENERGY_KWH, default=2): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_OPTIONS_COUNT, default=2): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_RUN_WITHIN_HOURS, default=24): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_MIN_OPTION_GAP_MINUTES, default=60): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=10080, step=15, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


def _validate_text_field(
    value: str,
    field: str,
    errors: dict[str, str],
    *,
    max_length: int,
) -> None:
    """Validate that text field is present and not too long."""
    normalized = str(value).strip()
    if not normalized:
        errors[field] = "text_required"
    elif len(normalized) > max_length:
        errors[field] = "text_too_long"


def _optional_max_distinct_options(
    run_within_minutes: int, duration_minutes: int, min_gap_minutes: int
) -> int:
    """Return the maximum non-overlapping options possible in the time window."""
    if run_within_minutes < duration_minutes:
        return 0
    separation_minutes = duration_minutes + min_gap_minutes
    return ((run_within_minutes - duration_minutes) // separation_minutes) + 1


def _validate_core_data(
    data: dict[str, Any], *, include_name: bool = False
) -> dict[str, str]:
    """Validate core planner values."""
    errors: dict[str, str] = {}
    if include_name:
        _validate_text_field(
            str(data.get(CONF_NAME, "")),
            CONF_NAME,
            errors,
            max_length=MAX_NAME_LENGTH,
        )
    return errors


def _validate_source_adapter_input(data: dict[str, Any]) -> dict[str, str]:
    """Validate adapter text fields before resolving entities/templates."""
    errors: dict[str, str] = {}
    if data.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT:
        return errors
    manual = data.get(SECTION_SOURCE_MANUAL, {})
    if not isinstance(manual, dict):
        manual = {}
    _validate_text_field(
        str(manual.get(CONF_NAME, data.get(CONF_NAME, ""))),
        CONF_NAME,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(manual.get(CONF_TIME_KEY, data.get(CONF_TIME_KEY, ""))),
        CONF_TIME_KEY,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(manual.get(CONF_VALUE_KEY, data.get(CONF_VALUE_KEY, ""))),
        CONF_VALUE_KEY,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    return errors


def _validate_service_adapter_input(data: dict[str, Any]) -> dict[str, str]:
    """Validate service adapter fields before calling the service."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_SERVICE, "")),
        CONF_SERVICE,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    if data.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT:
        return errors
    manual = data.get(SECTION_SOURCE_MANUAL, {})
    if not isinstance(manual, dict):
        manual = {}
    _validate_text_field(
        str(manual.get(CONF_NAME, data.get(CONF_NAME, ""))),
        CONF_NAME,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(manual.get(CONF_TIME_KEY, data.get(CONF_TIME_KEY, ""))),
        CONF_TIME_KEY,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(manual.get(CONF_VALUE_KEY, data.get(CONF_VALUE_KEY, ""))),
        CONF_VALUE_KEY,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    return errors


async def _async_prepare_entity_source_input(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Return explicit entity adapter config, resolving auto-detect if needed."""
    selected_entities = user_input[CONF_WATTPLAN_ENTITY_ID]
    if isinstance(selected_entities, str):
        entity_ids = [selected_entities]
    else:
        entity_ids = [str(entity_id) for entity_id in selected_entities]

    adapter_type = str(user_input[CONF_ADAPTER_TYPE])
    manual = user_input.get(SECTION_SOURCE_MANUAL, {})
    if not isinstance(manual, dict):
        manual = {}
    root_key = str(manual.get(CONF_NAME, user_input.get(CONF_NAME, "")))
    time_key = str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, "")))
    value_key = str(manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, "")))

    resolved_adapter = adapter_type

    if adapter_type == ADAPTER_TYPE_AUTO_DETECT:
        detected = await async_auto_detect_entity_adapter(hass, entity_ids)
        resolved_adapter = ADAPTER_TYPE_ATTRIBUTE_OBJECTS
        root_key = detected.root_key
        time_key = detected.time_key
        value_key = detected.value_key

    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        CONF_WATTPLAN_ENTITY_ID: entity_ids,
        CONF_ADAPTER_TYPE: resolved_adapter,
        CONF_NAME: root_key,
        CONF_TIME_KEY: time_key,
        CONF_VALUE_KEY: value_key,
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
    return source


async def _async_prepare_service_source_input(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Return explicit service adapter config, resolving auto-detect if needed."""
    adapter_type = str(user_input[CONF_ADAPTER_TYPE])
    manual = user_input.get(SECTION_SOURCE_MANUAL, {})
    if not isinstance(manual, dict):
        manual = {}
    root_key = str(manual.get(CONF_NAME, user_input.get(CONF_NAME, "")))
    time_key = str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, "")))
    value_key = str(manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, "")))
    service_name = str(user_input[CONF_SERVICE])
    resolved_adapter = adapter_type

    if adapter_type == ADAPTER_TYPE_AUTO_DETECT:
        detected = await async_auto_detect_service_adapter(hass, service_name)
        resolved_adapter = ADAPTER_TYPE_SERVICE_RESPONSE
        root_key = detected.root_key
        time_key = detected.time_key
        value_key = detected.value_key

    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
        CONF_SERVICE: service_name,
        CONF_ADAPTER_TYPE: resolved_adapter,
        CONF_NAME: root_key,
        CONF_TIME_KEY: time_key,
        CONF_VALUE_KEY: value_key,
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
    return source


def _source_from_entity_adapter_user_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return staged entity adapter config without resolving auto-detect."""
    selected_entities = user_input[CONF_WATTPLAN_ENTITY_ID]
    if isinstance(selected_entities, str):
        entity_ids = [selected_entities]
    else:
        entity_ids = [str(entity_id) for entity_id in selected_entities]

    manual = user_input.get(SECTION_SOURCE_MANUAL, {})
    if not isinstance(manual, dict):
        manual = {}

    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        CONF_WATTPLAN_ENTITY_ID: entity_ids,
        CONF_ADAPTER_TYPE: str(user_input[CONF_ADAPTER_TYPE]),
        CONF_NAME: str(manual.get(CONF_NAME, user_input.get(CONF_NAME, ""))),
        CONF_TIME_KEY: str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, ""))),
        CONF_VALUE_KEY: str(
            manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, ""))
        ),
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
    return source


def _source_from_service_adapter_user_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return staged service adapter config without resolving auto-detect."""
    manual = user_input.get(SECTION_SOURCE_MANUAL, {})
    if not isinstance(manual, dict):
        manual = {}

    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
        CONF_SERVICE: str(user_input[CONF_SERVICE]),
        CONF_ADAPTER_TYPE: str(user_input[CONF_ADAPTER_TYPE]),
        CONF_NAME: str(manual.get(CONF_NAME, user_input.get(CONF_NAME, ""))),
        CONF_TIME_KEY: str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, ""))),
        CONF_VALUE_KEY: str(
            manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, ""))
        ),
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
    return source


async def _async_resolve_source_for_review(
    hass: HomeAssistant,
    *,
    source: dict[str, Any],
    source_input: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve staged source config into the explicit runtime form used for validation."""
    mode = source.get(CONF_SOURCE_MODE)
    adapter_type = source.get(CONF_ADAPTER_TYPE)

    if mode == SOURCE_MODE_ENTITY_ADAPTER and adapter_type == ADAPTER_TYPE_AUTO_DETECT:
        if source_input is None:
            raise SourceProviderError(
                "source_validation",
                "Entity adapter auto detect is missing staged input",
                details={"source_mode": SOURCE_MODE_ENTITY_ADAPTER},
            )
        resolved = await _async_prepare_entity_source_input(hass, source_input)
        return resolved, _auto_detect_step_defaults(source_input, resolved)

    if mode == SOURCE_MODE_SERVICE_ADAPTER and adapter_type == ADAPTER_TYPE_AUTO_DETECT:
        if source_input is None:
            raise SourceProviderError(
                "source_validation",
                "Service adapter auto detect is missing staged input",
                details={"source_mode": SOURCE_MODE_SERVICE_ADAPTER},
            )
        resolved = await _async_prepare_service_source_input(hass, source_input)
        return resolved, _auto_detect_step_defaults(source_input, resolved)

    return source, source_input


class WattPlanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WattPlan."""

    VERSION = 1
    MINOR_VERSION = 1

    _core: dict[str, Any]
    _sources: dict[str, dict[str, Any]]
    _last_source_available_count: int | None = None
    _pending_source_key: str | None = None
    _pending_source: dict[str, Any] | None = None
    _pending_source_input: dict[str, Any] | None = None
    _pending_source_step_id: str | None = None
    _pending_source_summary: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WattPlanOptionsFlow:
        """Return the options flow handler."""
        return WattPlanOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this handler."""
        return {
            SUBENTRY_TYPE_BATTERY: BatterySubentryFlowHandler,
            SUBENTRY_TYPE_COMFORT: ComfortSubentryFlowHandler,
            SUBENTRY_TYPE_OPTIONAL: OptionalSubentryFlowHandler,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show setup requirements before configuration."""
        return await self.async_step_requirements(user_input)

    async def async_step_requirements(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show prerequisites and continue to planner setup."""
        if user_input is not None:
            return await self.async_step_planner_setup()

        return self.async_show_form(
            step_id="requirements",
            data_schema=vol.Schema({}),
            last_step=False,
        )

    async def async_step_planner_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_core_data(user_input, include_name=True)
            if not errors:
                self._core = _normalize_core_input(user_input)
                self._sources = {}
                return await self.async_step_source_price()

        return self.async_show_form(
            step_id="planner_setup",
            data_schema=self.add_suggested_values_to_schema(
                _core_schema(include_name=True), user_input or {}
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the price source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            include_not_used=False,
            step_id="source_price",
        )

    async def async_step_source_export_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the export price source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            include_not_used=True,
            step_id="source_export_price",
        )

    async def async_step_source_usage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the usage source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_USAGE,
            user_input,
            include_not_used=True,
            include_built_in=True,
            step_id="source_usage",
        )

    async def async_step_source_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the PV source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_PV,
            user_input,
            include_not_used=True,
            include_energy_provider=True,
            step_id="source_pv",
        )

    async def _async_step_source_mode(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        include_not_used: bool,
        include_built_in: bool = False,
        include_energy_provider: bool = False,
        step_id: str,
    ) -> ConfigFlowResult:
        """Select source mode and branch to mode specific step."""
        existing = self._sources.get(key, {})
        include_energy_provider_option = await self._async_include_energy_provider_mode(
            existing,
            include_energy_provider=include_energy_provider,
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            mode = user_input[CONF_SOURCE_MODE]
            if mode == SOURCE_MODE_TEMPLATE:
                if key == CONF_SOURCE_IMPORT_PRICE:
                    return await self.async_step_source_price_template()
                if key == CONF_SOURCE_EXPORT_PRICE:
                    return await self.async_step_source_export_price_template()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_template()
                return await self.async_step_source_pv_template()

            if mode == SOURCE_MODE_ENTITY_ADAPTER:
                if key == CONF_SOURCE_IMPORT_PRICE:
                    return await self.async_step_source_price_adapter()
                if key == CONF_SOURCE_EXPORT_PRICE:
                    return await self.async_step_source_export_price_adapter()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_adapter()
                return await self.async_step_source_pv_adapter()

            if mode == SOURCE_MODE_SERVICE_ADAPTER:
                if key == CONF_SOURCE_IMPORT_PRICE:
                    return await self.async_step_source_price_service()
                if key == CONF_SOURCE_EXPORT_PRICE:
                    return await self.async_step_source_export_price_service()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_service()
                return await self.async_step_source_pv_service()

            if include_energy_provider and mode == SOURCE_MODE_ENERGY_PROVIDER:
                return await self.async_step_source_pv_energy_provider()

            if include_built_in and mode == SOURCE_MODE_BUILT_IN:
                return await self.async_step_source_usage_built_in()

            if include_not_used and mode == SOURCE_MODE_NOT_USED:
                self._sources[key] = {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
                return await self._async_after_source_saved(key)

            errors["base"] = "invalid_source_mode"

        default_mode = existing.get(
            CONF_SOURCE_MODE,
            _preferred_source_mode(
                key,
                include_not_used=include_not_used,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider_option,
            ),
        )
        if default_mode == SOURCE_MODE_NOT_USED and not include_not_used:
            default_mode = SOURCE_MODE_TEMPLATE
        if (
            default_mode == SOURCE_MODE_ENERGY_PROVIDER
            and not include_energy_provider_option
        ):
            default_mode = SOURCE_MODE_TEMPLATE

        return self.async_show_form(
            step_id=step_id,
            data_schema=_source_mode_schema(
                default_mode,
                include_not_used=include_not_used,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider_option,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure price source template."""
        return await self._async_step_source_template(
            CONF_SOURCE_IMPORT_PRICE, user_input, step_id="source_price_template"
        )

    async def async_step_source_export_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure export price source template."""
        return await self._async_step_source_template(
            CONF_SOURCE_EXPORT_PRICE, user_input, step_id="source_export_price_template"
        )

    async def async_step_source_usage_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source template."""
        return await self._async_step_source_template(
            CONF_SOURCE_USAGE, user_input, step_id="source_usage_template"
        )

    async def async_step_source_pv_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source template."""
        return await self._async_step_source_template(
            CONF_SOURCE_PV, user_input, step_id="source_pv_template"
        )

    async def _async_step_source_template(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure source using template mode."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                CONF_TEMPLATE: user_input[CONF_TEMPLATE],
                CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
            }
            source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
            return await self._async_prepare_source_review(
                key, source, source_step_id=step_id
            )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_template_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    async def async_step_source_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure price source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_IMPORT_PRICE, user_input, step_id="source_price_adapter"
        )

    async def async_step_source_export_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure export price source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_EXPORT_PRICE, user_input, step_id="source_export_price_adapter"
        )

    async def async_step_source_usage_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_USAGE, user_input, step_id="source_usage_adapter"
        )

    async def async_step_source_pv_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_PV, user_input, step_id="source_pv_adapter"
        )

    async def async_step_source_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure price source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_IMPORT_PRICE, user_input, step_id="source_price_service"
        )

    async def async_step_source_export_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure export price source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_EXPORT_PRICE, user_input, step_id="source_export_price_service"
        )

    async def async_step_source_usage_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_USAGE, user_input, step_id="source_usage_service"
        )

    async def async_step_source_pv_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_PV, user_input, step_id="source_pv_service"
        )

    async def async_step_source_pv_energy_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source from an Energy solar forecast provider."""
        return await self._async_step_source_energy_provider(
            CONF_SOURCE_PV, user_input, step_id="source_pv_energy_provider"
        )

    async def async_step_source_usage_built_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source built-in forecast mode."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(CONF_SOURCE_USAGE)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN,
                CONF_WATTPLAN_ENTITY_ID: user_input[CONF_WATTPLAN_ENTITY_ID],
                CONF_HISTORY_DAYS: int(user_input[CONF_HISTORY_DAYS]),
            }
            return await self._async_prepare_source_review(
                CONF_SOURCE_USAGE,
                source,
                source_step_id="source_usage_built_in",
            )

        return self.async_show_form(
            step_id="source_usage_built_in",
            data_schema=self.add_suggested_values_to_schema(
                _source_built_in_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(
                CONF_SOURCE_USAGE
            ),
            last_step=False,
        )

    async def _async_step_source_adapter(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure source using entity adapter mode."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            errors.update(_validate_source_adapter_input(user_input))
            if not errors:
                return await self._async_prepare_source_review(
                    key,
                    _source_from_entity_adapter_user_input(user_input),
                    source_step_id=step_id,
                    source_input=user_input,
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_adapter_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    async def _async_step_source_service(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure source using service adapter mode."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            errors.update(_validate_service_adapter_input(user_input))
            if not errors:
                return await self._async_prepare_source_review(
                    key,
                    _source_from_service_adapter_user_input(user_input),
                    source_step_id=step_id,
                    source_input=user_input,
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_service_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    async def _async_step_source_energy_provider(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure source using an Energy solar forecast provider."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing
        provider_options = await self._async_energy_provider_options()

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER,
                CONF_CONFIG_ENTRY_ID: user_input[CONF_CONFIG_ENTRY_ID],
                CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
            }
            source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
            if not provider_options:
                errors["base"] = "energy_provider_none_available"
            else:
                return await self._async_prepare_source_review(
                    key, source, source_step_id=step_id
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_energy_provider_schema(
                    {
                        CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
                        CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
                        **existing,
                    },
                    provider_options=provider_options,
                ),
                defaults,
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    def _source_description_placeholders(self, key: str) -> dict[str, str]:
        """Return description placeholders for source steps."""
        source_label = "Price"
        if key == CONF_SOURCE_EXPORT_PRICE:
            source_label = "Export price"
        elif key == CONF_SOURCE_USAGE:
            source_label = "Usage"
        elif key == CONF_SOURCE_PV:
            source_label = "PV"

        return {
            "source_label": source_label,
            "required_count": str(_expected_slots(self._core)),
            "available_count": str(self._last_source_available_count or 0),
            "slot_minutes": str(self._core[CONF_SLOT_MINUTES]),
        }

    async def _async_energy_provider_options(self) -> list[selector.SelectOptionDict]:
        """Return Energy solar forecast providers as selector options."""
        entries = await async_get_energy_solar_forecast_entries(self.hass)
        return [
            selector.SelectOptionDict(value=entry.entry_id, label=entry.title)
            for entry in entries
        ]

    async def _async_include_energy_provider_mode(
        self,
        existing: dict[str, Any],
        *,
        include_energy_provider: bool,
    ) -> bool:
        """Return whether Energy provider should be offered in source mode."""
        if not include_energy_provider:
            return False
        if existing.get(CONF_SOURCE_MODE) == SOURCE_MODE_ENERGY_PROVIDER:
            return True
        return bool(await self._async_energy_provider_options())

    def _source_step_defaults(self, key: str) -> dict[str, Any]:
        """Return defaults for the active source input step."""
        if self._pending_source_key == key:
            if self._pending_source_input is not None:
                return _source_base_defaults(self._pending_source_input)
            if self._pending_source is not None:
                return _source_base_defaults(self._pending_source)
        return _source_base_defaults(self._sources.get(key, {}))

    async def _async_prepare_source_review(
        self,
        key: str,
        source: dict[str, Any],
        *,
        source_step_id: str,
        source_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Stage one source for review before saving it."""
        self._pending_source_key = key
        self._pending_source = source
        self._pending_source_input = source_input
        self._pending_source_step_id = source_step_id
        self._pending_source_summary = await _async_source_summary(
            self.hass,
            core_data=self._core,
            key=key,
            source=source,
            source_input=source_input,
            floor_to_slot=self._floor_to_slot,
            validate_built_in_entity=self._validate_built_in_usage_entity,
        )
        self._pending_source_input = self._pending_source_summary.get(
            "resolved_source_input", source_input
        )
        return await self.async_step_source_review()

    async def async_step_source_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review and confirm one staged source."""
        if self._pending_source_key is None or self._pending_source is None:
            return await self.async_step_source_price()

        key = self._pending_source_key
        pending = dict(self._pending_source)
        summary = self._pending_source_summary or {}
        resolved_pending = dict(summary.get("resolved_source", pending))
        is_valid = bool(summary.get("is_valid", False))
        defaults = {
            CONF_ACCEPT_SOURCE_SUMMARY: is_valid,
        }
        errors: dict[str, str] = (
            {"base": str(summary["error_key"])}
            if summary.get("error_key") is not None
            else {}
        )
        if user_input is not None:
            if not is_valid:
                return await self._async_return_to_pending_source_step()
            if not user_input[CONF_ACCEPT_SOURCE_SUMMARY]:
                return await self._async_return_to_pending_source_step()
            try:
                await self._async_validate_source(key, resolved_pending)
            except vol.Invalid as err:
                errors["base"] = str(err)
                self._pending_source_summary = await _async_source_summary(
                    self.hass,
                    core_data=self._core,
                    key=key,
                    source=pending,
                    source_input=self._pending_source_input,
                    floor_to_slot=self._floor_to_slot,
                    validate_built_in_entity=self._validate_built_in_usage_entity,
                )
                summary = self._pending_source_summary or {}
                resolved_pending = dict(summary.get("resolved_source", pending))
                is_valid = bool(summary.get("is_valid", False))
                defaults[CONF_ACCEPT_SOURCE_SUMMARY] = is_valid
            else:
                self._sources[key] = resolved_pending
                self._pending_source_key = None
                self._pending_source = None
                self._pending_source_input = None
                self._pending_source_step_id = None
                self._pending_source_summary = None
                return await self._async_after_source_saved(key)

        accept_note = await _async_config_translation(
            self.hass,
            "review_accept_note_valid" if is_valid else "review_accept_note_invalid",
        )

        return self.async_show_form(
            step_id="source_review",
            data_schema=self.add_suggested_values_to_schema(
                _source_review_schema(defaults, include_accept=is_valid),
                user_input or {},
            ),
            errors=errors,
            description_placeholders={
                **self._source_description_placeholders(key),
                "raw_coverage_start": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="raw_coverage_start",
                ),
                "raw_coverage_end": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="raw_coverage_end",
                ),
                "raw_coverage_summary": str(summary.get("raw_coverage_summary", "")),
                "adjusted_coverage_start": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="coverage_start",
                ),
                "adjusted_coverage_end": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="coverage_end",
                ),
                "adjusted_coverage_summary": str(summary.get("coverage_summary", "")),
                "review_text": str(summary.get("review_text", "")),
                "diagnostic_text": str(summary.get("diagnostic_text", "")),
                "accept_note": accept_note,
            },
            last_step=self._is_final_source_step(key),
        )

    async def _async_return_to_pending_source_step(self) -> ConfigFlowResult:
        """Return to the staged source input step."""
        if self._pending_source_step_id is None:
            return await self.async_step_source_price()
        return await getattr(self, f"async_step_{self._pending_source_step_id}")()

    async def _async_after_source_saved(self, key: str) -> ConfigFlowResult:
        """Continue to the next source or create the config entry."""
        if key == CONF_SOURCE_IMPORT_PRICE:
            return await self.async_step_source_usage()
        if key == CONF_SOURCE_USAGE:
            return await self.async_step_source_pv()
        if key == CONF_SOURCE_PV:
            pv_source = self._sources.get(CONF_SOURCE_PV, {})
            if pv_source.get(CONF_SOURCE_MODE) == SOURCE_MODE_NOT_USED:
                return await self.async_step_setup_complete()
            return await self.async_step_source_export_price()
        return await self.async_step_setup_complete()

    def _is_final_source_step(self, key: str) -> bool:
        """Return if the source config step is the last one before create."""
        if key == CONF_SOURCE_PV:
            pv_source = self._sources.get(CONF_SOURCE_PV, {})
            return pv_source.get(CONF_SOURCE_MODE) == SOURCE_MODE_NOT_USED
        return key == CONF_SOURCE_EXPORT_PRICE

    async def async_step_setup_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show one final setup summary and next actions before entry creation."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._core[CONF_NAME],
                data={
                    **{key: value for key, value in self._core.items() if key != CONF_NAME},
                    CONF_SOURCES: self._sources,
                },
                options={
                    CONF_PLANNING_ENABLED: True,
                    CONF_ACTION_EMISSION_ENABLED: True,
                },
            )

        return self.async_show_form(
            step_id="setup_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={
                "setup_name": str(self._core[CONF_NAME]),
                "slot_minutes": str(self._core[CONF_SLOT_MINUTES]),
                "plan_hours": str(self._core[CONF_HOURS_TO_PLAN]),
                "price_source": _source_mode_summary(
                    self._sources.get(CONF_SOURCE_IMPORT_PRICE)
                ),
                "export_price_source": _source_mode_summary(
                    self._sources.get(CONF_SOURCE_EXPORT_PRICE)
                ),
                "usage_source": _source_mode_summary(
                    self._sources.get(CONF_SOURCE_USAGE)
                ),
                "pv_source": _source_mode_summary(self._sources.get(CONF_SOURCE_PV)),
            },
            last_step=True,
        )

    async def _async_validate_source(self, key: str, source: dict[str, Any]) -> None:
        """Validate source config against the current horizon."""
        try:
            self._last_source_available_count = await _async_validate_source_values(
                self.hass,
                core_data=self._core,
                key=key,
                source=source,
                floor_to_slot=self._floor_to_slot,
                validate_built_in_entity=self._validate_built_in_usage_entity,
            )
        except SourceProviderError as err:
            if available_count := err.details.get("available_count"):
                self._last_source_available_count = int(available_count)
            raise vol.Invalid(_invalid_key_from_source_error(err)) from err

    def _validate_built_in_usage_entity(self, entity_id: str) -> None:
        """Validate built-in usage entity metadata before forecasting."""
        state = self.hass.states.get(entity_id)
        if state is None:
            raise vol.Invalid("entity_not_found")
        if state.attributes.get("device_class") != "energy":
            raise vol.Invalid("built_in_requires_energy_kwh")
        if state.attributes.get("unit_of_measurement") != "kWh":
            raise vol.Invalid("built_in_requires_energy_kwh")

    def _floor_to_slot(self, value: datetime, slot_minutes: int) -> datetime:
        """Floor datetime down to nearest slot boundary."""
        seconds = int(value.timestamp())
        slot_seconds = slot_minutes * 60
        floored = (seconds // slot_seconds) * slot_seconds
        return datetime.fromtimestamp(floored, tz=UTC)


class WattPlanOptionsFlow(OptionsFlowWithReload):
    """Handle WattPlan options flow."""

    _data: dict[str, Any]
    _options: dict[str, Any]
    _selected_subentry_id: str | None
    _last_source_available_count: int | None
    _pending_source_key: str | None
    _pending_source: dict[str, Any] | None
    _pending_source_input: dict[str, Any] | None
    _pending_source_step_id: str | None
    _pending_source_summary: dict[str, Any] | None

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._data = deepcopy(dict(config_entry.data))
        self._options = deepcopy(dict(config_entry.options))
        self._options.setdefault(CONF_PLANNING_ENABLED, True)
        self._options.setdefault(CONF_ACTION_EMISSION_ENABLED, True)
        self._selected_subentry_id = None
        self._last_source_available_count = None
        self._pending_source_key = None
        self._pending_source = None
        self._pending_source_input = None
        self._pending_source_step_id = None
        self._pending_source_summary = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu for options."""
        menu_options = [
            "planner_core",
            "source_price",
            "source_usage",
            "source_pv",
            "source_export_price",
        ]
        menu_options.append("planner_timers")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_planner_core(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit core values."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_core_data(user_input)
            if not errors:
                self._data.update(_normalize_core_input(user_input))
                self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
                return await self.async_step_init()

        return self.async_show_form(
            step_id="planner_core",
            data_schema=self.add_suggested_values_to_schema(
                _core_schema(self._data), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_planner_timers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure timer behavior flags."""
        if user_input is not None:
            self._options.update(user_input)
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=self._options
            )
            return await self.async_step_init()

        return self.async_show_form(
            step_id="planner_timers",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PLANNING_ENABLED,
                        default=self._options[CONF_PLANNING_ENABLED],
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_ACTION_EMISSION_ENABLED,
                        default=self._options[CONF_ACTION_EMISSION_ENABLED],
                    ): selector.BooleanSelector(),
                }
            ),
            description_placeholders={
                "slot_minutes": str(self._data[CONF_SLOT_MINUTES])
            },
        )

    async def async_step_battery_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show battery edit/remove actions."""
        if not self._subentries_by_type(SUBENTRY_TYPE_BATTERY):
            return self.async_abort(reason="nothing_configured")
        return self.async_show_menu(
            step_id="battery_entities",
            menu_options=["battery_edit_select", "battery_remove_select", "init"],
        )

    async def async_step_comfort_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show comfort edit/remove actions."""
        if not self._subentries_by_type(SUBENTRY_TYPE_COMFORT):
            return self.async_abort(reason="nothing_configured")
        return self.async_show_menu(
            step_id="comfort_entities",
            menu_options=["comfort_edit_select", "comfort_remove_select", "init"],
        )

    async def async_step_optional_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show optional edit/remove actions."""
        if not self._subentries_by_type(SUBENTRY_TYPE_OPTIONAL):
            return self.async_abort(reason="nothing_configured")
        return self.async_show_menu(
            step_id="optional_entities",
            menu_options=["optional_edit_select", "optional_remove_select", "init"],
        )

    async def async_step_source_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the price source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            include_not_used=False,
            step_id="source_price",
        )

    async def async_step_source_export_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the export price source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            include_not_used=True,
            step_id="source_export_price",
        )

    async def async_step_source_usage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the usage source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_USAGE,
            user_input,
            include_not_used=True,
            include_built_in=True,
            step_id="source_usage",
        )

    async def async_step_source_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the PV source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_PV,
            user_input,
            include_not_used=True,
            include_energy_provider=True,
            step_id="source_pv",
        )

    async def _async_step_source_options_mode(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        include_not_used: bool,
        include_built_in: bool = False,
        include_energy_provider: bool = False,
        step_id: str,
    ) -> ConfigFlowResult:
        """Select source mode for options flow and branch."""
        errors: dict[str, str] = {}
        sources = self._data.get(CONF_SOURCES, {})
        existing = sources.get(key, {})
        include_energy_provider_option = await self._async_include_energy_provider_mode(
            existing,
            include_energy_provider=include_energy_provider,
        )

        if user_input is not None:
            mode = user_input[CONF_SOURCE_MODE]

            if mode == SOURCE_MODE_TEMPLATE:
                if key == CONF_SOURCE_IMPORT_PRICE:
                    return await self.async_step_source_price_template()
                if key == CONF_SOURCE_EXPORT_PRICE:
                    return await self.async_step_source_export_price_template()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_template()
                return await self.async_step_source_pv_template()

            if mode == SOURCE_MODE_ENTITY_ADAPTER:
                if key == CONF_SOURCE_IMPORT_PRICE:
                    return await self.async_step_source_price_adapter()
                if key == CONF_SOURCE_EXPORT_PRICE:
                    return await self.async_step_source_export_price_adapter()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_adapter()
                return await self.async_step_source_pv_adapter()

            if mode == SOURCE_MODE_SERVICE_ADAPTER:
                if key == CONF_SOURCE_IMPORT_PRICE:
                    return await self.async_step_source_price_service()
                if key == CONF_SOURCE_EXPORT_PRICE:
                    return await self.async_step_source_export_price_service()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_service()
                return await self.async_step_source_pv_service()

            if include_energy_provider and mode == SOURCE_MODE_ENERGY_PROVIDER:
                return await self.async_step_source_pv_energy_provider()

            if include_built_in and mode == SOURCE_MODE_BUILT_IN:
                return await self.async_step_source_usage_built_in()

            if include_not_used and mode == SOURCE_MODE_NOT_USED:
                sources[key] = {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
                self._data[CONF_SOURCES] = sources
                self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
                return await self.async_step_init()

            errors["base"] = "invalid_source_mode"

        return self.async_show_form(
            step_id=step_id,
            data_schema=_source_mode_schema(
                existing.get(
                    CONF_SOURCE_MODE,
                    _preferred_source_mode(
                        key,
                        include_not_used=include_not_used,
                        include_built_in=include_built_in,
                        include_energy_provider=include_energy_provider_option,
                    ),
                )
                if (
                    existing.get(CONF_SOURCE_MODE) != SOURCE_MODE_ENERGY_PROVIDER
                    or include_energy_provider_option
                )
                else SOURCE_MODE_TEMPLATE,
                include_not_used=include_not_used,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider_option,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit price source template in options."""
        return await self._async_step_source_template_options(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            step_id="source_price_template",
        )

    async def async_step_source_export_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit export price source template in options."""
        return await self._async_step_source_template_options(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            step_id="source_export_price_template",
        )

    async def async_step_source_usage_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source template in options."""
        return await self._async_step_source_template_options(
            CONF_SOURCE_USAGE,
            user_input,
            step_id="source_usage_template",
        )

    async def async_step_source_pv_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV source template in options."""
        return await self._async_step_source_template_options(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_template",
        )

    async def _async_step_source_template_options(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Edit one source in template mode from options."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                CONF_TEMPLATE: user_input[CONF_TEMPLATE],
                CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
            }
            source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
            return await self._async_prepare_source_review(
                key, source, source_step_id=step_id
            )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_template_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    async def async_step_source_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit price source adapter in options."""
        return await self._async_step_source_adapter_options(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            step_id="source_price_adapter",
        )

    async def async_step_source_export_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit export price source adapter in options."""
        return await self._async_step_source_adapter_options(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            step_id="source_export_price_adapter",
        )

    async def async_step_source_usage_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source adapter in options."""
        return await self._async_step_source_adapter_options(
            CONF_SOURCE_USAGE,
            user_input,
            step_id="source_usage_adapter",
        )

    async def async_step_source_pv_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV source adapter in options."""
        return await self._async_step_source_adapter_options(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_adapter",
        )

    async def async_step_source_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit price source service adapter in options."""
        return await self._async_step_source_service_options(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            step_id="source_price_service",
        )

    async def async_step_source_export_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit export price source service adapter in options."""
        return await self._async_step_source_service_options(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            step_id="source_export_price_service",
        )

    async def async_step_source_usage_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source service adapter in options."""
        return await self._async_step_source_service_options(
            CONF_SOURCE_USAGE,
            user_input,
            step_id="source_usage_service",
        )

    async def async_step_source_pv_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV source service adapter in options."""
        return await self._async_step_source_service_options(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_service",
        )

    async def async_step_source_pv_energy_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV Energy solar forecast provider in options."""
        return await self._async_step_source_energy_provider_options(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_energy_provider",
        )

    async def async_step_source_usage_built_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source built-in forecast mode in options."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(CONF_SOURCE_USAGE)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN,
                CONF_WATTPLAN_ENTITY_ID: user_input[CONF_WATTPLAN_ENTITY_ID],
                CONF_HISTORY_DAYS: int(user_input[CONF_HISTORY_DAYS]),
            }
            return await self._async_prepare_source_review(
                CONF_SOURCE_USAGE,
                source,
                source_step_id="source_usage_built_in",
            )

        return self.async_show_form(
            step_id="source_usage_built_in",
            data_schema=self.add_suggested_values_to_schema(
                _source_built_in_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(
                CONF_SOURCE_USAGE
            ),
            last_step=False,
        )

    async def _async_step_source_adapter_options(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Edit one source in entity adapter mode from options."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            errors.update(_validate_source_adapter_input(user_input))
            if not errors:
                return await self._async_prepare_source_review(
                    key,
                    _source_from_entity_adapter_user_input(user_input),
                    source_step_id=step_id,
                    source_input=user_input,
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_adapter_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    async def _async_step_source_service_options(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Edit one source in service adapter mode from options."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            errors.update(_validate_service_adapter_input(user_input))
            if not errors:
                return await self._async_prepare_source_review(
                    key,
                    _source_from_service_adapter_user_input(user_input),
                    source_step_id=step_id,
                    source_input=user_input,
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_service_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    async def _async_step_source_energy_provider_options(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Edit one source in Energy provider mode from options."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing
        provider_options = await self._async_energy_provider_options()

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER,
                CONF_CONFIG_ENTRY_ID: user_input[CONF_CONFIG_ENTRY_ID],
                CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
            }
            source.update(user_input.get(SECTION_SOURCE_ADVANCED, {}))
            if not provider_options:
                errors["base"] = "energy_provider_none_available"
            else:
                return await self._async_prepare_source_review(
                    key, source, source_step_id=step_id
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_energy_provider_schema(
                    {
                        CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
                        CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
                        **existing,
                    },
                    provider_options=provider_options,
                ),
                defaults,
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    def _source_description_placeholders(self, key: str) -> dict[str, str]:
        """Return description placeholders for source steps."""
        source_label = "Price"
        if key == CONF_SOURCE_EXPORT_PRICE:
            source_label = "Export price"
        elif key == CONF_SOURCE_USAGE:
            source_label = "Usage"
        elif key == CONF_SOURCE_PV:
            source_label = "PV"

        return {
            "source_label": source_label,
            "required_count": str(_expected_slots(self._data)),
            "available_count": str(self._last_source_available_count or 0),
            "slot_minutes": str(self._data[CONF_SLOT_MINUTES]),
        }

    async def _async_energy_provider_options(self) -> list[selector.SelectOptionDict]:
        """Return Energy solar forecast providers as selector options."""
        entries = await async_get_energy_solar_forecast_entries(self.hass)
        return [
            selector.SelectOptionDict(value=entry.entry_id, label=entry.title)
            for entry in entries
        ]

    async def _async_include_energy_provider_mode(
        self,
        existing: dict[str, Any],
        *,
        include_energy_provider: bool,
    ) -> bool:
        """Return whether Energy provider should be offered in source mode."""
        if not include_energy_provider:
            return False
        if existing.get(CONF_SOURCE_MODE) == SOURCE_MODE_ENERGY_PROVIDER:
            return True
        return bool(await self._async_energy_provider_options())

    def _source_step_defaults(self, key: str) -> dict[str, Any]:
        """Return defaults for the active source input step."""
        if self._pending_source_key == key:
            if self._pending_source_input is not None:
                return _source_base_defaults(self._pending_source_input)
            if self._pending_source is not None:
                return _source_base_defaults(self._pending_source)
        return _source_base_defaults(self._data.get(CONF_SOURCES, {}).get(key, {}))

    async def _async_prepare_source_review(
        self,
        key: str,
        source: dict[str, Any],
        *,
        source_step_id: str,
        source_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Stage one source for review before saving it."""
        self._pending_source_key = key
        self._pending_source = source
        self._pending_source_input = source_input
        self._pending_source_step_id = source_step_id
        self._pending_source_summary = await _async_source_summary(
            self.hass,
            core_data=self._data,
            key=key,
            source=source,
            source_input=source_input,
            floor_to_slot=self._floor_to_slot,
            validate_built_in_entity=self._validate_built_in_usage_entity,
        )
        self._pending_source_input = self._pending_source_summary.get(
            "resolved_source_input", source_input
        )
        return await self.async_step_source_review()

    async def async_step_source_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review and confirm one staged source in options flow."""
        if self._pending_source_key is None or self._pending_source is None:
            return await self.async_step_init()

        key = self._pending_source_key
        pending = dict(self._pending_source)
        summary = self._pending_source_summary or {}
        resolved_pending = dict(summary.get("resolved_source", pending))
        is_valid = bool(summary.get("is_valid", False))
        defaults = {
            CONF_ACCEPT_SOURCE_SUMMARY: is_valid,
        }
        errors: dict[str, str] = (
            {"base": str(summary["error_key"])}
            if summary.get("error_key") is not None
            else {}
        )
        if user_input is not None:
            if not is_valid:
                return await self._async_return_to_pending_source_step()
            if not user_input[CONF_ACCEPT_SOURCE_SUMMARY]:
                return await self._async_return_to_pending_source_step()
            try:
                await self._async_validate_source(key, resolved_pending)
            except vol.Invalid as err:
                errors["base"] = str(err)
                self._pending_source_summary = await _async_source_summary(
                    self.hass,
                    core_data=self._data,
                    key=key,
                    source=pending,
                    source_input=self._pending_source_input,
                    floor_to_slot=self._floor_to_slot,
                    validate_built_in_entity=self._validate_built_in_usage_entity,
                )
                summary = self._pending_source_summary or {}
                resolved_pending = dict(summary.get("resolved_source", pending))
                is_valid = bool(summary.get("is_valid", False))
                defaults[CONF_ACCEPT_SOURCE_SUMMARY] = is_valid
            else:
                sources = dict(self._data.get(CONF_SOURCES, {}))
                sources[key] = resolved_pending
                self._data[CONF_SOURCES] = sources
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=self._data
                )
                self._pending_source_key = None
                self._pending_source = None
                self._pending_source_input = None
                self._pending_source_step_id = None
                self._pending_source_summary = None
                return await self.async_step_init()

        accept_note = await _async_config_translation(
            self.hass,
            "review_accept_note_valid" if is_valid else "review_accept_note_invalid",
        )

        return self.async_show_form(
            step_id="source_review",
            data_schema=self.add_suggested_values_to_schema(
                _source_review_schema(defaults, include_accept=is_valid),
                user_input or {},
            ),
            errors=errors,
            description_placeholders={
                **self._source_description_placeholders(key),
                "raw_coverage_start": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="raw_coverage_start",
                ),
                "raw_coverage_end": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="raw_coverage_end",
                ),
                "raw_coverage_summary": str(summary.get("raw_coverage_summary", "")),
                "adjusted_coverage_start": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="coverage_start",
                ),
                "adjusted_coverage_end": await _coverage_placeholder_text(
                    self.hass,
                    summary=summary,
                    timezone_name=self.hass.config.time_zone,
                    field="coverage_end",
                ),
                "adjusted_coverage_summary": str(summary.get("coverage_summary", "")),
                "review_text": str(summary.get("review_text", "")),
                "diagnostic_text": str(summary.get("diagnostic_text", "")),
                "accept_note": accept_note,
            },
        )

    async def _async_return_to_pending_source_step(self) -> ConfigFlowResult:
        """Return to the staged source input step."""
        if self._pending_source_step_id is None:
            return await self.async_step_init()
        return await getattr(self, f"async_step_{self._pending_source_step_id}")()

    async def async_step_battery_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select battery subentry to edit."""
        return await self._async_select_item(
            SUBENTRY_TYPE_BATTERY,
            "battery_edit_select",
            self.async_step_battery_edit,
            user_input,
        )

    async def async_step_comfort_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select comfort subentry to edit."""
        return await self._async_select_item(
            SUBENTRY_TYPE_COMFORT,
            "comfort_edit_select",
            self.async_step_comfort_edit,
            user_input,
        )

    async def async_step_optional_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select optional subentry to edit."""
        return await self._async_select_item(
            SUBENTRY_TYPE_OPTIONAL,
            "optional_edit_select",
            self.async_step_optional_edit,
            user_input,
        )

    async def _async_select_item(
        self,
        subentry_type: str,
        step_id: str,
        next_step,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Select a subentry from a configured subentry type."""
        items = self._subentries_by_type(subentry_type)
        if not items:
            return self.async_abort(reason="nothing_configured")
        if user_input is not None:
            self._selected_subentry_id = user_input["item_id"]
            return await next_step()
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("item_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=item.subentry_id,
                                    label=item.title,
                                )
                                for item in items
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_battery_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit selected battery."""
        return await self._async_edit_subentry(
            SUBENTRY_TYPE_BATTERY,
            "battery_edit",
            _battery_schema,
            _validate_battery_data,
            user_input,
        )

    async def async_step_comfort_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit selected comfort subentry."""
        return await self._async_edit_subentry(
            SUBENTRY_TYPE_COMFORT,
            "comfort_edit",
            _comfort_schema,
            _validate_comfort_data,
            user_input,
        )

    async def async_step_optional_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit selected optional subentry."""
        return await self._async_edit_subentry(
            SUBENTRY_TYPE_OPTIONAL,
            "optional_edit",
            _optional_schema,
            _validate_optional_data,
            user_input,
        )

    async def _async_edit_subentry(
        self,
        subentry_type: str,
        step_id: str,
        schema_factory,
        validate_method,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Edit one selected subentry."""
        if self._selected_subentry_id is None:
            return self.async_abort(reason="nothing_configured")
        subentry = self.config_entry.subentries[self._selected_subentry_id]
        errors: dict[str, str] = {}
        defaults = dict(subentry.data)

        if user_input is not None:
            defaults = user_input
            if self._name_in_use(user_input[CONF_NAME], exclude_subentry_id=subentry.subentry_id):
                errors["base"] = "name_not_unique"
            else:
                errors.update(validate_method(user_input))
            if not errors:
                self.hass.config_entries.async_update_subentry(
                    self.config_entry,
                    subentry,
                    data=user_input,
                    title=_subentry_display_title(subentry_type, user_input),
                    unique_id=f"{subentry_type}:{_normalize_name(user_input[CONF_NAME])}",
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema_factory(), defaults),
            errors=errors,
            description_placeholders=(
                {"slot_minutes": str(self._data[CONF_SLOT_MINUTES])}
                if subentry_type == SUBENTRY_TYPE_COMFORT
                else None
            ),
        )

    async def async_step_battery_remove_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select battery subentry to remove."""
        return await self._async_remove_item(
            SUBENTRY_TYPE_BATTERY,
            "battery_remove_select",
            user_input,
        )

    async def async_step_comfort_remove_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select comfort subentry to remove."""
        return await self._async_remove_item(
            SUBENTRY_TYPE_COMFORT,
            "comfort_remove_select",
            user_input,
        )

    async def async_step_optional_remove_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select optional subentry to remove."""
        return await self._async_remove_item(
            SUBENTRY_TYPE_OPTIONAL,
            "optional_remove_select",
            user_input,
        )

    async def _async_remove_item(
        self, subentry_type: str, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Remove subentry from selected type."""
        items = self._subentries_by_type(subentry_type)
        if not items:
            return self.async_abort(reason="nothing_configured")
        if user_input is not None:
            self.hass.config_entries.async_remove_subentry(
                self.config_entry,
                user_input["item_id"],
            )
            return await self.async_step_init()

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("item_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=item.subentry_id,
                                    label=item.title,
                                )
                                for item in items
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    def _subentries_by_type(self, subentry_type: str) -> list[Any]:
        """Return subentries filtered by type."""
        return [
            subentry
            for subentry in self.config_entry.subentries.values()
            if subentry.subentry_type == subentry_type
        ]

    def _name_in_use(self, name: str, *, exclude_subentry_id: str | None = None) -> bool:
        """Return if a subentry title is already in use."""
        wanted = name.casefold()
        for subentry in self.config_entry.subentries.values():
            if subentry.subentry_id == exclude_subentry_id:
                continue
            if _subentry_name(subentry).casefold() == wanted:
                return True
        return False

    async def _async_validate_source(self, key: str, source: dict[str, Any]) -> None:
        """Validate source config against updated option state."""
        try:
            self._last_source_available_count = await _async_validate_source_values(
                self.hass,
                core_data=self._data,
                key=key,
                source=source,
                floor_to_slot=self._floor_to_slot,
                validate_built_in_entity=self._validate_built_in_usage_entity,
            )
        except SourceProviderError as err:
            if available_count := err.details.get("available_count"):
                self._last_source_available_count = int(available_count)
            raise vol.Invalid(_invalid_key_from_source_error(err)) from err

    def _validate_built_in_usage_entity(self, entity_id: str) -> None:
        """Validate built-in usage entity metadata before forecasting."""
        state = self.hass.states.get(entity_id)
        if state is None:
            raise vol.Invalid("entity_not_found")
        if state.attributes.get("device_class") != "energy":
            raise vol.Invalid("built_in_requires_energy_kwh")
        if state.attributes.get("unit_of_measurement") != "kWh":
            raise vol.Invalid("built_in_requires_energy_kwh")

    def _floor_to_slot(self, value: datetime, slot_minutes: int) -> datetime:
        """Floor datetime down to nearest slot boundary."""
        seconds = int(value.timestamp())
        slot_seconds = slot_minutes * 60
        floored = (seconds // slot_seconds) * slot_seconds
        return datetime.fromtimestamp(floored, tz=UTC)



def _subentry_name_in_use(entry: ConfigEntry, name: str) -> bool:
    """Return True if the name is already used by a subentry."""
    wanted = name.casefold()
    return any(
        _subentry_name(subentry).casefold() == wanted
        for subentry in entry.subentries.values()
    )


def _subentry_name_in_use_excluding(
    entry: ConfigEntry, name: str, exclude_subentry_id: str
) -> bool:
    """Return True if the name is used by another subentry."""
    wanted = name.casefold()
    return any(
        subentry.subentry_id != exclude_subentry_id
        and _subentry_name(subentry).casefold() == wanted
        for subentry in entry.subentries.values()
    )


def _validate_battery_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate battery values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    if float(data[CONF_MINIMUM_KWH]) > float(data[CONF_CAPACITY_KWH]):
        errors[CONF_MINIMUM_KWH] = "battery_minimum_exceeds_capacity"
    for field in (CONF_CHARGE_EFFICIENCY, CONF_DISCHARGE_EFFICIENCY):
        if not 0 < float(data[field]) <= 1:
            errors[field] = "battery_efficiency_invalid"
    return errors


def _normalize_battery_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten battery advanced settings into subentry data."""
    data = dict(user_input)
    data.update(data.pop(SECTION_BATTERY_ADVANCED, {}))
    data.setdefault(CONF_CHARGE_EFFICIENCY, 0.9)
    data.setdefault(CONF_DISCHARGE_EFFICIENCY, 0.9)
    return data


def _battery_form_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Return battery defaults shaped for the form schema."""
    defaults = dict(data)
    defaults[SECTION_BATTERY_ADVANCED] = {
        CONF_CHARGE_EFFICIENCY: defaults.get(CONF_CHARGE_EFFICIENCY, 0.9),
        CONF_DISCHARGE_EFFICIENCY: defaults.get(CONF_DISCHARGE_EFFICIENCY, 0.9),
    }
    return defaults


def _validate_comfort_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate comfort values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    rolling_window_hours = float(data[CONF_ROLLING_WINDOW_HOURS])
    rolling_window_minutes = int(rolling_window_hours * 60)
    if float(data[CONF_TARGET_ON_HOURS_PER_WINDOW]) > rolling_window_hours:
        errors[CONF_TARGET_ON_HOURS_PER_WINDOW] = "comfort_target_on_hours_invalid"
    if int(data[CONF_MIN_CONSECUTIVE_ON_MINUTES]) > rolling_window_minutes:
        errors[CONF_MIN_CONSECUTIVE_ON_MINUTES] = "comfort_duration_exceeds_window"
    if int(data[CONF_MIN_CONSECUTIVE_OFF_MINUTES]) > rolling_window_minutes:
        errors[CONF_MIN_CONSECUTIVE_OFF_MINUTES] = "comfort_duration_exceeds_window"
    if int(data[CONF_MAX_CONSECUTIVE_OFF_MINUTES]) > rolling_window_minutes:
        errors[CONF_MAX_CONSECUTIVE_OFF_MINUTES] = "comfort_duration_exceeds_window"
    if float(data[CONF_EXPECTED_POWER_KW]) <= 0:
        errors[CONF_EXPECTED_POWER_KW] = "comfort_expected_power_invalid"
    return errors


def _validate_optional_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate optional load values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    energy_kwh = data.get(CONF_ENERGY_KWH)
    if energy_kwh is None:
        errors[CONF_ENERGY_KWH] = "energy_kwh_required"
    elif float(energy_kwh) <= 0:
        errors[CONF_ENERGY_KWH] = "optional_energy_must_be_positive"

    duration_minutes = int(data[CONF_DURATION_MINUTES])
    run_within_minutes = int(data[CONF_RUN_WITHIN_HOURS] * 60)
    min_gap_minutes = int(data[CONF_MIN_OPTION_GAP_MINUTES])
    options_count = int(data[CONF_OPTIONS_COUNT])

    if duration_minutes > run_within_minutes:
        errors[CONF_DURATION_MINUTES] = "optional_duration_exceeds_window"
        return errors

    max_options = _optional_max_distinct_options(
        run_within_minutes, duration_minutes, min_gap_minutes
    )
    if options_count > max_options:
        errors[CONF_OPTIONS_COUNT] = "optional_options_exceed_window"

    return errors


class BatterySubentryFlowHandler(ConfigSubentryFlow):
    """Handle battery subentry flow."""

    _pending_input: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create a battery subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized_input = _normalize_battery_input(user_input)
            if _subentry_name_in_use(self._get_entry(), normalized_input[CONF_NAME]):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_battery_data(normalized_input))
            if not errors:
                self._pending_input = normalized_input
                return await self.async_step_complete()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _battery_schema(), _battery_form_defaults(user_input or {})
            ),
            errors=errors,
        )

    async def async_step_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after creating a battery."""
        if self._pending_input is None:
            return await self.async_step_user()
        if user_input is not None:
            pending = self._pending_input
            self._pending_input = None
            return self.async_create_entry(
                title=_subentry_display_title(SUBENTRY_TYPE_BATTERY, pending),
                data=pending,
                unique_id=f"{SUBENTRY_TYPE_BATTERY}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure a battery subentry."""
        errors: dict[str, str] = {}
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)

        if user_input is not None:
            normalized_input = _normalize_battery_input(user_input)
            if _subentry_name_in_use_excluding(
                self._get_entry(), normalized_input[CONF_NAME], subentry.subentry_id
            ):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_battery_data(normalized_input))

            if not errors:
                self._pending_input = normalized_input
                return await self.async_step_reconfigure_complete()
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _battery_schema(), _battery_form_defaults(defaults)
            ),
            errors=errors,
        )

    async def async_step_reconfigure_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after editing a battery."""
        if self._pending_input is None:
            return await self.async_step_reconfigure()
        if user_input is not None:
            subentry = self._get_reconfigure_subentry()
            pending = self._pending_input
            self._pending_input = None
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                data=pending,
                title=_subentry_display_title(SUBENTRY_TYPE_BATTERY, pending),
                unique_id=f"{SUBENTRY_TYPE_BATTERY}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="reconfigure_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )


class ComfortSubentryFlowHandler(ConfigSubentryFlow):
    """Handle comfort subentry flow."""

    _pending_input: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create a comfort subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _subentry_name_in_use(self._get_entry(), user_input[CONF_NAME]):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_comfort_data(user_input))
            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_complete()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _comfort_schema(), user_input or {}
            ),
            errors=errors,
            description_placeholders={
                "slot_minutes": str(self._get_entry().data[CONF_SLOT_MINUTES])
            },
        )

    async def async_step_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after creating a comfort load."""
        if self._pending_input is None:
            return await self.async_step_user()
        if user_input is not None:
            pending = self._pending_input
            self._pending_input = None
            return self.async_create_entry(
                title=_subentry_display_title(SUBENTRY_TYPE_COMFORT, pending),
                data=pending,
                unique_id=f"{SUBENTRY_TYPE_COMFORT}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure a comfort subentry."""
        errors: dict[str, str] = {}
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)

        if user_input is not None:
            if _subentry_name_in_use_excluding(
                self._get_entry(), user_input[CONF_NAME], subentry.subentry_id
            ):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_comfort_data(user_input))

            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_reconfigure_complete()
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_comfort_schema(), defaults),
            errors=errors,
            description_placeholders={
                "slot_minutes": str(self._get_entry().data[CONF_SLOT_MINUTES])
            },
        )

    async def async_step_reconfigure_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after editing a comfort load."""
        if self._pending_input is None:
            return await self.async_step_reconfigure()
        if user_input is not None:
            subentry = self._get_reconfigure_subentry()
            pending = self._pending_input
            self._pending_input = None
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                data=pending,
                title=_subentry_display_title(SUBENTRY_TYPE_COMFORT, pending),
                unique_id=f"{SUBENTRY_TYPE_COMFORT}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="reconfigure_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )


class OptionalSubentryFlowHandler(ConfigSubentryFlow):
    """Handle optional load subentry flow."""

    _pending_input: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create an optional load subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _subentry_name_in_use(self._get_entry(), user_input[CONF_NAME]):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_optional_data(user_input))
            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_complete()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _optional_schema(), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after creating an optional load."""
        if self._pending_input is None:
            return await self.async_step_user()
        if user_input is not None:
            pending = self._pending_input
            self._pending_input = None
            return self.async_create_entry(
                title=_subentry_display_title(SUBENTRY_TYPE_OPTIONAL, pending),
                data=pending,
                unique_id=f"{SUBENTRY_TYPE_OPTIONAL}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure an optional subentry."""
        errors: dict[str, str] = {}
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)

        if user_input is not None:
            if _subentry_name_in_use_excluding(
                self._get_entry(), user_input[CONF_NAME], subentry.subentry_id
            ):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_optional_data(user_input))

            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_reconfigure_complete()
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_optional_schema(), defaults),
            errors=errors,
        )

    async def async_step_reconfigure_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after editing an optional load."""
        if self._pending_input is None:
            return await self.async_step_reconfigure()
        if user_input is not None:
            subentry = self._get_reconfigure_subentry()
            pending = self._pending_input
            self._pending_input = None
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                data=pending,
                title=_subentry_display_title(SUBENTRY_TYPE_OPTIONAL, pending),
                unique_id=f"{SUBENTRY_TYPE_OPTIONAL}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="reconfigure_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )
