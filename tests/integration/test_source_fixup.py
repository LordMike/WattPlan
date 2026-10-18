"""Tests for WattPlan source fixup behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    EDGE_FILL_MODE_HOLD,
    FIXUP_PROFILE_EXTEND,
    FIXUP_PROFILE_REPAIR,
    FIXUP_PROFILE_STRICT,
    RESAMPLE_MODE_LINEAR,
    SOURCE_MODE_ENTITY_ADAPTER,
)
from custom_components.wattplan.source_config.provider import build_source_value_provider
from custom_components.wattplan.source_fixup import (
    SourceFixupProvider,
    SourceHealthKind,
)
from custom_components.wattplan.source_pipeline import build_source_base_provider
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
    assert provider.last_health.kind is SourceHealthKind.INCOMPLETE
    assert provider.last_health.available_count == 8
    assert provider.last_health.required_count == 48


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


class _SucceedOnceProvider(SourceProvider):
    """Return the first window as 0..n-1 and fail on every later call."""

    def __init__(self) -> None:
        """Initialize the stateful provider."""
        self._calls = 0

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return the first window and raise a fetch error afterwards."""
        self._calls += 1
        if self._calls == 1:
            return [float(index) for index in range(window.slots)]
        raise SourceProviderError("source_fetch", "temporary failure")


def _later_window(*, hours: int, slots: int = 24) -> SourceWindow:
    """Return a window starting the given hours after the first one."""
    return SourceWindow(
        start_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(hours=hours),
        slot_minutes=60,
        slots=slots,
    )


async def test_extend_stale_reuse_shifts_extends_and_expires() -> None:
    """Extend profile shifts the remembered day, repeats it to fill, then expires."""
    provider = SourceFixupProvider(_SucceedOnceProvider(), profile=FIXUP_PROFILE_EXTEND)
    await provider.async_values(_window(slots=24))

    assert await provider.async_values(_later_window(hours=1)) == [
        float(index) for index in range(1, 24)
    ] + [0.0]
    assert provider.last_health.using_stale is True
    assert await provider.async_values(_later_window(hours=23)) == [23.0] + [
        float(index) for index in range(23)
    ]

    # The remembered day has fully elapsed, so nothing is left to reuse.
    with pytest.raises(SourceProviderError, match="temporary failure"):
        await provider.async_values(_later_window(hours=24))
    assert provider.last_health.using_stale is False


@pytest.mark.parametrize("profile", [FIXUP_PROFILE_STRICT, FIXUP_PROFILE_REPAIR])
async def test_non_extend_stale_reuse_only_bridges_covered_window(profile: str) -> None:
    """Without Extend, stale values are reused only while they cover the whole request."""
    provider = SourceFixupProvider(_SucceedOnceProvider(), profile=profile)
    await provider.async_values(_window(slots=24))

    # The next slot's 23 remaining values still cover a 23-slot request.
    assert await provider.async_values(_later_window(hours=1, slots=23)) == [
        float(index) for index in range(1, 24)
    ]
    assert provider.last_health.using_stale is True

    # A full-length request now reaches past the remembered window and fails.
    with pytest.raises(SourceProviderError, match="temporary failure"):
        await provider.async_values(_later_window(hours=1))
    assert provider.last_health.using_stale is False


async def test_real_provider_nonfinite_refresh_uses_clean_cache_and_recovers(
    hass: HomeAssistant,
) -> None:
    """Invalid refreshes must use, but never replace, the last finite cache."""
    hass.states.async_set(
        "sensor.forecast",
        "ok",
        {"prices": [1.0, 2.0, 3.0, 4.0]},
    )
    provider = build_source_value_provider(
        hass,
        source_key="price",
        source_config={
            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
            "entity_id": "sensor.forecast",
            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_VALUES,
            CONF_NAME: "prices",
            "fixup_profile": FIXUP_PROFILE_EXTEND,
        },
    )
    window = _window(slots=4)

    assert await provider.async_values(window) == [1.0, 2.0, 3.0, 4.0]

    hass.states.async_set(
        "sensor.forecast",
        "ok",
        {"prices": [float("nan"), 20.0, 30.0, 40.0]},
    )
    assert await provider.async_values(window) == [1.0, 2.0, 3.0, 4.0]
    assert isinstance(provider, SourceFixupProvider)
    assert provider.last_health.kind is SourceHealthKind.UNAVAILABLE
    assert provider.last_health.using_stale is True
    assert provider.last_health.error_code == "source_parse"
    assert provider.last_health.reason == "nonfinite_value"

    hass.states.async_set(
        "sensor.forecast",
        "ok",
        {"prices": [5.0, 6.0, 7.0, 8.0]},
    )
    assert await provider.async_values(window) == [5.0, 6.0, 7.0, 8.0]
    assert provider.last_health.kind is SourceHealthKind.OK
    assert provider.last_health.using_stale is False

    hass.states.async_set(
        "sensor.forecast",
        "ok",
        {"prices": [float("inf"), 60.0, 70.0, 80.0]},
    )
    assert await provider.async_values(window) == [5.0, 6.0, 7.0, 8.0]


async def test_fixup_rejects_nonfinite_direct_provider_values() -> None:
    """The shared fixup boundary must protect direct providers and the cache."""
    provider = SourceFixupProvider(
        _StaticProvider(values=[float("nan")] * 4),
        profile=FIXUP_PROFILE_STRICT,
    )

    with pytest.raises(SourceProviderError, match="non-finite") as err:
        await provider.async_values(_window(slots=4))

    assert err.value.code == "source_parse"
    assert err.value.details["provider_reason"] == "nonfinite_value"
    assert provider.last_health.kind is SourceHealthKind.UNAVAILABLE
    assert provider.last_health.using_stale is False


async def test_adapter_pipeline_keeps_advanced_provider_settings(
    hass: HomeAssistant,
) -> None:
    """Advanced provider settings should still apply before fixup runs."""
    hass.states.async_set(
        "sensor.forecast",
        "ok",
        {"prices": [1, 3, 5, 7, 9, 11, 13, 15]},
    )
    # Route through the shared pipeline so multi-entity list config is composed
    # the same way in tests as it is in runtime planning and flow review.
    provider = build_source_base_provider(
        hass,
        source_key="import_price",
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
