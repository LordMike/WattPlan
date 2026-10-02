"""Tests for turning timestamped source points into one value per planner slot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from custom_components.wattplan.const import (
    AGGREGATION_MODE_FIRST,
    AGGREGATION_MODE_LAST,
    CLAMP_MODE_NEAREST,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_CONFIG_ENTRY_ID,
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_RESAMPLE_MODE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_TEMPLATE,
    EDGE_FILL_MODE_HOLD,
    FIXUP_PROFILE_REPAIR,
    FIXUP_PROFILE_STRICT,
    RESAMPLE_MODE_FORWARD_FILL,
    RESAMPLE_MODE_LINEAR,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_TEMPLATE,
)
from custom_components.wattplan.source_config.provider import build_source_value_provider
from custom_components.wattplan.source_provider import TemplateAdapterSourceProvider
from custom_components.wattplan.source_types import SourceProviderError, SourceWindow
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _window(*, slot_minutes: int = 15, slots: int = 4) -> SourceWindow:
    """Return a planner window starting at START."""
    return SourceWindow(start_at=START, slot_minutes=slot_minutes, slots=slots)


def _points(step_minutes: int, values: list[float], *, offset_minutes: int = 0) -> list[dict]:
    """Return evenly spaced point objects starting at START + offset."""
    return [
        {
            "start": (
                START + timedelta(minutes=offset_minutes + step_minutes * index)
            ).isoformat(),
            "value": value,
        }
        for index, value in enumerate(values)
    ]


def _template_provider(
    hass: HomeAssistant,
    source_name: str,
    payload: list[dict],
    **extra: object,
) -> TemplateAdapterSourceProvider:
    """Return a template-backed provider for one source."""
    return TemplateAdapterSourceProvider(
        hass,
        source_name=source_name,
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ {payload!r} }}}}",
            **extra,
        },
    )


async def test_past_points_do_not_land_on_current_slot(hass: HomeAssistant) -> None:
    """Already-elapsed points must not snap onto slot 0 with nearest clamp."""
    payload = _points(15, [0.5, 0.75, 1.0, 2.0, 3.0, 4.0], offset_minutes=-30)
    provider = _template_provider(
        hass,
        CONF_SOURCE_IMPORT_PRICE,
        payload,
        **{
            CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
            CONF_AGGREGATION_MODE: AGGREGATION_MODE_FIRST,
        },
    )

    assert await provider.async_values(_window()) == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize("source_name", [CONF_SOURCE_IMPORT_PRICE, CONF_SOURCE_EXPORT_PRICE])
async def test_hourly_price_repeats_across_quarter_hour_slots(
    hass: HomeAssistant, source_name: str
) -> None:
    """A per-kWh price applies unchanged to every slot its hour covers."""
    provider = _template_provider(hass, source_name, _points(60, [0.30, 0.40]))

    values = await provider.async_values(_window(slots=8))

    assert values == pytest.approx([0.30] * 4 + [0.40] * 4)


@pytest.mark.parametrize(
    ("aggregation_mode", "expected_first_hour"),
    [(AGGREGATION_MODE_FIRST, 0.50), (AGGREGATION_MODE_LAST, 0.30)],
)
async def test_duplicate_timestamps_follow_payload_order(
    hass: HomeAssistant, aggregation_mode: str, expected_first_hour: float
) -> None:
    """First/last pick by payload order, and duplicates keep interval coverage."""
    payload = [
        {"start": START.isoformat(), "value": 0.50},
        {"start": START.isoformat(), "value": 0.30},
        {"start": (START + timedelta(hours=1)).isoformat(), "value": 0.40},
    ]
    provider = _template_provider(
        hass,
        CONF_SOURCE_IMPORT_PRICE,
        payload,
        **{CONF_AGGREGATION_MODE: aggregation_mode},
    )

    values = await provider.async_values(_window(slots=8))

    assert values == pytest.approx([expected_first_hour] * 4 + [0.40] * 4)


def _holey_hourly_prices() -> list[dict]:
    """Return hourly prices with 02:00 and 03:00 missing."""
    return [
        point
        for point in _points(60, [1.0, 2.0, 0.0, 0.0, 5.0, 6.0])
        if point["value"] != 0.0
    ]


async def test_multi_hour_hole_fails_under_strict_profile(hass: HomeAssistant) -> None:
    """A hole is not hidden by holding the previous value when strict."""
    provider = build_source_value_provider(
        hass,
        source_key=CONF_SOURCE_IMPORT_PRICE,
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ {_holey_hourly_prices()!r} }}}}",
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_STRICT,
            CONF_RESAMPLE_MODE: RESAMPLE_MODE_FORWARD_FILL,
            CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
        },
    )

    with pytest.raises(SourceProviderError) as err:
        await provider.async_values(_window(slot_minutes=60, slots=6))

    assert err.value.details["available_count"] == 4
    assert err.value.details["required_count"] == 6


@pytest.mark.parametrize(
    ("resample_mode", "expected"),
    [
        (RESAMPLE_MODE_FORWARD_FILL, [1.0, 2.0, 2.0, 2.0, 5.0, 6.0]),
        (RESAMPLE_MODE_LINEAR, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
    ],
)
async def test_multi_hour_hole_is_left_to_resample_mode(
    hass: HomeAssistant, resample_mode: str, expected: list[float]
) -> None:
    """The configured resample mode, not interval stretching, fills a hole."""
    provider = build_source_value_provider(
        hass,
        source_key=CONF_SOURCE_IMPORT_PRICE,
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ {_holey_hourly_prices()!r} }}}}",
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_REPAIR,
            CONF_RESAMPLE_MODE: resample_mode,
        },
    )

    values = await provider.async_values(_window(slot_minutes=60, slots=6))

    assert values == pytest.approx(expected)


async def test_energy_provider_night_without_periods_is_zero(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Energy forecasts that skip night periods mean zero, not missing data."""
    entry = MockConfigEntry(
        domain="forecast_solar",
        entry_id="solar-entry",
        title="Forecast.Solar",
        state=ConfigEntryState.LOADED,
    )
    entry.async_unload = AsyncMock(return_value=True)
    entry.add_to_hass(hass)
    wh_hours = {
        "2026-01-01T00:00:00+00:00": 500.0,
        "2026-01-01T01:00:00+00:00": 400.0,
        "2026-01-01T04:00:00+00:00": 300.0,
        "2026-01-01T05:00:00+00:00": 200.0,
    }
    monkeypatch.setattr(
        "custom_components.wattplan.source_providers.payloads.async_get_energy_solar_forecast_platforms",
        AsyncMock(
            return_value={
                "forecast_solar": AsyncMock(return_value={"wh_hours": wh_hours})
            }
        ),
    )
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name=CONF_SOURCE_PV,
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER,
            CONF_CONFIG_ENTRY_ID: entry.entry_id,
        },
    )

    values = await provider.async_values(_window(slot_minutes=60, slots=6))

    assert values == pytest.approx([0.5, 0.4, 0.0, 0.0, 0.3, 0.2])


@pytest.mark.parametrize("source_name", [CONF_SOURCE_USAGE, CONF_SOURCE_PV])
async def test_hourly_energy_splits_across_quarter_hour_slots(
    hass: HomeAssistant, source_name: str
) -> None:
    """Energy per hour is divided over the quarter-hour slots it covers."""
    provider = _template_provider(hass, source_name, _points(60, [1.0, 2.0]))

    values = await provider.async_values(_window(slots=8))

    assert values == pytest.approx([0.25] * 4 + [0.5] * 4)
