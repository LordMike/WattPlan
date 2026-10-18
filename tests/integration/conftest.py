"""Common fixtures for the WattPlan tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.wattplan.optimizer.planners.presets import run_preset


@pytest.fixture(autouse=True)
def inline_planner_transport(hass):
    """Keep integration unit tests' optimizer mocks in process.

    Real process startup, cancellation and native solver execution are covered
    separately by test_planner_process.py and the lifecycle transport tests.
    """
    async def run(params, preset):
        return await hass.async_add_executor_job(run_preset, params, preset)

    with patch("custom_components.wattplan.coordinator.async_run_preset", run):
        yield


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.wattplan.async_setup_entry", return_value=True
    ) as mock_setup_entry, patch(
        "custom_components.wattplan.async_unload_entry", return_value=True
    ):
        yield mock_setup_entry


@pytest.fixture
def frozen_clock(freezer):
    """Freeze the clock mid-hour so hour-aligned test data cannot straddle a boundary."""
    freezer.move_to("2026-06-15 12:30:00+00:00")
    return freezer
