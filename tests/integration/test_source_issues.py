"""Tests for WattPlan source repair issue helpers."""

from __future__ import annotations

from custom_components.wattplan.const import (
    CONF_CLAMP_MODE,
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_RESAMPLE_MODE,
    CONF_SOURCE_PRICE,
    CONF_SOURCES,
)
from custom_components.wattplan.repairs import async_create_fix_flow
from custom_components.wattplan.source_issues import (
    build_source_issue,
    source_fill_defaults_needed,
    source_issue_id,
    update_entry_source_with_fill_defaults,
)

from homeassistant.const import CONF_NAME

from tests.common import MockConfigEntry


def test_build_source_issue_includes_grace_note() -> None:
    """A stale-backed issue should tell the user when coverage expires."""
    entry = MockConfigEntry(domain="wattplan", title="Home", data={CONF_NAME: "Home"})
    issue = build_source_issue(
        entry=entry,
        source_key=CONF_SOURCE_PRICE,
        kind="source_unavailable",
        source_name="price forecast",
        consequence="No new plan can be produced once the temporary coverage expires.",
        expires_at=None,
        available_count=0,
        required_count=24,
        is_fixable=False,
    )

    assert issue.kind == "source_unavailable"
    assert "already affects the current plan" in issue.placeholders["grace_note"]


async def test_incomplete_repair_updates_source_config(
    hass,
) -> None:
    """The repair helper should apply the shared horizon-filling defaults."""
    entry = MockConfigEntry(
        domain="wattplan",
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SOURCES: {
                CONF_SOURCE_PRICE: {
                    CONF_FIXUP_PROFILE: "strict_input",
                }
            },
        },
    )
    entry.add_to_hass(hass)

    changed = update_entry_source_with_fill_defaults(
        hass,
        entry,
        source_key=CONF_SOURCE_PRICE,
    )

    assert changed is True
    updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated_entry is not None
    source_config = updated_entry.data[CONF_SOURCES][CONF_SOURCE_PRICE]
    assert source_config[CONF_FIXUP_PROFILE] == "extend_daily_pattern"
    assert source_config[CONF_CLAMP_MODE] == "nearest"
    assert source_config[CONF_RESAMPLE_MODE] == "linear"
    assert source_config[CONF_EDGE_FILL_MODE] == "hold"
    assert source_fill_defaults_needed(source_config) is False


async def test_incomplete_repair_flow_completes_when_entry_exists(hass) -> None:
    """The repair flow should write the defaults and complete cleanly."""
    entry = MockConfigEntry(
        domain="wattplan",
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SOURCES: {
                CONF_SOURCE_PRICE: {
                    CONF_FIXUP_PROFILE: "strict_input",
                }
            },
        },
    )
    entry.add_to_hass(hass)
    flow = await async_create_fix_flow(
        hass,
        source_issue_id(entry.entry_id, CONF_SOURCE_PRICE, "source_incomplete"),
        {
            "entry_id": entry.entry_id,
            "source_key": CONF_SOURCE_PRICE,
        },
    )
    flow.hass = hass

    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
