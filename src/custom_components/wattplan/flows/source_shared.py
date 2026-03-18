"""Config flow for the WattPlan integration."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
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

from .common import (
    _expected_slots,
    _format_number,
    _normalize_name,
    _subentry_display_title,
    _subentry_name,
)
from ..const import (
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
    CONF_OPTIMIZER_PROFILE,
    CONF_PREFER_PV_SURPLUS_CHARGING,
    CONF_PROVIDERS,
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
    OPTIMIZER_PROFILE_AGGRESSIVE,
    OPTIMIZER_PROFILE_BALANCED,
    OPTIMIZER_PROFILE_CONSERVATIVE,
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
from ..datetime_utils import parse_datetime_like
from ..forecast_provider import ForecastProvider
from ..source_pipeline import build_source_base_provider, build_source_value_provider
from ..source_provider import (
    async_auto_detect_entity_adapter,
    async_auto_detect_service_adapter,
    async_get_energy_solar_forecast_entries,
    primary_provider_config,
    source_mode,
)
from ..source_types import SourceProviderError, SourceWindow

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


def _format_coverage_datetime(
    value: str | datetime, timezone_name: str | None
) -> str:
    """Format coverage datetimes in the Home Assistant local timezone."""
    parsed = parse_datetime_like(value)
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
    provider = primary_provider_config(source)
    defaults = {
        **dict(provider),
        **{
            key: value
            for key, value in source.items()
            if key not in {CONF_PROVIDERS}
        },
    }
    providers = source.get(CONF_PROVIDERS)
    if isinstance(providers, list) and providers and source_mode(source) == SOURCE_MODE_ENTITY_ADAPTER:
        defaults[CONF_WATTPLAN_ENTITY_ID] = [
            provider[CONF_WATTPLAN_ENTITY_ID]
            for provider in providers
            if isinstance(provider, dict) and CONF_WATTPLAN_ENTITY_ID in provider
        ]
    return defaults


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
    provider = primary_provider_config(resolved_source)
    defaults[CONF_NAME] = provider.get(CONF_NAME, "")
    defaults[CONF_TIME_KEY] = provider.get(CONF_TIME_KEY, "")
    defaults[CONF_VALUE_KEY] = provider.get(CONF_VALUE_KEY, "")
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
        last_changed = parse_datetime_like(row.get("last_changed"))
        if last_changed is not None:
            rows.append(last_changed)
    if not rows:
        for row in debug.get("raw_statistics_rows", []):
            started = parse_datetime_like(row.get("start"))
            if started is not None:
                rows.append(started)
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
            point_dt = parse_datetime_like(point.get(time_key))
            if point_dt is None:
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
        path = str(best.get("path", "<root>"))
        time_key = str(best.get("time_key", ""))
        value_key = str(best.get("value_key", ""))
        return (
            f"- ✅ Looks usable: `{entity_id}`. Found forecast data in `{path}` "
            f"using `{time_key}` for time and `{value_key}` for value."
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
        return (
            "Remove the entities that do not match the others, or switch to manual "
            "mapping."
        )
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

    providers: list[dict[str, Any]] = []
    entity_ids: list[str] = []
    for item in detected:
        if not isinstance(item, dict):
            continue
        root_key = str(item.get("root_key", ""))
        time_key = str(item.get("time_key", ""))
        value_key = str(item.get("value_key", ""))
        entity_id = str(item.get("entity_id", ""))
        if not entity_id or not root_key or not time_key or not value_key:
            continue
        entity_ids.append(entity_id)
        providers.append(
            {
                CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                CONF_WATTPLAN_ENTITY_ID: entity_id,
                CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                CONF_NAME: root_key,
                CONF_TIME_KEY: time_key,
                CONF_VALUE_KEY: value_key,
            }
        )

    if not providers:
        return None

    return {
        **source,
        CONF_WATTPLAN_ENTITY_ID: entity_ids,
        CONF_PROVIDERS: providers,
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
        lines.append("")
        detected_providers = resolved_source.get(CONF_PROVIDERS)
        if isinstance(detected_providers, list) and len(detected_providers) > 1:
            lines.append(
                "- WattPlan checked each selected entity and found usable forecast data."
            )
            lines.append("")
            lines.extend(
                f"- ✅ Looks usable: `{provider_config.get(CONF_WATTPLAN_ENTITY_ID, 'entity')}`. "
                f"Found forecast data in `{provider_config.get(CONF_NAME, '') or '<root>'}` "
                f"using `{provider_config.get(CONF_TIME_KEY, '')}` for time and "
                f"`{provider_config.get(CONF_VALUE_KEY, '')}` for value."
                for provider_config in detected_providers
                if isinstance(provider_config, dict)
            )
            return "\n".join(lines)

        provider = primary_provider_config(resolved_source)
        lines.extend(
            [
                "- WattPlan found usable forecast data automatically.",
                "",
                f"- ✅ Looks usable. Found forecast data in "
                f"`{provider.get(CONF_NAME, '') or '<root>'}` using "
                f"`{provider.get(CONF_TIME_KEY, '')}` for time and "
                f"`{provider.get(CONF_VALUE_KEY, '')}` for value.",
            ]
        )
        return "\n".join(lines)

    lines.append("")
    if err.details.get("diagnostic_kind") == "auto_detect_conflict":
        lines.append(
            "- WattPlan found usable forecast data, but the selected input did not "
            "resolve to one consistent structure."
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
    detected_mappings = err.details.get("detected_mappings")
    if isinstance(detected_mappings, list) and detected_mappings:
        lines.append("")
        for detected in detected_mappings:
            entity_id = str(detected.get("entity_id", "entity"))
            root_key = str(detected.get("root_key", "<root>"))
            time_key = str(detected.get("time_key", ""))
            value_key = str(detected.get("value_key", ""))
            lines.append(
                f"- ✅ Looks usable: `{entity_id}`. Found forecast data in `{root_key}` "
                f"using `{time_key}` for time and `{value_key}` for value."
            )
    entity_candidates = err.details.get("entity_candidates")
    if isinstance(entity_candidates, dict):
        lines.append("")
        for entity_id, candidates in entity_candidates.items():
            if isinstance(detected_mappings, list) and any(
                str(item.get("entity_id")) == str(entity_id) for item in detected_mappings
            ):
                continue
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

    mode = source_mode(source)
    provider = primary_provider_config(source)
    if mode == SOURCE_MODE_ENTITY_ADAPTER:
        entity_ids = source.get(CONF_WATTPLAN_ENTITY_ID, provider.get(CONF_WATTPLAN_ENTITY_ID))
        if isinstance(entity_ids, list) and entity_ids:
            return f"Entity attribute: {', '.join(str(entity_id) for entity_id in entity_ids)}"
        if isinstance(entity_ids, str) and entity_ids:
            return f"Entity attribute: {entity_ids}"
        return "Entity attribute"
    if mode == SOURCE_MODE_SERVICE_ADAPTER:
        service = provider.get(CONF_SERVICE, source.get(CONF_SERVICE))
        return f"Service call: {service}" if service else "Service call"
    if mode == SOURCE_MODE_TEMPLATE:
        return "Template"
    if mode == SOURCE_MODE_BUILT_IN:
        entity_id = provider.get(CONF_WATTPLAN_ENTITY_ID, source.get(CONF_WATTPLAN_ENTITY_ID))
        return f"Built in: {entity_id}" if entity_id else "Built in"
    if mode == SOURCE_MODE_ENERGY_PROVIDER:
        config_entry_id = provider.get(CONF_CONFIG_ENTRY_ID, source.get(CONF_CONFIG_ENTRY_ID))
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


def _optimizer_profile_selector(default: str) -> selector.SelectSelector:
    """Build selector for user-facing optimizer profiles."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(
                    value=OPTIMIZER_PROFILE_AGGRESSIVE,
                    label="Aggressive",
                ),
                selector.SelectOptionDict(
                    value=OPTIMIZER_PROFILE_BALANCED,
                    label="Balanced",
                ),
                selector.SelectOptionDict(
                    value=OPTIMIZER_PROFILE_CONSERVATIVE,
                    label="Conservative",
                ),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _core_schema(
    defaults: dict[str, Any] | None = None,
    *,
    include_name: bool = False,
    include_profile: bool = False,
    profile_last: bool = False,
) -> vol.Schema:
    """Build schema for the core planner settings."""
    defaults = defaults or {}
    slot_default = str(defaults.get(CONF_SLOT_MINUTES, 15))
    hours_default = str(defaults.get(CONF_HOURS_TO_PLAN, 48))
    schema: dict[Any, Any] = {}
    profile_field = None
    if include_profile:
        profile_field = (
            vol.Required(
                CONF_OPTIMIZER_PROFILE,
                default=str(
                    defaults.get(CONF_OPTIMIZER_PROFILE, OPTIMIZER_PROFILE_BALANCED)
                ),
            ),
            _optimizer_profile_selector(
                str(defaults.get(CONF_OPTIMIZER_PROFILE, OPTIMIZER_PROFILE_BALANCED))
            ),
        )
    schema.update({
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
    })
    if include_profile and not profile_last:
        assert profile_field is not None
        schema[profile_field[0]] = profile_field[1]
    if include_name:
        schema[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "WattPlan"))] = (
            selector.TextSelector()
        )
    if include_profile and profile_last:
        assert profile_field is not None
        schema[profile_field[0]] = profile_field[1]
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
                    CONF_PREFER_PV_SURPLUS_CHARGING: False,
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
                        vol.Required(
                            CONF_PREFER_PV_SURPLUS_CHARGING,
                            default=False,
                        ): selector.BooleanSelector(),
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

    if adapter_type == ADAPTER_TYPE_AUTO_DETECT:
        detected_list = await async_auto_detect_entity_adapter(hass, entity_ids)
        providers = [
            {
                CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                CONF_WATTPLAN_ENTITY_ID: entity_id,
                CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                CONF_NAME: detected.root_key,
                CONF_TIME_KEY: detected.time_key,
                CONF_VALUE_KEY: detected.value_key,
            }
            for entity_id, detected in zip(entity_ids, detected_list, strict=True)
        ]
    else:
        providers = [
            {
                CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                CONF_WATTPLAN_ENTITY_ID: entity_id,
                CONF_ADAPTER_TYPE: adapter_type,
                CONF_NAME: root_key,
                CONF_TIME_KEY: time_key,
                CONF_VALUE_KEY: value_key,
            }
            for entity_id in entity_ids
        ]

    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        CONF_PROVIDERS: providers,
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
        CONF_PROVIDERS: [
            {
                CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
                CONF_SERVICE: service_name,
                CONF_ADAPTER_TYPE: resolved_adapter,
                CONF_NAME: root_key,
                CONF_TIME_KEY: time_key,
                CONF_VALUE_KEY: value_key,
            }
        ],
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

    if mode == SOURCE_MODE_ENTITY_ADAPTER:
        if source_input is None:
            raise SourceProviderError(
                "source_validation",
                "Entity adapter source is missing staged input",
                details={"source_mode": SOURCE_MODE_ENTITY_ADAPTER},
            )
        resolved = await _async_prepare_entity_source_input(hass, source_input)
        if source_input.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT:
            return resolved, _auto_detect_step_defaults(source_input, resolved)
        return resolved, source_input

    if mode == SOURCE_MODE_SERVICE_ADAPTER:
        if source_input is None:
            raise SourceProviderError(
                "source_validation",
                "Service adapter source is missing staged input",
                details={"source_mode": SOURCE_MODE_SERVICE_ADAPTER},
            )
        resolved = await _async_prepare_service_source_input(hass, source_input)
        if source_input.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT:
            return resolved, _auto_detect_step_defaults(source_input, resolved)
        return resolved, source_input

    return source, source_input


class _SharedSourceFlow:
    """Shared source-step helpers for setup and options flows."""

    _last_source_available_count: int | None
    _pending_source_key: str | None
    _pending_source: dict[str, Any] | None
    _pending_source_input: dict[str, Any] | None
    _pending_source_step_id: str | None
    _pending_source_summary: dict[str, Any] | None

    def _core_data(self) -> dict[str, Any]:
        """Return the active planner core data for this flow."""
        raise NotImplementedError

    def _stored_source(self, key: str) -> dict[str, Any]:
        """Return the currently persisted source config for a source key."""
        raise NotImplementedError

    async def _async_handle_source_marked_not_used(self, key: str) -> ConfigFlowResult:
        """Persist a not-used source choice and continue the flow."""
        raise NotImplementedError

    async def _async_default_source_step(self) -> ConfigFlowResult:
        """Return the flow step used when no staged source is active."""
        raise NotImplementedError

    async def _async_commit_reviewed_source(
        self, key: str, resolved_pending: dict[str, Any]
    ) -> ConfigFlowResult:
        """Persist one reviewed source and continue the flow."""
        raise NotImplementedError

    def _review_form_last_step(self, key: str) -> bool:
        """Return whether the review form should be marked last-step."""
        return False

    async def _async_branch_to_source_mode_step(
        self,
        key: str,
        mode: str,
        *,
        include_built_in: bool,
        include_energy_provider: bool,
    ) -> ConfigFlowResult | None:
        """Return the concrete step for a chosen source mode when supported."""
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

        return None

    def _default_source_mode(
        self,
        existing: dict[str, Any],
        *,
        key: str,
        include_not_used: bool,
        include_built_in: bool,
        include_energy_provider_option: bool,
    ) -> str:
        """Return the preferred default mode for one source selection step."""
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
            return SOURCE_MODE_TEMPLATE
        if (
            default_mode == SOURCE_MODE_ENERGY_PROVIDER
            and not include_energy_provider_option
        ):
            return SOURCE_MODE_TEMPLATE
        return default_mode

    def _source_description_placeholders(self, key: str) -> dict[str, str]:
        """Return description placeholders for source steps."""
        source_label = "Price"
        if key == CONF_SOURCE_EXPORT_PRICE:
            source_label = "Export price"
        elif key == CONF_SOURCE_USAGE:
            source_label = "Usage"
        elif key == CONF_SOURCE_PV:
            source_label = "PV"

        core_data = self._core_data()
        return {
            "source_label": source_label,
            "required_count": str(_expected_slots(core_data)),
            "available_count": str(self._last_source_available_count or 0),
            "slot_minutes": str(core_data[CONF_SLOT_MINUTES]),
        }

    async def _async_energy_provider_options(self) -> list[selector.SelectOptionDict]:
        """Return Energy solar forecast providers as selector options."""
        from .. import config_flow as config_flow_module

        entries = await config_flow_module.async_get_energy_solar_forecast_entries(
            self.hass
        )
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
        return _source_base_defaults(self._stored_source(key))

    async def _async_source_template_step(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure or edit a source using template mode."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                CONF_PROVIDERS: [
                    {
                        CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                        CONF_TEMPLATE: user_input[CONF_TEMPLATE],
                    }
                ],
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

    async def _async_source_adapter_step(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure or edit a source using entity adapter mode."""
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

    async def _async_source_service_step(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure or edit a source using service adapter mode."""
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

    async def _async_source_energy_provider_step(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure or edit a source using an Energy solar forecast provider."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(key)
        defaults = existing
        provider_options = await self._async_energy_provider_options()

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER,
                CONF_PROVIDERS: [
                    {
                        CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER,
                        CONF_CONFIG_ENTRY_ID: user_input[CONF_CONFIG_ENTRY_ID],
                    }
                ],
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

    async def _async_source_built_in_step(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure or edit the built-in usage forecast source."""
        errors: dict[str, str] = {}
        existing = self._source_step_defaults(CONF_SOURCE_USAGE)
        defaults = existing

        if user_input is not None:
            defaults = user_input
            source = {
                CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN,
                CONF_PROVIDERS: [
                    {
                        CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN,
                        CONF_WATTPLAN_ENTITY_ID: user_input[CONF_WATTPLAN_ENTITY_ID],
                        CONF_HISTORY_DAYS: int(user_input[CONF_HISTORY_DAYS]),
                    }
                ],
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

    async def _async_refresh_pending_source_summary(
        self,
        key: str,
        source: dict[str, Any],
        source_input: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Rebuild the staged source summary for the current core data."""
        self._pending_source_summary = await _async_source_summary(
            self.hass,
            core_data=self._core_data(),
            key=key,
            source=source,
            source_input=source_input,
            floor_to_slot=self._floor_to_slot,
            validate_built_in_entity=self._validate_built_in_usage_entity,
        )
        return self._pending_source_summary

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
        summary = await self._async_refresh_pending_source_summary(
            key, source, source_input
        )
        self._pending_source_input = summary.get("resolved_source_input", source_input)
        return await self.async_step_source_review()

    async def async_step_source_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review and confirm one staged source."""
        if self._pending_source_key is None or self._pending_source is None:
            return await self._async_default_source_step()

        key = self._pending_source_key
        pending = dict(self._pending_source)
        summary = self._pending_source_summary or {}
        resolved_pending = dict(summary.get("resolved_source", pending))
        is_valid = bool(summary.get("is_valid", False))
        defaults = {CONF_ACCEPT_SOURCE_SUMMARY: is_valid}
        errors: dict[str, str] = (
            {"base": str(summary["error_key"])}
            if summary.get("error_key") is not None
            else {}
        )

        if user_input is not None:
            if not is_valid or not user_input[CONF_ACCEPT_SOURCE_SUMMARY]:
                return await self._async_return_to_pending_source_step()
            try:
                await self._async_validate_source(key, resolved_pending)
            except vol.Invalid as err:
                errors["base"] = str(err)
                summary = await self._async_refresh_pending_source_summary(
                    key, pending, self._pending_source_input
                )
                resolved_pending = dict(summary.get("resolved_source", pending))
                is_valid = bool(summary.get("is_valid", False))
                defaults[CONF_ACCEPT_SOURCE_SUMMARY] = is_valid
            else:
                self._pending_source_key = None
                self._pending_source = None
                self._pending_source_input = None
                self._pending_source_step_id = None
                self._pending_source_summary = None
                return await self._async_commit_reviewed_source(key, resolved_pending)

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
            last_step=self._review_form_last_step(key),
        )

    async def _async_return_to_pending_source_step(self) -> ConfigFlowResult:
        """Return to the staged source input step."""
        if self._pending_source_step_id is None:
            return await self._async_default_source_step()
        return await getattr(self, f"async_step_{self._pending_source_step_id}")()

    async def _async_validate_source(self, key: str, source: dict[str, Any]) -> None:
        """Validate source config against the current horizon."""
        try:
            self._last_source_available_count = await _async_validate_source_values(
                self.hass,
                core_data=self._core_data(),
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
