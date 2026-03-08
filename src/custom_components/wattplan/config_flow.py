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
    CONF_SOURCE_PRICE,
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
from .source_pipeline import (
    async_fetch_source_payload,
    build_source_base_provider,
    build_source_value_provider,
)
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


def _invalid_key_from_source_error(err: SourceProviderError) -> str:
    """Map source provider errors to flow translation keys."""

    built_in_reason = err.details.get("built_in_reason")
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
    if err.code == "source_validation" and (
        "entity_ids" in err.details or "service" in err.details
    ):
        return "invalid_payload"
    return "not_enough_values"


async def _async_source_summary(
    hass: HomeAssistant,
    *,
    core_data: dict[str, Any],
    key: str,
    source: dict[str, Any],
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
    has_gaps = False
    history_coverage_days = 0.0
    mode = source.get(CONF_SOURCE_MODE)

    try:
        if mode == SOURCE_MODE_BUILT_IN:
            provider = build_source_base_provider(
                hass,
                source_key=key,
                source_config=source,
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
            available_count = len(debug["forecast_values"])
            coverage_start, coverage_end, history_coverage_days = (
                _built_in_history_coverage(debug, start_at)
            )
        else:
            payload = await async_fetch_source_payload(
                hass,
                source_key=key,
                source_config=source,
            )
            available_count, coverage_start, coverage_end, has_gaps = (
                _summarize_payload_coverage(
                    payload,
                    source,
                    start_at=start_at,
                    slot_minutes=slot_minutes,
                    floor_to_slot=floor_to_slot,
                )
            )
    except (SourceProviderError, vol.Invalid):
        pass

    error_key: str | None = None
    try:
        available_count = await _async_validate_source_values(
            hass,
            core_data=core_data,
            key=key,
            source=source,
            floor_to_slot=floor_to_slot,
            validate_built_in_entity=validate_built_in_entity,
        )
        is_valid = True
    except SourceProviderError as err:
        error_key = _invalid_key_from_source_error(err)
        is_valid = False
        if available_from_error := err.details.get("available_count"):
            available_count = int(available_from_error)
            coverage_end = start_at + (available_count * timedelta(minutes=slot_minutes))

    history_warning = False
    review_text_key = "review_ready"
    review_text_placeholders: dict[str, str] | None = None
    if mode == SOURCE_MODE_BUILT_IN and history_coverage_days < 7:
        history_warning = True
        review_text_key = "review_limited_history"
        review_text_placeholders = {"history_days": f"{history_coverage_days:.1f}"}
    if not is_valid:
        review_text_key = "review_invalid"
    elif available_count < expected_slots:
        review_text_key = "review_incomplete"
        review_text_placeholders = None
    elif has_gaps:
        review_text_key = "review_has_gaps"
        review_text_placeholders = None

    review_text = await _async_config_translation(
        hass,
        review_text_key,
        placeholders=review_text_placeholders,
    )

    return {
        "available_count": available_count,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "review_text": review_text,
        "is_valid": is_valid,
        "error_key": error_key,
        "history_warning": history_warning,
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
    if include_not_used:
        options.append(selector.SelectOptionDict(value=SOURCE_MODE_NOT_USED, label="Not used"))
    options.append(selector.SelectOptionDict(value=SOURCE_MODE_TEMPLATE, label="Template"))

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
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "forecast")): (
                selector.TextSelector()
            ),
            vol.Required(CONF_TIME_KEY, default=defaults.get(CONF_TIME_KEY, "start")): (
                selector.TextSelector()
            ),
            vol.Required(CONF_VALUE_KEY, default=defaults.get(CONF_VALUE_KEY, "value")): (
                selector.TextSelector()
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
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): (
                selector.TextSelector()
            ),
            vol.Required(CONF_TIME_KEY, default=defaults.get(CONF_TIME_KEY, "start")): (
                selector.TextSelector()
            ),
            vol.Required(CONF_VALUE_KEY, default=defaults.get(CONF_VALUE_KEY, "value")): (
                selector.TextSelector()
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
            vol.Required(CONF_CAPACITY_KWH): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1, max=1000, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_MINIMUM_KWH): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=1000, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_MAX_CHARGE_KW): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=500, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_MAX_DISCHARGE_KW): selector.NumberSelector(
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
            vol.Required(CONF_DURATION_MINUTES): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=1440, step=15, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_RUN_WITHIN_HOURS, default=24): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_ENERGY_KWH): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=0.1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_OPTIONS_COUNT, default=3): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX
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
    _validate_text_field(
        str(data.get(CONF_NAME, "")),
        CONF_NAME,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(data.get(CONF_TIME_KEY, "")),
        CONF_TIME_KEY,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(data.get(CONF_VALUE_KEY, "")),
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
    _validate_text_field(
        str(data.get(CONF_NAME, "")),
        CONF_NAME,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(data.get(CONF_TIME_KEY, "")),
        CONF_TIME_KEY,
        errors,
        max_length=MAX_SOURCE_KEY_LENGTH,
    )
    _validate_text_field(
        str(data.get(CONF_VALUE_KEY, "")),
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
    root_key = str(user_input.get(CONF_NAME, ""))
    time_key = str(user_input.get(CONF_TIME_KEY, ""))
    value_key = str(user_input.get(CONF_VALUE_KEY, ""))

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
    root_key = str(user_input.get(CONF_NAME, ""))
    time_key = str(user_input.get(CONF_TIME_KEY, ""))
    value_key = str(user_input.get(CONF_VALUE_KEY, ""))
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
            CONF_SOURCE_PRICE,
            user_input,
            include_not_used=False,
            step_id="source_price",
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
        errors: dict[str, str] = {}

        if user_input is not None:
            mode = user_input[CONF_SOURCE_MODE]
            if mode == SOURCE_MODE_TEMPLATE:
                if key == CONF_SOURCE_PRICE:
                    return await self.async_step_source_price_template()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_template()
                return await self.async_step_source_pv_template()

            if mode == SOURCE_MODE_ENTITY_ADAPTER:
                if key == CONF_SOURCE_PRICE:
                    return await self.async_step_source_price_adapter()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_adapter()
                return await self.async_step_source_pv_adapter()

            if mode == SOURCE_MODE_SERVICE_ADAPTER:
                if key == CONF_SOURCE_PRICE:
                    return await self.async_step_source_price_service()
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
            SOURCE_MODE_NOT_USED if include_not_used else SOURCE_MODE_TEMPLATE,
        )
        if default_mode == SOURCE_MODE_NOT_USED and not include_not_used:
            default_mode = SOURCE_MODE_TEMPLATE

        return self.async_show_form(
            step_id=step_id,
            data_schema=_source_mode_schema(
                default_mode,
                include_not_used=include_not_used,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure price source template."""
        return await self._async_step_source_template(
            CONF_SOURCE_PRICE, user_input, step_id="source_price_template"
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
            CONF_SOURCE_PRICE, user_input, step_id="source_price_adapter"
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
            CONF_SOURCE_PRICE, user_input, step_id="source_price_service"
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
                try:
                    source = await self._async_prepare_entity_source(user_input)
                    source_input = (
                        _auto_detect_step_defaults(user_input, source)
                        if user_input.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT
                        else user_input
                    )
                    return await self._async_prepare_source_review(
                        key,
                        source,
                        source_step_id=step_id,
                        source_input=source_input,
                    )
                except SourceProviderError as err:
                    errors["base"] = _invalid_key_from_source_error(err)

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _source_adapter_schema(existing), defaults
            ),
            errors=errors,
            description_placeholders=self._source_description_placeholders(key),
            last_step=False,
        )

    async def _async_prepare_entity_source(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Return explicit entity adapter config, resolving auto-detect if needed."""
        return await _async_prepare_entity_source_input(self.hass, user_input)

    async def _async_prepare_service_source(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Return explicit service adapter config, resolving auto-detect if needed."""
        return await _async_prepare_service_source_input(self.hass, user_input)

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
                try:
                    source = await self._async_prepare_service_source(user_input)
                    source_input = (
                        _auto_detect_step_defaults(user_input, source)
                        if user_input.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT
                        else user_input
                    )
                    return await self._async_prepare_source_review(
                        key,
                        source,
                        source_step_id=step_id,
                        source_input=source_input,
                    )
                except SourceProviderError as err:
                    errors["base"] = _invalid_key_from_source_error(err)

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
        if key == CONF_SOURCE_USAGE:
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
            floor_to_slot=self._floor_to_slot,
            validate_built_in_entity=self._validate_built_in_usage_entity,
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
                await self._async_validate_source(key, pending)
            except vol.Invalid as err:
                errors["base"] = str(err)
                self._pending_source_summary = await _async_source_summary(
                    self.hass,
                    core_data=self._core,
                    key=key,
                    source=pending,
                    floor_to_slot=self._floor_to_slot,
                    validate_built_in_entity=self._validate_built_in_usage_entity,
                )
                summary = self._pending_source_summary or {}
                is_valid = bool(summary.get("is_valid", False))
                defaults[CONF_ACCEPT_SOURCE_SUMMARY] = is_valid
            else:
                self._sources[key] = pending
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
                "coverage_start": _format_coverage_datetime(
                    summary.get("coverage_start", "Unknown"),
                    self.hass.config.time_zone,
                ),
                "coverage_end": _format_coverage_datetime(
                    summary.get("coverage_end", "Unknown"),
                    self.hass.config.time_zone,
                ),
                "coverage_summary": (
                    f"{summary.get('available_count', 0)} usable intervals, "
                    f"{_expected_slots(self._core)} needed, "
                    f"{self._core[CONF_SLOT_MINUTES]}-minute resolution"
                ),
                "review_text": str(summary.get("review_text", "")),
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
        if key == CONF_SOURCE_PRICE:
            return await self.async_step_source_usage()
        if key == CONF_SOURCE_USAGE:
            return await self.async_step_source_pv()
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

    def _is_final_source_step(self, key: str) -> bool:
        """Return if the source config step is the last one before create."""
        return key == CONF_SOURCE_PV

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
            CONF_SOURCE_PRICE,
            user_input,
            include_not_used=False,
            step_id="source_price",
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

        if user_input is not None:
            mode = user_input[CONF_SOURCE_MODE]

            if mode == SOURCE_MODE_TEMPLATE:
                if key == CONF_SOURCE_PRICE:
                    return await self.async_step_source_price_template()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_template()
                return await self.async_step_source_pv_template()

            if mode == SOURCE_MODE_ENTITY_ADAPTER:
                if key == CONF_SOURCE_PRICE:
                    return await self.async_step_source_price_adapter()
                if key == CONF_SOURCE_USAGE:
                    return await self.async_step_source_usage_adapter()
                return await self.async_step_source_pv_adapter()

            if mode == SOURCE_MODE_SERVICE_ADAPTER:
                if key == CONF_SOURCE_PRICE:
                    return await self.async_step_source_price_service()
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
                    SOURCE_MODE_NOT_USED if include_not_used else SOURCE_MODE_TEMPLATE,
                ),
                include_not_used=include_not_used,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit price source template in options."""
        return await self._async_step_source_template_options(
            CONF_SOURCE_PRICE,
            user_input,
            step_id="source_price_template",
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
            CONF_SOURCE_PRICE,
            user_input,
            step_id="source_price_adapter",
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
            CONF_SOURCE_PRICE,
            user_input,
            step_id="source_price_service",
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
                try:
                    source = await _async_prepare_entity_source_input(
                        self.hass, user_input
                    )
                    source_input = (
                        _auto_detect_step_defaults(user_input, source)
                        if user_input.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT
                        else user_input
                    )
                    return await self._async_prepare_source_review(
                        key,
                        source,
                        source_step_id=step_id,
                        source_input=source_input,
                    )
                except SourceProviderError as err:
                    errors["base"] = _invalid_key_from_source_error(err)

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
                try:
                    source = await _async_prepare_service_source_input(
                        self.hass, user_input
                    )
                    source_input = (
                        _auto_detect_step_defaults(user_input, source)
                        if user_input.get(CONF_ADAPTER_TYPE) == ADAPTER_TYPE_AUTO_DETECT
                        else user_input
                    )
                    return await self._async_prepare_source_review(
                        key,
                        source,
                        source_step_id=step_id,
                        source_input=source_input,
                    )
                except SourceProviderError as err:
                    errors["base"] = _invalid_key_from_source_error(err)

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
        if key == CONF_SOURCE_USAGE:
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
            floor_to_slot=self._floor_to_slot,
            validate_built_in_entity=self._validate_built_in_usage_entity,
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
                await self._async_validate_source(key, pending)
            except vol.Invalid as err:
                errors["base"] = str(err)
                self._pending_source_summary = await _async_source_summary(
                    self.hass,
                    core_data=self._data,
                    key=key,
                    source=pending,
                    floor_to_slot=self._floor_to_slot,
                    validate_built_in_entity=self._validate_built_in_usage_entity,
                )
                summary = self._pending_source_summary or {}
                is_valid = bool(summary.get("is_valid", False))
                defaults[CONF_ACCEPT_SOURCE_SUMMARY] = is_valid
            else:
                sources = dict(self._data.get(CONF_SOURCES, {}))
                sources[key] = pending
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
                "coverage_start": _format_coverage_datetime(
                    summary.get("coverage_start", "Unknown"),
                    self.hass.config.time_zone,
                ),
                "coverage_end": _format_coverage_datetime(
                    summary.get("coverage_end", "Unknown"),
                    self.hass.config.time_zone,
                ),
                "coverage_summary": (
                    f"{summary.get('available_count', 0)} usable intervals, "
                    f"{_expected_slots(self._data)} needed, "
                    f"{self._data[CONF_SLOT_MINUTES]}-minute resolution"
                ),
                "review_text": str(summary.get("review_text", "")),
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
                return self.async_create_entry(
                    title=_subentry_display_title(
                        SUBENTRY_TYPE_BATTERY, normalized_input
                    ),
                    data=normalized_input,
                    unique_id=(
                        f"{SUBENTRY_TYPE_BATTERY}:"
                        f"{_normalize_name(normalized_input[CONF_NAME])}"
                    ),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _battery_schema(), _battery_form_defaults(user_input or {})
            ),
            errors=errors,
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
                return self.async_update_reload_and_abort(
                    self._get_entry(),
                    subentry,
                    data=normalized_input,
                    title=_subentry_display_title(
                        SUBENTRY_TYPE_BATTERY, normalized_input
                    ),
                    unique_id=(
                        f"{SUBENTRY_TYPE_BATTERY}:"
                        f"{_normalize_name(normalized_input[CONF_NAME])}"
                    ),
                )
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _battery_schema(), _battery_form_defaults(defaults)
            ),
            errors=errors,
        )


class ComfortSubentryFlowHandler(ConfigSubentryFlow):
    """Handle comfort subentry flow."""

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
                return self.async_create_entry(
                    title=_subentry_display_title(SUBENTRY_TYPE_COMFORT, user_input),
                    data=user_input,
                    unique_id=f"{SUBENTRY_TYPE_COMFORT}:{_normalize_name(user_input[CONF_NAME])}",
                )

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
                return self.async_update_reload_and_abort(
                    self._get_entry(),
                    subentry,
                    data=user_input,
                    title=_subentry_display_title(SUBENTRY_TYPE_COMFORT, user_input),
                    unique_id=f"{SUBENTRY_TYPE_COMFORT}:{_normalize_name(user_input[CONF_NAME])}",
                )
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_comfort_schema(), defaults),
            errors=errors,
            description_placeholders={
                "slot_minutes": str(self._get_entry().data[CONF_SLOT_MINUTES])
            },
        )


class OptionalSubentryFlowHandler(ConfigSubentryFlow):
    """Handle optional load subentry flow."""

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
                return self.async_create_entry(
                    title=_subentry_display_title(SUBENTRY_TYPE_OPTIONAL, user_input),
                    data=user_input,
                    unique_id=f"{SUBENTRY_TYPE_OPTIONAL}:{_normalize_name(user_input[CONF_NAME])}",
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _optional_schema(), user_input or {}
            ),
            errors=errors,
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
                return self.async_update_reload_and_abort(
                    self._get_entry(),
                    subentry,
                    data=user_input,
                    title=_subentry_display_title(SUBENTRY_TYPE_OPTIONAL, user_input),
                    unique_id=f"{SUBENTRY_TYPE_OPTIONAL}:{_normalize_name(user_input[CONF_NAME])}",
                )
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_optional_schema(), defaults),
            errors=errors,
        )
