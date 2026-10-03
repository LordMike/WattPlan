"""Large per-update sensor attributes must stay out of the Recorder."""

from __future__ import annotations

from homeassistant.const import MATCH_ALL

from custom_components.wattplan.sensors.diagnostics import (
    PlanDetailsSensor,
    UsageForecastSensor,
)
from custom_components.wattplan.sensors.outlook import PlanOutlookSensor


def test_usage_forecast_series_is_unrecorded() -> None:
    """The per-slot usage forecast is large and changes every plan."""
    assert "forecast" in UsageForecastSensor._unrecorded_attributes


def test_outlook_per_update_attributes_are_unrecorded() -> None:
    """Outlook fact detail and coverage timestamps change on every plan."""
    unrecorded = PlanOutlookSensor._unrecorded_attributes
    assert {"fact_details", "statements", "covered_start", "covered_end"} <= unrecorded
    # The prose itself stays recorded so history shows what was advised.
    assert not {"headline", "line_1", "line_2", "text"} & unrecorded


def test_plan_details_still_excludes_everything() -> None:
    """Plan details keep excluding all attributes."""
    assert PlanDetailsSensor._unrecorded_attributes == frozenset({MATCH_ALL})
