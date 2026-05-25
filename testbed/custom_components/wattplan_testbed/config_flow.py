"""Config and subentry flows for the WattPlan testbed integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlowWithReload,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_BASE_OFFSET,
    CONF_CAPACITY_KWH,
    CONF_CLOUD_FACTOR,
    CONF_DEFAULT_SOC_PCT,
    CONF_FACTOR,
    CONF_INITIAL_TOTAL_KWH,
    CONF_NOISE,
    CONF_PEAK_KWH,
    CONF_PHASE_HOURS,
    CONF_PROFILE,
    CONF_SEED,
    CONF_SLOT_MINUTES,
    CONF_UPDATE_INTERVAL_MINUTES,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    GENERATOR_LOAD,
    GENERATOR_PRICE,
    GENERATOR_PV,
    SUBENTRY_BATTERY,
    SUBENTRY_LOAD,
    SUBENTRY_PRICE,
    SUBENTRY_PV,
)


def _number(default: float, *, min_value: float, max_value: float, step: float) -> Any:
    """Return a number selector field."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=min_value,
            max=max_value,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _entry_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return config-entry schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "WattPlan Testbed")): selector.TextSelector(),
            vol.Required(
                CONF_UPDATE_INTERVAL_MINUTES,
                default=int(
                    defaults.get(
                        CONF_UPDATE_INTERVAL_MINUTES,
                        defaults.get(CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES),
                    )
                ),
            ): _number(DEFAULT_UPDATE_INTERVAL_MINUTES, min_value=5, max_value=60, step=5),
        }
    )


def normalize_entry_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return root entry data, migrating old slot fields."""
    return {
        CONF_NAME: str(data.get(CONF_NAME, "WattPlan Testbed")),
        CONF_UPDATE_INTERVAL_MINUTES: int(
            data.get(
                CONF_UPDATE_INTERVAL_MINUTES,
                data.get(CONF_SLOT_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES),
            )
        ),
    }


def _price_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return price subentry schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Price")): selector.TextSelector(),
            vol.Required(CONF_BASE_OFFSET, default=float(defaults.get(CONF_BASE_OFFSET, 0.3))): _number(0.3, min_value=-5, max_value=5, step=0.01),
            vol.Required(CONF_FACTOR, default=float(defaults.get(CONF_FACTOR, 1.0))): _number(1.0, min_value=-10, max_value=10, step=0.05),
            vol.Required(CONF_NOISE, default=float(defaults.get(CONF_NOISE, 0.03))): _number(0.03, min_value=0, max_value=5, step=0.01),
            vol.Required(CONF_PHASE_HOURS, default=float(defaults.get(CONF_PHASE_HOURS, 0.0))): _number(0.0, min_value=-24, max_value=24, step=0.25),
            vol.Required(CONF_SEED, default=int(defaults.get(CONF_SEED, 1))): _number(1, min_value=0, max_value=999999, step=1),
        }
    )


def _pv_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return PV subentry schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "PV")): selector.TextSelector(),
            vol.Required(CONF_PEAK_KWH, default=float(defaults.get(CONF_PEAK_KWH, 1.2))): _number(1.2, min_value=0, max_value=100, step=0.1),
            vol.Required(CONF_CLOUD_FACTOR, default=float(defaults.get(CONF_CLOUD_FACTOR, 0.15))): _number(0.15, min_value=0, max_value=1, step=0.05),
            vol.Required(CONF_FACTOR, default=float(defaults.get(CONF_FACTOR, 1.0))): _number(1.0, min_value=0, max_value=10, step=0.05),
            vol.Required(CONF_PHASE_HOURS, default=float(defaults.get(CONF_PHASE_HOURS, 0.0))): _number(0.0, min_value=-24, max_value=24, step=0.25),
            vol.Required(CONF_SEED, default=int(defaults.get(CONF_SEED, 11))): _number(11, min_value=0, max_value=999999, step=1),
        }
    )


def _load_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return load subentry schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Load")): selector.TextSelector(),
            vol.Required(CONF_PROFILE, default=defaults.get(CONF_PROFILE, "medium")): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="light", label="Light"),
                        selector.SelectOptionDict(value="medium", label="Medium"),
                        selector.SelectOptionDict(value="heavy", label="Heavy"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(CONF_FACTOR, default=float(defaults.get(CONF_FACTOR, 1.0))): _number(1.0, min_value=0, max_value=10, step=0.05),
            vol.Required(CONF_NOISE, default=float(defaults.get(CONF_NOISE, 0.04))): _number(0.04, min_value=0, max_value=5, step=0.01),
            vol.Required(CONF_PHASE_HOURS, default=float(defaults.get(CONF_PHASE_HOURS, 0.0))): _number(0.0, min_value=-24, max_value=24, step=0.25),
            vol.Required(CONF_INITIAL_TOTAL_KWH, default=float(defaults.get(CONF_INITIAL_TOTAL_KWH, 1000.0))): _number(1000.0, min_value=0, max_value=1000000, step=1),
            vol.Required(CONF_SEED, default=int(defaults.get(CONF_SEED, 23))): _number(23, min_value=0, max_value=999999, step=1),
        }
    )


def _battery_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return battery subentry schema."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Battery")): selector.TextSelector(),
            vol.Required(CONF_CAPACITY_KWH, default=float(defaults.get(CONF_CAPACITY_KWH, 10.0))): _number(10.0, min_value=0.1, max_value=1000, step=0.1),
            vol.Required(CONF_DEFAULT_SOC_PCT, default=float(defaults.get(CONF_DEFAULT_SOC_PCT, 50.0))): _number(50.0, min_value=0, max_value=100, step=1),
            vol.Required(CONF_SEED, default=int(defaults.get(CONF_SEED, 101))): _number(101, min_value=0, max_value=999999, step=1),
        }
    )


def _slug(value: str) -> str:
    """Return stable slug."""
    return slugify(value) or "asset"


class WattPlanTestbedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle WattPlan testbed config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "TestbedOptionsFlow":
        """Return root configure/options flow."""
        return TestbedOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentry flow handlers."""
        return {
            SUBENTRY_BATTERY: BatterySubentryFlow,
            SUBENTRY_LOAD: LoadSubentryFlow,
            SUBENTRY_PRICE: PriceSubentryFlow,
            SUBENTRY_PV: PvSubentryFlow,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Create the testbed root entry."""
        if user_input is not None:
            data = normalize_entry_data(user_input)
            return self.async_create_entry(title=str(data[CONF_NAME]), data=data)
        return self.async_show_form(step_id="user", data_schema=_entry_schema())


class TestbedOptionsFlow(OptionsFlowWithReload):
    """Handle root testbed configuration edits."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._data = normalize_entry_data(dict(config_entry.data))

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit root testbed settings."""
        return await self.async_step_configure(user_input)

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit root testbed settings."""
        if user_input is not None:
            self._data = normalize_entry_data(user_input)
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                title=str(self._data[CONF_NAME]),
                data=self._data,
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="configure",
            data_schema=self.add_suggested_values_to_schema(
                _entry_schema(self._data), self._data
            ),
        )


class _BaseSubentryFlow(ConfigSubentryFlow):
    """Base flow for simple testbed subentries."""

    subentry_type: str
    title_prefix: str
    generator_name: str | None = None

    def schema(self, defaults: dict[str, Any] | None = None) -> vol.Schema:
        """Return schema for this subentry."""
        raise NotImplementedError

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create one subentry."""
        if user_input is not None:
            data = dict(user_input)
            if self.generator_name is not None:
                data["generator"] = self.generator_name
            name = str(data[CONF_NAME])
            return self.async_create_entry(
                title=f"{self.title_prefix}: {name}",
                data=data,
                unique_id=f"{self.subentry_type}:{_slug(name)}",
            )
        return self.async_show_form(step_id="user", data_schema=self.schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure one subentry."""
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)
        if user_input is not None:
            data = dict(user_input)
            if self.generator_name is not None:
                data["generator"] = self.generator_name
            name = str(data[CONF_NAME])
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                title=f"{self.title_prefix}: {name}",
                data=data,
                unique_id=f"{self.subentry_type}:{_slug(name)}",
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self.schema(defaults), defaults
            ),
        )


class PriceSubentryFlow(_BaseSubentryFlow):
    """Create a fake price source."""

    subentry_type = SUBENTRY_PRICE
    title_prefix = "Price"
    generator_name = GENERATOR_PRICE

    def schema(self, defaults: dict[str, Any] | None = None) -> vol.Schema:
        """Return price schema."""
        return _price_schema(defaults)


class PvSubentryFlow(_BaseSubentryFlow):
    """Create a fake PV source."""

    subentry_type = SUBENTRY_PV
    title_prefix = "PV"
    generator_name = GENERATOR_PV

    def schema(self, defaults: dict[str, Any] | None = None) -> vol.Schema:
        """Return PV schema."""
        return _pv_schema(defaults)


class LoadSubentryFlow(_BaseSubentryFlow):
    """Create a fake load source."""

    subentry_type = SUBENTRY_LOAD
    title_prefix = "Load"
    generator_name = GENERATOR_LOAD

    def schema(self, defaults: dict[str, Any] | None = None) -> vol.Schema:
        """Return load schema."""
        return _load_schema(defaults)


class BatterySubentryFlow(_BaseSubentryFlow):
    """Create a fake battery."""

    subentry_type = SUBENTRY_BATTERY
    title_prefix = "Battery"

    def schema(self, defaults: dict[str, Any] | None = None) -> vol.Schema:
        """Return battery schema."""
        return _battery_schema(defaults)
