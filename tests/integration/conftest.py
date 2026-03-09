"""Common fixtures for the WattPlan tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.wattplan.async_setup_entry", return_value=True
    ) as mock_setup_entry, patch(
        "custom_components.wattplan.async_unload_entry", return_value=True
    ):
        yield mock_setup_entry
