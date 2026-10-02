"""Tests for turning timestamped source points into one value per planner slot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.wattplan.const import (
    AGGREGATION_MODE_FIRST,
    CLAMP_MODE_NEAREST,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_TEMPLATE,
    SOURCE_MODE_TEMPLATE,
)
from custom_components.wattplan.source_provider import TemplateAdapterSourceProvider
from custom_components.wattplan.source_types import SourceWindow
import pytest

from homeassistant.core import HomeAssistant

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
