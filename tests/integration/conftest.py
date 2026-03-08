"""Common fixtures for the WattPlan tests."""

from collections.abc import Generator
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/mnt/n/Personal/hass-core/tests/testing_config")


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.wattplan.async_setup_entry", return_value=True
    ) as mock_setup_entry:
        yield mock_setup_entry
