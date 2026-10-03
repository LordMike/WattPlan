"""Unit tests for source defaults and recommendation helpers."""

import pytest

from custom_components.wattplan.const import (
    AGGREGATION_MODE_FIRST,
    CLAMP_MODE_NEAREST,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_PROVIDERS,
    CONF_RESAMPLE_MODE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    EDGE_FILL_MODE_HOLD,
    FIXUP_PROFILE_EXTEND,
    RESAMPLE_MODE_LINEAR,
    RESAMPLE_MODE_NONE,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
)
from custom_components.wattplan.source_config.presets import (
    apply_horizon_fill_defaults,
    auto_detect_step_defaults,
    default_modifier_values,
    preferred_source_mode,
    source_base_defaults,
    source_fill_defaults_needed,
)


def test_default_modifier_values_do_not_resample() -> None:
    assert default_modifier_values() == {
        CONF_AGGREGATION_MODE: AGGREGATION_MODE_FIRST,
        CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
        CONF_RESAMPLE_MODE: RESAMPLE_MODE_NONE,
        CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
    }


@pytest.mark.parametrize(
    ("key", "include_not_used", "kwargs", "expected"),
    [
        (CONF_SOURCE_IMPORT_PRICE, True, {}, SOURCE_MODE_ENTITY_ADAPTER),
        (CONF_SOURCE_EXPORT_PRICE, True, {}, SOURCE_MODE_NOT_USED),
        (CONF_SOURCE_EXPORT_PRICE, False, {}, SOURCE_MODE_ENTITY_ADAPTER),
        (CONF_SOURCE_USAGE, True, {"include_built_in": True}, SOURCE_MODE_BUILT_IN),
        (CONF_SOURCE_USAGE, True, {}, SOURCE_MODE_NOT_USED),
        (CONF_SOURCE_USAGE, False, {}, SOURCE_MODE_ENTITY_ADAPTER),
        (
            CONF_SOURCE_PV,
            True,
            {"include_energy_provider": True},
            SOURCE_MODE_ENERGY_PROVIDER,
        ),
        (CONF_SOURCE_PV, True, {}, SOURCE_MODE_NOT_USED),
        (CONF_SOURCE_PV, False, {}, SOURCE_MODE_ENTITY_ADAPTER),
        ("unknown", True, {}, SOURCE_MODE_NOT_USED),
        ("unknown", False, {}, SOURCE_MODE_TEMPLATE),
    ],
)
def test_preferred_source_mode(key, include_not_used, kwargs, expected) -> None:
    assert (
        preferred_source_mode(key, include_not_used=include_not_used, **kwargs)
        == expected
    )


def test_source_base_defaults_merges_provider_and_source_keys() -> None:
    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
        CONF_FIXUP_PROFILE: "repair",
        CONF_PROVIDERS: [{"template": "{{ 1 }}", "name": "p"}],
    }
    defaults = source_base_defaults(source)
    assert defaults["template"] == "{{ 1 }}"
    assert defaults[CONF_FIXUP_PROFILE] == "repair"
    assert CONF_PROVIDERS not in defaults


def test_source_base_defaults_collects_entity_ids_of_all_adapter_providers() -> None:
    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        CONF_PROVIDERS: [
            {"entity_id": "sensor.a"},
            {"entity_id": "sensor.b"},
            {"name": "no entity"},
        ],
    }
    assert source_base_defaults(source)["entity_id"] == ["sensor.a", "sensor.b"]


def test_auto_detect_step_defaults_keeps_auto_mode_and_shows_resolved_keys() -> None:
    defaults = auto_detect_step_defaults(
        {"entity_id": "sensor.a", "adapter_type": "something"},
        {"name": "attr", "time_key": "start", "value_key": "price"},
    )
    assert defaults["entity_id"] == "sensor.a"
    assert defaults["name"] == "attr"
    assert defaults["time_key"] == "start"
    assert defaults["value_key"] == "price"
    assert defaults["adapter_type"] == "auto_detect"


def test_auto_detect_step_defaults_blank_when_nothing_resolved() -> None:
    defaults = auto_detect_step_defaults({}, {})
    assert defaults["name"] == ""
    assert defaults["time_key"] == ""
    assert defaults["value_key"] == ""


def test_apply_horizon_fill_defaults_overrides_only_fill_keys() -> None:
    result = apply_horizon_fill_defaults(
        {CONF_RESAMPLE_MODE: RESAMPLE_MODE_NONE, "keep": 1}
    )
    assert result["keep"] == 1
    assert result[CONF_FIXUP_PROFILE] == FIXUP_PROFILE_EXTEND
    assert result[CONF_RESAMPLE_MODE] == RESAMPLE_MODE_LINEAR


def test_source_fill_defaults_needed_flips_once_defaults_are_applied() -> None:
    source = {CONF_FIXUP_PROFILE: "repair", "other": 1}
    assert source_fill_defaults_needed(source) is True
    assert source_fill_defaults_needed(apply_horizon_fill_defaults(source)) is False
