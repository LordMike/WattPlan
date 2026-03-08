"""Common fixtures for the WattPlan tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def enable_custom_integrations() -> None:
    """Compatibility fixture for custom-component tests outside hass-core.

    WattPlan imports directly from `src/custom_components`, so the test suite
    does not need Home Assistant's source checkout just to satisfy this common
    fixture name.
    """
    yield


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.wattplan.async_setup_entry", return_value=True
    ) as mock_setup_entry:
        yield mock_setup_entry
