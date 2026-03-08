"""Tests for WattPlan source provider behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from custom_components.wattplan.adapter_auto import (
    auto_detect_mapping,
    resolve_nested_value,
)
from custom_components.wattplan.const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    ADAPTER_TYPE_ATTRIBUTE_VALUES,
    ADAPTER_TYPE_SERVICE_RESPONSE,
    AGGREGATION_MODE_MAX,
    AGGREGATION_MODE_MEAN,
    CLAMP_MODE_NEAREST,
    CONF_ADAPTER_TYPE,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_CONFIG_ENTRY_ID,
    CONF_EDGE_FILL_MODE,
    CONF_RESAMPLE_MODE,
    CONF_SERVICE,
    CONF_SOURCE_MODE,
    CONF_TEMPLATE,
    EDGE_FILL_MODE_HOLD,
    RESAMPLE_MODE_FORWARD_FILL,
    RESAMPLE_MODE_LINEAR,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_SERVICE_ADAPTER,
    SOURCE_MODE_TEMPLATE,
)
from custom_components.wattplan.source_pipeline import build_source_base_provider
from custom_components.wattplan.source_provider import (
    EnergySolarForecastSourceProvider,
    TemplateAdapterSourceProvider,
    async_auto_detect_entity_adapter,
    async_auto_detect_service_adapter,
)
from custom_components.wattplan.source_types import SourceProviderError, SourceWindow
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from tests.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _window() -> SourceWindow:
    """Return common test window."""
    return SourceWindow(
        start_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        slot_minutes=15,
        slots=4,
    )


def _hour_window() -> SourceWindow:
    """Return an hourly window for merged multi-entity series."""
    return SourceWindow(
        start_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        slot_minutes=60,
        slots=4,
    )


def _hour_window() -> SourceWindow:
    """Return an hourly window for merged multi-entity series."""
    return SourceWindow(
        start_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        slot_minutes=60,
        slots=4,
    )


def _template_config(payload: list[dict[str, object]], **extra: object) -> dict[str, object]:
    """Build template source config with optional overrides."""
    return {
        CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
        CONF_TEMPLATE: f"{{{{ {payload!r} }}}}",
        **extra,
    }


def test_auto_detect_mapping_prefers_largest_compatible_list() -> None:
    """Auto detection should choose the candidate list with the most usable rows."""
    payload = {
        "prices": {
            "small": [{"start": "2026-01-01T00:00:00+00:00", "value": 1.0}],
            "home": [
                {"start_time": "2026-01-01T00:00:00+00:00", "price": 1.0},
                {"start_time": "2026-01-01T01:00:00+00:00", "price": 2.0},
            ],
        }
    }

    detected = auto_detect_mapping(payload)

    assert detected is not None
    assert detected.root_key == "prices.home"
    assert detected.time_key == "start_time"
    assert detected.value_key == "price"


def test_auto_detect_mapping_rejects_three_timestamps() -> None:
    """Auto detection should reject rows with more than two timestamps."""
    payload = [
        {
            "anfang": "2026-01-01T00:00:00+00:00",
            "mitte": "2026-01-01T00:15:00+00:00",
            "ende": "2026-01-01T00:30:00+00:00",
            "preis": 1.2,
        }
    ]

    detected = auto_detect_mapping(payload)

    assert detected is None


def test_auto_detect_mapping_rejects_two_numeric_fields() -> None:
    """Auto detection should reject rows with more than one numeric field."""
    payload = [
        {
            "anfang": "2026-01-01T00:00:00+00:00",
            "preis": 1.2,
            "wert": 2.4,
        }
    ]

    detected = auto_detect_mapping(payload)

    assert detected is None


def test_auto_detect_mapping_prefers_unsuffixed_numeric_base_key() -> None:
    """Auto detection should accept one primary numeric field plus band variants."""
    payload = [
        {
            "period_start": "2026-01-01T00:00:00+00:00",
            "pv_estimate": 1.2,
            "pv_estimate10": 0.6,
            "pv_estimate_90": 1.8,
        }
    ]

    detected = auto_detect_mapping(payload)

    assert detected is not None
    assert detected.time_key == "period_start"
    assert detected.value_key == "pv_estimate"


def test_resolve_nested_value_supports_root_a_and_a_b() -> None:
    """Nested value resolution should support root, one level, and two levels."""
    payload = {
        "a": {
            "b": [
                {"anfang": "2026-01-01T00:00:00+00:00", "preis": 1.0},
            ]
        }
    }

    assert resolve_nested_value(payload["a"]["b"], "") == payload["a"]["b"]
    assert resolve_nested_value(payload, "a") == payload["a"]
    assert resolve_nested_value(payload, "a.b") == payload["a"]["b"]


async def test_template_provider_returns_values(hass: HomeAssistant) -> None:
    """Template source should return one value per slot."""
    payload = [
        {"start": "2026-01-01T00:00:00+00:00", "value": 1.0},
        {"start": "2026-01-01T00:15:00+00:00", "value": 2.0},
        {"start": "2026-01-01T00:30:00+00:00", "value": 3.0},
        {"start": "2026-01-01T00:45:00+00:00", "value": 4.0},
    ]
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config=_template_config(payload),
    )

    values = await provider.async_values(_window())

    assert values == [1.0, 2.0, 3.0, 4.0]


async def test_entity_adapter_provider_returns_values(hass: HomeAssistant) -> None:
    """Entity adapter source should return attribute values."""
    hass.states.async_set("sensor.forecast", "ok", {"prices": [1.0, 2.0, 3.0, 4.0]})
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
            "entity_id": "sensor.forecast",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_VALUES,
            CONF_NAME: "prices",
        },
    )

    values = await provider.async_values(_window())

    assert values == [1.0, 2.0, 3.0, 4.0]


async def test_entity_adapter_auto_detects_nested_attribute(
    hass: HomeAssistant,
) -> None:
    """Auto detect should resolve one mapping shared by selected entities."""
    hass.states.async_set(
        "sensor.first",
        "ok",
        {
            "prices": {
                "home": [
                    {
                        "starts_at": "2026-01-01T00:00:00+00:00",
                        "total": 1.0,
                    }
                ]
            }
        },
    )
    hass.states.async_set(
        "sensor.second",
        "ok",
        {
            "prices": {
                "home": [
                    {
                        "starts_at": "2026-01-01T00:00:00+00:00",
                        "total": 1.0,
                    }
                ]
            }
        },
    )

    detected = await async_auto_detect_entity_adapter(hass, ["sensor.first", "sensor.second"])

    assert detected.root_key == "prices.home"
    assert detected.time_key == "starts_at"
    assert detected.value_key == "total"


async def test_entity_adapter_provider_merges_multiple_entities(
    hass: HomeAssistant,
) -> None:
    """Entity adapter should merge list payloads from multiple selected entities."""
    hass.states.async_set(
        "sensor.today",
        "ok",
        {
            "detailedForecast": [
                {
                    "period_start": "2026-01-01T00:00:00+00:00",
                    "pv_estimate": 1.0,
                },
                {
                    "period_start": "2026-01-01T01:00:00+00:00",
                    "pv_estimate": 2.0,
                },
            ]
        },
    )
    hass.states.async_set(
        "sensor.tomorrow",
        "ok",
        {
            "detailedForecast": [
                {
                    "period_start": "2026-01-01T02:00:00+00:00",
                    "pv_estimate": 3.0,
                },
                {
                    "period_start": "2026-01-01T03:00:00+00:00",
                    "pv_estimate": 4.0,
                },
            ]
        },
    )
    provider = build_source_base_provider(
        hass,
        source_key="solar",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
            "entity_id": ["sensor.today", "sensor.tomorrow"],
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
            CONF_NAME: "detailedForecast",
            "time_key": "period_start",
            "value_key": "pv_estimate",
        },
    )

    values = await provider.async_values(_hour_window())

    assert values == [1.0, 2.0, 3.0, 4.0]


async def test_service_adapter_provider_returns_values(hass: HomeAssistant) -> None:
    """Service adapter should return nested service response values."""

    async def _handle_prices(call: ServiceCall) -> dict[str, Any]:
        return {
            "prices": {
                "home": [
                    {"start": "2026-01-01T00:00:00+00:00", "price": 1.0},
                    {"start": "2026-01-01T00:15:00+00:00", "price": 2.0},
                    {"start": "2026-01-01T00:30:00+00:00", "price": 3.0},
                    {"start": "2026-01-01T00:45:00+00:00", "price": 4.0},
                ]
            }
        }

    hass.services.async_register(
        "test",
        "prices",
        _handle_prices,
        supports_response=SupportsResponse.ONLY,
    )
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
            CONF_SERVICE: "test.prices",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_SERVICE_RESPONSE,
            CONF_NAME: "prices.home",
            "time_key": "start",
            "value_key": "price",
        },
    )

    values = await provider.async_values(_window())

    assert values == [1.0, 2.0, 3.0, 4.0]


async def test_service_adapter_provider_returns_values_from_root_key_a(
    hass: HomeAssistant,
) -> None:
    """Explicit service adapter should support a single-segment root key."""

    async def _handle_prices(call: ServiceCall) -> dict[str, Any]:
        return {
            "a": [
                {"anfang": "2026-01-01T00:00:00+00:00", "preis": 1.0},
                {"anfang": "2026-01-01T00:15:00+00:00", "preis": 2.0},
                {"anfang": "2026-01-01T00:30:00+00:00", "preis": 3.0},
                {"anfang": "2026-01-01T00:45:00+00:00", "preis": 4.0},
            ]
        }

    hass.services.async_register(
        "test",
        "a_prices",
        _handle_prices,
        supports_response=SupportsResponse.ONLY,
    )
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
            CONF_SERVICE: "test.a_prices",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_SERVICE_RESPONSE,
            CONF_NAME: "a",
            "time_key": "anfang",
            "value_key": "preis",
        },
    )

    values = await provider.async_values(_window())

    assert values == [1.0, 2.0, 3.0, 4.0]


async def test_service_adapter_provider_returns_values_from_root_key_a_b(
    hass: HomeAssistant,
) -> None:
    """Explicit service adapter should support a dotted root key."""

    async def _handle_prices(call: ServiceCall) -> dict[str, Any]:
        return {
            "a": {
                "b": [
                    {"anfang": "2026-01-01T00:00:00+00:00", "preis": 1.0},
                    {"anfang": "2026-01-01T00:15:00+00:00", "preis": 2.0},
                    {"anfang": "2026-01-01T00:30:00+00:00", "preis": 3.0},
                    {"anfang": "2026-01-01T00:45:00+00:00", "preis": 4.0},
                ]
            }
        }

    hass.services.async_register(
        "test",
        "ab_prices",
        _handle_prices,
        supports_response=SupportsResponse.ONLY,
    )
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
            CONF_SERVICE: "test.ab_prices",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_SERVICE_RESPONSE,
            CONF_NAME: "a.b",
            "time_key": "anfang",
            "value_key": "preis",
        },
    )

    values = await provider.async_values(_window())

    assert values == [1.0, 2.0, 3.0, 4.0]


async def test_service_adapter_auto_detects_nested_response(
    hass: HomeAssistant,
) -> None:
    """Auto detect should resolve nested service response mappings."""

    async def _handle_prices(call: ServiceCall) -> dict[str, Any]:
        return {
            "prices": {
                "Home": [
                    {
                        "start_time": "2026-01-01T00:00:00+00:00",
                        "end_time": "2026-01-01T00:15:00+00:00",
                        "price": 1.0,
                    }
                ]
            }
        }

    hass.services.async_register(
        "test",
        "nested_prices",
        _handle_prices,
        supports_response=SupportsResponse.ONLY,
    )

    detected = await async_auto_detect_service_adapter(hass, "test.nested_prices")

    assert detected.root_key == "prices.Home"
    assert detected.time_key == "start_time"
    assert detected.value_key == "price"


async def test_service_adapter_auto_detects_later_valid_array(
    hass: HomeAssistant,
) -> None:
    """Auto detect should skip invalid arrays and keep scanning later candidates."""

    async def _handle_prices(call: ServiceCall) -> dict[str, Any]:
        return {
            "a": [{"titel": "irrelevant"}],
            "b": [
                {"anfang": "2026-01-01T00:00:00+00:00", "preis": 1.0},
                {"anfang": "2026-01-01T00:15:00+00:00", "preis": 2.0},
            ],
        }

    hass.services.async_register(
        "test",
        "later_valid_prices",
        _handle_prices,
        supports_response=SupportsResponse.ONLY,
    )

    detected = await async_auto_detect_service_adapter(hass, "test.later_valid_prices")

    assert detected.root_key == "b"
    assert detected.time_key == "anfang"
    assert detected.value_key == "preis"


async def test_service_adapter_auto_detects_dotted_root_with_foreign_names(
    hass: HomeAssistant,
) -> None:
    """Auto detect should support nested roots without relying on English keys."""

    async def _handle_prices(call: ServiceCall) -> dict[str, Any]:
        return {
            "a": {
                "b": [
                    {
                        "anfang": "2026-01-01T00:00:00+00:00",
                        "ende": "2026-01-01T00:15:00+00:00",
                        "preis": 1.0,
                    },
                    {
                        "anfang": "2026-01-01T00:15:00+00:00",
                        "ende": "2026-01-01T00:30:00+00:00",
                        "preis": 2.0,
                    },
                ]
            }
        }

    hass.services.async_register(
        "test",
        "foreign_nested_prices",
        _handle_prices,
        supports_response=SupportsResponse.ONLY,
    )

    detected = await async_auto_detect_service_adapter(
        hass, "test.foreign_nested_prices"
    )

    assert detected.root_key == "a.b"
    assert detected.time_key == "anfang"
    assert detected.value_key == "preis"


async def test_aggregation_mode_groups_values(hass: HomeAssistant) -> None:
    """Aggregation should reduce multiple values per slot."""
    hass.states.async_set("sensor.forecast", "ok", {"prices": [1, 2, 3, 4, 5, 6, 7, 8]})
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
            "entity_id": "sensor.forecast",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_VALUES,
            CONF_NAME: "prices",
            CONF_AGGREGATION_MODE: AGGREGATION_MODE_MAX,
        },
    )

    values = await provider.async_values(_window())

    assert values == [2.0, 4.0, 6.0, 8.0]


async def test_clamp_mode_nearest_aligns_timestamps(hass: HomeAssistant) -> None:
    """Clamp nearest should map off-grid timestamps to nearest slot."""
    payload = [
        {"start": "2026-01-01T00:01:00+00:00", "value": 1.0},
        {"start": "2026-01-01T00:16:00+00:00", "value": 2.0},
        {"start": "2026-01-01T00:29:00+00:00", "value": 3.0},
        {"start": "2026-01-01T00:47:00+00:00", "value": 4.0},
    ]
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config=_template_config(payload, **{CONF_CLAMP_MODE: CLAMP_MODE_NEAREST}),
    )

    values = await provider.async_values(_window())

    assert values == [1.0, 2.0, 3.0, 4.0]


async def test_resample_mode_forward_fill_fills_gaps(hass: HomeAssistant) -> None:
    """Forward fill should fill interior missing slots."""
    payload = [
        {"start": "2026-01-01T00:00:00+00:00", "value": 1.0},
        {"start": "2026-01-01T00:30:00+00:00", "value": 3.0},
        {"start": "2026-01-01T00:45:00+00:00", "value": 4.0},
    ]
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config=_template_config(
            payload,
            **{CONF_RESAMPLE_MODE: RESAMPLE_MODE_FORWARD_FILL},
        ),
    )

    values = await provider.async_values(_window())

    assert values == [1.0, 1.0, 3.0, 4.0]


async def test_edge_fill_mode_hold_fills_edges(hass: HomeAssistant) -> None:
    """Edge hold should fill leading and trailing missing slots."""
    payload = [
        {"start": "2026-01-01T00:15:00+00:00", "value": 2.0},
        {"start": "2026-01-01T00:30:00+00:00", "value": 3.0},
    ]
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config=_template_config(
            payload,
            **{CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD},
        ),
    )

    values = await provider.async_values(_window())

    assert values == [2.0, 2.0, 3.0, 3.0]


async def test_all_modifiers_with_irregular_data(hass: HomeAssistant) -> None:
    """Clamping, aggregation, resampling, and edge fill should cooperate."""
    payload = [
        {"start": "2026-01-01T00:20:00+00:00", "value": 10.0},
        {"start": "2026-01-01T00:44:00+00:00", "value": 20.0},
        {"start": "2026-01-01T00:46:00+00:00", "value": 28.0},
    ]
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config=_template_config(
            payload,
            **{
                CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
                CONF_AGGREGATION_MODE: AGGREGATION_MODE_MEAN,
                CONF_RESAMPLE_MODE: RESAMPLE_MODE_LINEAR,
                CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
            },
        ),
    )

    values = await provider.async_values(_window())

    assert values == [10.0, 10.0, 17.0, 24.0]


async def test_template_string_output_raises_error(hass: HomeAssistant) -> None:
    """Template returning a string should raise a parse error."""
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: "{{ 'not-a-list' }}",
        },
    )

    with pytest.raises(SourceProviderError, match="rendered a string"):
        await provider.async_values(_window())


async def test_energy_provider_extends_horizon_from_previous_day(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Energy provider should normalize Wh values and repeat from 24h earlier."""
    entry = MockConfigEntry(
        domain="forecast_solar",
        entry_id="solar-entry",
        title="Solcast",
        state=ConfigEntryState.LOADED,
    )
    entry.async_unload = AsyncMock(return_value=True)
    entry.add_to_hass(hass)

    wh_hours = {
        f"2026-01-01T{hour:02d}:00:00+00:00": hour * 100.0 for hour in range(24)
    }
    monkeypatch.setattr(
        "custom_components.wattplan.source_provider.async_get_energy_solar_forecast_platforms",
        AsyncMock(
            return_value={
                "forecast_solar": AsyncMock(return_value={"wh_hours": wh_hours})
            }
        ),
    )

    provider = EnergySolarForecastSourceProvider(
        hass,
        source_name="pv",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER,
            CONF_CONFIG_ENTRY_ID: entry.entry_id,
            CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
            CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
        },
    )

    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            slot_minutes=60,
            slots=48,
        )
    )

    assert values[:4] == [0.0, 0.1, 0.2, 0.3]
    assert values[24:28] == [0.0, 0.1, 0.2, 0.3]
