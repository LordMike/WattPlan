"""Tests for WattPlan source fixup behavior."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.wattplan.const import (
    ADAPTER_TYPE_ATTRIBUTE_VALUES,
    AGGREGATION_MODE_MEAN,
    CLAMP_MODE_NEAREST,
    CONF_ADAPTER_TYPE,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_EDGE_FILL_MODE,
    CONF_RESAMPLE_MODE,
    CONF_SOURCE_MODE,
    CONF_TEMPLATE,
    EDGE_FILL_MODE_HOLD,
    FIXUP_PROFILE_EXTEND,
    FIXUP_PROFILE_STRICT,
    RESAMPLE_MODE_LINEAR,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_TEMPLATE,
)
from custom_components.wattplan.source_fixup import (
    SourceFixupProvider,
    SourceHealthKind,
    effective_provider_config,
)
from custom_components.wattplan.source_provider import TemplateAdapterSourceProvider
from custom_components.wattplan.source_types import (
    SourceProvider,
    SourceProviderError,
    SourceWindow,
)
import pytest

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant


def _window(*, slots: int) -> SourceWindow:
    """Return a common planner window."""
    return SourceWindow(
        start_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        slot_minutes=60,
        slots=slots,
    )


class _StaticProvider(SourceProvider):
    """Return fixed values or fail with a fixed error."""

    def __init__(
        self,
        *,
        values: list[float] | None = None,
        error: SourceProviderError | None = None,
    ) -> None:
        """Initialize static provider."""
        self._values = values
        self._error = error

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return the configured values or raise the configured error."""
        if self._error is not None:
            raise self._error
        assert self._values is not None
        return self._values[: window.slots]


class _PartialThenBaseDayProvider(SourceProvider):
    """Fail for full horizon and return one day when asked for one day."""

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return one day's data for the repair pass only."""
        if window.slots == 24:
            return [float(index) for index in range(24)]
        raise SourceProviderError(
            "source_validation",
            "not enough values",
            details={"available_count": 24},
        )

async def test_strict_fixup_re_raises_provider_error() -> None:
    """Strict fixup should not try to invent missing tail values."""
    error = SourceProviderError(
        "source_validation",
        "not enough values",
        details={"available_count": 8},
    )
    provider = SourceFixupProvider(_StaticProvider(error=error), profile=FIXUP_PROFILE_STRICT)

    with pytest.raises(SourceProviderError) as err:
        await provider.async_values(_window(slots=48))

    assert err.value is error


async def test_extend_fixup_repeats_t_minus_24h() -> None:
    """Extend profile should repeat values from 24 hours earlier."""
    provider = SourceFixupProvider(
        _PartialThenBaseDayProvider(),
        profile=FIXUP_PROFILE_EXTEND,
    )

    values = await provider.async_values(_window(slots=48))

    assert len(values) == 48
    assert values[:24] == [float(index) for index in range(24)]
    assert values[24:] == [float(index) for index in range(24)]
    assert provider.last_health.kind is SourceHealthKind.OK
    assert provider.last_health.using_stale is False


async def test_extend_fixup_requires_full_day_of_base_values() -> None:
    """Extend profile should fail if it cannot obtain one day to repeat."""

    class _TooShortProvider(SourceProvider):
        async def async_values(self, window: SourceWindow) -> list[float]:
            if window.slots == 12:
                return [1.0] * 12
            raise SourceProviderError(
                "source_validation",
                "not enough values",
                details={"available_count": 12},
            )

    provider = SourceFixupProvider(_TooShortProvider(), profile=FIXUP_PROFILE_EXTEND)

    with pytest.raises(SourceProviderError, match="not enough values"):
        await provider.async_values(_window(slots=48))


async def test_fixup_marks_unavailable_when_stale_cache_covers_failure() -> None:
    """A transient fetch failure should be classified as unavailable with grace."""

    class _FlakyProvider(SourceProvider):
        def __init__(self) -> None:
            self._calls = 0

        async def async_values(self, window: SourceWindow) -> list[float]:
            self._calls += 1
            if self._calls == 1:
                return [float(index) for index in range(window.slots)]
            raise SourceProviderError("source_fetch", "fetch failed")

    provider = SourceFixupProvider(_FlakyProvider(), profile=FIXUP_PROFILE_EXTEND)

    await provider.async_values(_window(slots=24))
    values = await provider.async_values(_window(slots=12))

    assert len(values) == 12
    assert provider.last_health.kind is SourceHealthKind.UNAVAILABLE
    assert provider.last_health.using_stale is True
    assert provider.last_health.expires_at == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


async def test_fixup_marks_incomplete_when_some_intervals_are_missing() -> None:
    """A short source should be classified as incomplete when repair fails."""

    provider = SourceFixupProvider(
        _StaticProvider(
            error=SourceProviderError(
                "source_validation",
                "not enough values",
                details={"available_count": 6, "required_count": 24},
            )
        ),
        profile=FIXUP_PROFILE_STRICT,
    )

    with pytest.raises(SourceProviderError, match="not enough values"):
        await provider.async_values(_window(slots=24))

    assert provider.last_health.kind is SourceHealthKind.INCOMPLETE
    assert provider.last_health.available_count == 6
    assert provider.last_health.required_count == 24


def test_effective_provider_config_disables_fill_in_strict_profile() -> None:
    """Strict profile should turn off provider-side reshaping helpers."""
    source_config = {
        CONF_RESAMPLE_MODE: RESAMPLE_MODE_LINEAR,
        CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
        "fixup_profile": FIXUP_PROFILE_STRICT,
    }

    effective = effective_provider_config(source_config)

    assert effective[CONF_RESAMPLE_MODE] != RESAMPLE_MODE_LINEAR
    assert effective[CONF_EDGE_FILL_MODE] != EDGE_FILL_MODE_HOLD


async def test_template_provider_and_fixup_pipeline_cover_full_horizon(
    hass: HomeAssistant,
) -> None:
    """One end-to-end pipeline test should cover provider plus fixup together."""
    payload = [float(index) for index in range(24)]
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="usage",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ {payload!r} }}}}",
        },
    )

    values = await SourceFixupProvider(
        provider,
        profile=FIXUP_PROFILE_EXTEND,
    ).async_values(_window(slots=48))

    assert len(values) == 48
    assert values[:24] == payload
    assert values[24:] == payload


async def test_adapter_pipeline_keeps_advanced_provider_settings(
    hass: HomeAssistant,
) -> None:
    """Advanced provider settings should still apply before fixup runs."""
    hass.states.async_set(
        "sensor.forecast",
        "ok",
        {"prices": [1, 3, 5, 7, 9, 11, 13, 15]},
    )
    provider = TemplateAdapterSourceProvider(
        hass,
        source_name="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
            "entity_id": ["sensor.forecast"],
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_VALUES,
            CONF_NAME: "prices",
            CONF_AGGREGATION_MODE: AGGREGATION_MODE_MEAN,
            CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
            CONF_RESAMPLE_MODE: RESAMPLE_MODE_LINEAR,
            CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
        },
    )

    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            slot_minutes=120,
            slots=4,
        )
    )

    assert values == [2.0, 6.0, 10.0, 14.0]
