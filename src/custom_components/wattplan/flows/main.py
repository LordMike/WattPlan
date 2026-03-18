"""Main config and options flows for WattPlan."""

from __future__ import annotations

from typing import Any

from .common import _normalize_name, _subentry_display_title, _subentry_name
from .forms import (
    _battery_form_defaults,
    _normalize_battery_input,
    _subentry_name_in_use,
    _subentry_name_in_use_excluding,
    _validate_battery_data,
    _validate_comfort_data,
    _validate_optional_data,
)
from .source_shared import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_HOURS_TO_PLAN,
    CONF_NAME,
    CONF_OPTIMIZER_PROFILE,
    CONF_PLANNING_ENABLED,
    CONF_SLOT_MINUTES,
    CONF_SOURCES,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    DOMAIN,
    OPTIMIZER_PROFILE_BALANCED,
    OptionsFlowWithReload,
    SOURCE_MODE_NOT_USED,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
    _SharedSourceFlow,
    _battery_schema,
    _comfort_schema,
    _core_schema,
    _final_setup_schema,
    _normalize_core_input,
    _optional_schema,
    _source_mode_schema,
    _source_mode_summary,
    _validate_core_data,
    callback,
    deepcopy,
    selector,
    vol,
)

class WattPlanConfigFlow(_SharedSourceFlow, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WattPlan."""

    VERSION = 1
    MINOR_VERSION = 1

    _core: dict[str, Any]
    _entry_options: dict[str, Any]
    _sources: dict[str, dict[str, Any]]
    _last_source_available_count: int | None = None
    _pending_source_key: str | None = None
    _pending_source: dict[str, Any] | None = None
    _pending_source_input: dict[str, Any] | None = None
    _pending_source_step_id: str | None = None
    _pending_source_summary: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WattPlanOptionsFlow:
        """Return the options flow handler."""
        return WattPlanOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this handler."""
        from ..config_flow import (
            BatterySubentryFlowHandler,
            ComfortSubentryFlowHandler,
            OptionalSubentryFlowHandler,
        )

        return {
            SUBENTRY_TYPE_BATTERY: BatterySubentryFlowHandler,
            SUBENTRY_TYPE_COMFORT: ComfortSubentryFlowHandler,
            SUBENTRY_TYPE_OPTIONAL: OptionalSubentryFlowHandler,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show setup requirements before configuration."""
        return await self.async_step_requirements(user_input)

    async def async_step_requirements(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show prerequisites and continue to planner setup."""
        if user_input is not None:
            return await self.async_step_planner_setup()

        return self.async_show_form(
            step_id="requirements",
            data_schema=vol.Schema({}),
            last_step=False,
        )

    async def async_step_planner_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_core_data(user_input, include_name=True)
            if not errors:
                normalized = _normalize_core_input(user_input)
                self._entry_options = {
                    CONF_PLANNING_ENABLED: True,
                    CONF_ACTION_EMISSION_ENABLED: True,
                    CONF_OPTIMIZER_PROFILE: str(
                        normalized.pop(
                            CONF_OPTIMIZER_PROFILE, OPTIMIZER_PROFILE_BALANCED
                        )
                    ),
                }
                self._core = normalized
                self._sources = {}
                return await self.async_step_source_price()

        return self.async_show_form(
            step_id="planner_setup",
            data_schema=self.add_suggested_values_to_schema(
                _core_schema(include_name=True, include_profile=True), user_input or {}
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the price source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            include_not_used=False,
            step_id="source_price",
        )

    async def async_step_source_export_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the export price source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            include_not_used=True,
            step_id="source_export_price",
        )

    async def async_step_source_usage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the usage source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_USAGE,
            user_input,
            include_not_used=True,
            include_built_in=True,
            step_id="source_usage",
        )

    async def async_step_source_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the mode for the PV source."""
        return await self._async_step_source_mode(
            CONF_SOURCE_PV,
            user_input,
            include_not_used=True,
            include_energy_provider=True,
            step_id="source_pv",
        )

    async def _async_step_source_mode(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        include_not_used: bool,
        include_built_in: bool = False,
        include_energy_provider: bool = False,
        step_id: str,
    ) -> ConfigFlowResult:
        """Select source mode and branch to mode specific step."""
        existing = self._stored_source(key)
        include_energy_provider_option = await self._async_include_energy_provider_mode(
            existing,
            include_energy_provider=include_energy_provider,
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            mode = user_input[CONF_SOURCE_MODE]
            branched = await self._async_branch_to_source_mode_step(
                key,
                mode,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider,
            )
            if branched is not None:
                return branched

            if include_not_used and mode == SOURCE_MODE_NOT_USED:
                return await self._async_handle_source_marked_not_used(key)

            errors["base"] = "invalid_source_mode"

        default_mode = self._default_source_mode(
            existing,
            key=key,
            include_not_used=include_not_used,
            include_built_in=include_built_in,
            include_energy_provider_option=include_energy_provider_option,
        )

        return self.async_show_form(
            step_id=step_id,
            data_schema=_source_mode_schema(
                default_mode,
                include_not_used=include_not_used,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider_option,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure price source template."""
        return await self._async_source_template_step(
            CONF_SOURCE_IMPORT_PRICE, user_input, step_id="source_price_template"
        )

    async def async_step_source_export_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure export price source template."""
        return await self._async_source_template_step(
            CONF_SOURCE_EXPORT_PRICE, user_input, step_id="source_export_price_template"
        )

    async def async_step_source_usage_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source template."""
        return await self._async_source_template_step(
            CONF_SOURCE_USAGE, user_input, step_id="source_usage_template"
        )

    async def async_step_source_pv_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source template."""
        return await self._async_source_template_step(
            CONF_SOURCE_PV, user_input, step_id="source_pv_template"
        )

    async def async_step_source_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure price source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_IMPORT_PRICE, user_input, step_id="source_price_adapter"
        )

    async def async_step_source_export_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure export price source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_EXPORT_PRICE, user_input, step_id="source_export_price_adapter"
        )

    async def async_step_source_usage_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_USAGE, user_input, step_id="source_usage_adapter"
        )

    async def async_step_source_pv_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source adapter."""
        return await self._async_step_source_adapter(
            CONF_SOURCE_PV, user_input, step_id="source_pv_adapter"
        )

    async def async_step_source_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure price source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_IMPORT_PRICE, user_input, step_id="source_price_service"
        )

    async def async_step_source_export_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure export price source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_EXPORT_PRICE, user_input, step_id="source_export_price_service"
        )

    async def async_step_source_usage_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_USAGE, user_input, step_id="source_usage_service"
        )

    async def async_step_source_pv_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source service adapter."""
        return await self._async_step_source_service(
            CONF_SOURCE_PV, user_input, step_id="source_pv_service"
        )

    async def async_step_source_pv_energy_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure PV source from an Energy solar forecast provider."""
        return await self._async_step_source_energy_provider(
            CONF_SOURCE_PV, user_input, step_id="source_pv_energy_provider"
        )

    async def async_step_source_usage_built_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure usage source built-in forecast mode."""
        return await self._async_source_built_in_step(user_input)

    async def _async_step_source_adapter(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure source using entity adapter mode."""
        return await self._async_source_adapter_step(key, user_input, step_id=step_id)

    async def _async_step_source_service(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure source using service adapter mode."""
        return await self._async_source_service_step(key, user_input, step_id=step_id)

    async def _async_step_source_energy_provider(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        """Configure source using an Energy solar forecast provider."""
        return await self._async_source_energy_provider_step(
            key, user_input, step_id=step_id
        )

    def _core_data(self) -> dict[str, Any]:
        """Return planner core data for the setup flow."""
        return self._core

    def _stored_source(self, key: str) -> dict[str, Any]:
        """Return persisted setup-flow source data for a source key."""
        return self._sources.get(key, {})

    async def _async_handle_source_marked_not_used(self, key: str) -> ConfigFlowResult:
        """Persist a disabled source in setup flow and continue."""
        self._sources[key] = {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
        return await self._async_after_source_saved(key)

    async def _async_default_source_step(self) -> ConfigFlowResult:
        """Return the first source step when review state is missing."""
        return await self.async_step_source_price()

    async def _async_commit_reviewed_source(
        self, key: str, resolved_pending: dict[str, Any]
    ) -> ConfigFlowResult:
        """Persist the reviewed source and continue setup."""
        self._sources[key] = resolved_pending
        return await self._async_after_source_saved(key)

    def _review_form_last_step(self, key: str) -> bool:
        """Return whether the source review is the final setup form."""
        return self._is_final_source_step(key)

    async def _async_after_source_saved(self, key: str) -> ConfigFlowResult:
        """Continue to the next source or create the config entry."""
        if key == CONF_SOURCE_IMPORT_PRICE:
            return await self.async_step_source_usage()
        if key == CONF_SOURCE_USAGE:
            return await self.async_step_source_pv()
        if key == CONF_SOURCE_PV:
            pv_source = self._sources.get(CONF_SOURCE_PV, {})
            if pv_source.get(CONF_SOURCE_MODE) == SOURCE_MODE_NOT_USED:
                return await self.async_step_setup_complete()
            return await self.async_step_source_export_price()
        return await self.async_step_setup_complete()

    def _is_final_source_step(self, key: str) -> bool:
        """Return if the source config step is the last one before create."""
        if key == CONF_SOURCE_PV:
            pv_source = self._sources.get(CONF_SOURCE_PV, {})
            return pv_source.get(CONF_SOURCE_MODE) == SOURCE_MODE_NOT_USED
        return key == CONF_SOURCE_EXPORT_PRICE

    async def async_step_setup_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show one final setup summary and next actions before entry creation."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._core[CONF_NAME],
                data={
                    **{key: value for key, value in self._core.items() if key != CONF_NAME},
                    CONF_SOURCES: self._sources,
                },
                options=self._entry_options,
            )

        return self.async_show_form(
            step_id="setup_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={
                "setup_name": str(self._core[CONF_NAME]),
                "slot_minutes": str(self._core[CONF_SLOT_MINUTES]),
                "plan_hours": str(self._core[CONF_HOURS_TO_PLAN]),
                "price_source": _source_mode_summary(
                    self._sources.get(CONF_SOURCE_IMPORT_PRICE)
                ),
                "export_price_source": _source_mode_summary(
                    self._sources.get(CONF_SOURCE_EXPORT_PRICE)
                ),
                "usage_source": _source_mode_summary(
                    self._sources.get(CONF_SOURCE_USAGE)
                ),
                "pv_source": _source_mode_summary(self._sources.get(CONF_SOURCE_PV)),
            },
            last_step=True,
        )

class WattPlanOptionsFlow(_SharedSourceFlow, OptionsFlowWithReload):
    """Handle WattPlan options flow."""

    _data: dict[str, Any]
    _options: dict[str, Any]
    _selected_subentry_id: str | None
    _last_source_available_count: int | None
    _pending_source_key: str | None
    _pending_source: dict[str, Any] | None
    _pending_source_input: dict[str, Any] | None
    _pending_source_step_id: str | None
    _pending_source_summary: dict[str, Any] | None

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._data = deepcopy(dict(config_entry.data))
        self._options = deepcopy(dict(config_entry.options))
        self._options.setdefault(CONF_PLANNING_ENABLED, True)
        self._options.setdefault(CONF_ACTION_EMISSION_ENABLED, True)
        self._options.setdefault(
            CONF_OPTIMIZER_PROFILE, OPTIMIZER_PROFILE_BALANCED
        )
        self._selected_subentry_id = None
        self._last_source_available_count = None
        self._pending_source_key = None
        self._pending_source = None
        self._pending_source_input = None
        self._pending_source_step_id = None
        self._pending_source_summary = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu for options."""
        menu_options = [
            "planner_core",
            "planner_timers",
            "source_price",
            "source_usage",
            "source_pv",
            "source_export_price",
        ]

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_planner_core(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit core values."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_core_data(user_input)
            if not errors:
                normalized = _normalize_core_input(user_input)
                self._options[CONF_OPTIMIZER_PROFILE] = str(
                    normalized.pop(CONF_OPTIMIZER_PROFILE, OPTIMIZER_PROFILE_BALANCED)
                )
                self._data.update(normalized)
                self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, options=self._options
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id="planner_core",
            data_schema=self.add_suggested_values_to_schema(
                _core_schema(
                    {
                        **self._data,
                        CONF_OPTIMIZER_PROFILE: self._options[CONF_OPTIMIZER_PROFILE],
                    },
                    include_profile=True,
                    profile_last=True,
                ),
                user_input or {},
            ),
            errors=errors,
        )

    async def async_step_planner_timers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure timer behavior flags."""
        if user_input is not None:
            self._options.update(user_input)
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=self._options
            )
            return await self.async_step_init()

        return self.async_show_form(
            step_id="planner_timers",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PLANNING_ENABLED,
                        default=self._options[CONF_PLANNING_ENABLED],
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_ACTION_EMISSION_ENABLED,
                        default=self._options[CONF_ACTION_EMISSION_ENABLED],
                    ): selector.BooleanSelector(),
                }
            ),
            description_placeholders={
                "slot_minutes": str(self._data[CONF_SLOT_MINUTES])
            },
        )

    async def async_step_battery_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show battery edit/remove actions."""
        if not self._subentries_by_type(SUBENTRY_TYPE_BATTERY):
            return self.async_abort(reason="nothing_configured")
        return self.async_show_menu(
            step_id="battery_entities",
            menu_options=["battery_edit_select", "battery_remove_select", "init"],
        )

    async def async_step_comfort_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show comfort edit/remove actions."""
        if not self._subentries_by_type(SUBENTRY_TYPE_COMFORT):
            return self.async_abort(reason="nothing_configured")
        return self.async_show_menu(
            step_id="comfort_entities",
            menu_options=["comfort_edit_select", "comfort_remove_select", "init"],
        )

    async def async_step_optional_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show optional edit/remove actions."""
        if not self._subentries_by_type(SUBENTRY_TYPE_OPTIONAL):
            return self.async_abort(reason="nothing_configured")
        return self.async_show_menu(
            step_id="optional_entities",
            menu_options=["optional_edit_select", "optional_remove_select", "init"],
        )

    async def async_step_source_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the price source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            include_not_used=False,
            step_id="source_price",
        )

    async def async_step_source_export_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the export price source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            include_not_used=True,
            step_id="source_export_price",
        )

    async def async_step_source_usage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the usage source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_USAGE,
            user_input,
            include_not_used=True,
            include_built_in=True,
            step_id="source_usage",
        )

    async def async_step_source_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select mode for the PV source in options."""
        return await self._async_step_source_options_mode(
            CONF_SOURCE_PV,
            user_input,
            include_not_used=True,
            include_energy_provider=True,
            step_id="source_pv",
        )

    async def _async_step_source_options_mode(
        self,
        key: str,
        user_input: dict[str, Any] | None,
        *,
        include_not_used: bool,
        include_built_in: bool = False,
        include_energy_provider: bool = False,
        step_id: str,
    ) -> ConfigFlowResult:
        """Select source mode for options flow and branch."""
        errors: dict[str, str] = {}
        existing = self._stored_source(key)
        include_energy_provider_option = await self._async_include_energy_provider_mode(
            existing,
            include_energy_provider=include_energy_provider,
        )

        if user_input is not None:
            mode = user_input[CONF_SOURCE_MODE]
            branched = await self._async_branch_to_source_mode_step(
                key,
                mode,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider,
            )
            if branched is not None:
                return branched

            if include_not_used and mode == SOURCE_MODE_NOT_USED:
                return await self._async_handle_source_marked_not_used(key)

            errors["base"] = "invalid_source_mode"

        return self.async_show_form(
            step_id=step_id,
            data_schema=_source_mode_schema(
                self._default_source_mode(
                    existing,
                    key=key,
                    include_not_used=include_not_used,
                    include_built_in=include_built_in,
                    include_energy_provider_option=include_energy_provider_option,
                ),
                include_not_used=include_not_used,
                include_built_in=include_built_in,
                include_energy_provider=include_energy_provider_option,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_source_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit price source template in options."""
        return await self._async_source_template_step(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            step_id="source_price_template",
        )

    async def async_step_source_export_price_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit export price source template in options."""
        return await self._async_source_template_step(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            step_id="source_export_price_template",
        )

    async def async_step_source_usage_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source template in options."""
        return await self._async_source_template_step(
            CONF_SOURCE_USAGE,
            user_input,
            step_id="source_usage_template",
        )

    async def async_step_source_pv_template(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV source template in options."""
        return await self._async_source_template_step(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_template",
        )

    async def async_step_source_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit price source adapter in options."""
        return await self._async_source_adapter_step(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            step_id="source_price_adapter",
        )

    async def async_step_source_export_price_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit export price source adapter in options."""
        return await self._async_source_adapter_step(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            step_id="source_export_price_adapter",
        )

    async def async_step_source_usage_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source adapter in options."""
        return await self._async_source_adapter_step(
            CONF_SOURCE_USAGE,
            user_input,
            step_id="source_usage_adapter",
        )

    async def async_step_source_pv_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV source adapter in options."""
        return await self._async_source_adapter_step(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_adapter",
        )

    async def async_step_source_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit price source service adapter in options."""
        return await self._async_source_service_step(
            CONF_SOURCE_IMPORT_PRICE,
            user_input,
            step_id="source_price_service",
        )

    async def async_step_source_export_price_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit export price source service adapter in options."""
        return await self._async_source_service_step(
            CONF_SOURCE_EXPORT_PRICE,
            user_input,
            step_id="source_export_price_service",
        )

    async def async_step_source_usage_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source service adapter in options."""
        return await self._async_source_service_step(
            CONF_SOURCE_USAGE,
            user_input,
            step_id="source_usage_service",
        )

    async def async_step_source_pv_service(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV source service adapter in options."""
        return await self._async_source_service_step(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_service",
        )

    async def async_step_source_pv_energy_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit PV Energy solar forecast provider in options."""
        return await self._async_source_energy_provider_step(
            CONF_SOURCE_PV,
            user_input,
            step_id="source_pv_energy_provider",
        )

    async def async_step_source_usage_built_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit usage source built-in forecast mode in options."""
        return await self._async_source_built_in_step(user_input)

    def _core_data(self) -> dict[str, Any]:
        """Return planner core data for the options flow."""
        return self._data

    def _stored_source(self, key: str) -> dict[str, Any]:
        """Return persisted options-flow source data for a source key."""
        return self._data.get(CONF_SOURCES, {}).get(key, {})

    async def _async_handle_source_marked_not_used(self, key: str) -> ConfigFlowResult:
        """Persist a disabled source in options flow and return to menu."""
        sources = dict(self._data.get(CONF_SOURCES, {}))
        sources[key] = {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED}
        self._data[CONF_SOURCES] = sources
        self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
        return await self.async_step_init()

    async def _async_default_source_step(self) -> ConfigFlowResult:
        """Return the menu step when review state is missing."""
        return await self.async_step_init()

    async def _async_commit_reviewed_source(
        self, key: str, resolved_pending: dict[str, Any]
    ) -> ConfigFlowResult:
        """Persist the reviewed source and return to the options menu."""
        sources = dict(self._data.get(CONF_SOURCES, {}))
        sources[key] = resolved_pending
        self._data[CONF_SOURCES] = sources
        self.hass.config_entries.async_update_entry(self.config_entry, data=self._data)
        return await self.async_step_init()

    async def async_step_battery_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select battery subentry to edit."""
        return await self._async_select_item(
            SUBENTRY_TYPE_BATTERY,
            "battery_edit_select",
            self.async_step_battery_edit,
            user_input,
        )

    async def async_step_comfort_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select comfort subentry to edit."""
        return await self._async_select_item(
            SUBENTRY_TYPE_COMFORT,
            "comfort_edit_select",
            self.async_step_comfort_edit,
            user_input,
        )

    async def async_step_optional_edit_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select optional subentry to edit."""
        return await self._async_select_item(
            SUBENTRY_TYPE_OPTIONAL,
            "optional_edit_select",
            self.async_step_optional_edit,
            user_input,
        )

    async def _async_select_item(
        self,
        subentry_type: str,
        step_id: str,
        next_step,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Select a subentry from a configured subentry type."""
        items = self._subentries_by_type(subentry_type)
        if not items:
            return self.async_abort(reason="nothing_configured")
        if user_input is not None:
            self._selected_subentry_id = user_input["item_id"]
            return await next_step()
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("item_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=item.subentry_id,
                                    label=item.title,
                                )
                                for item in items
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_battery_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit selected battery."""
        return await self._async_edit_subentry(
            SUBENTRY_TYPE_BATTERY,
            "battery_edit",
            _battery_schema,
            _validate_battery_data,
            user_input,
        )

    async def async_step_comfort_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit selected comfort subentry."""
        return await self._async_edit_subentry(
            SUBENTRY_TYPE_COMFORT,
            "comfort_edit",
            _comfort_schema,
            _validate_comfort_data,
            user_input,
        )

    async def async_step_optional_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit selected optional subentry."""
        return await self._async_edit_subentry(
            SUBENTRY_TYPE_OPTIONAL,
            "optional_edit",
            _optional_schema,
            _validate_optional_data,
            user_input,
        )

    async def _async_edit_subentry(
        self,
        subentry_type: str,
        step_id: str,
        schema_factory,
        validate_method,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Edit one selected subentry."""
        if self._selected_subentry_id is None:
            return self.async_abort(reason="nothing_configured")
        subentry = self.config_entry.subentries[self._selected_subentry_id]
        errors: dict[str, str] = {}
        defaults = dict(subentry.data)

        if user_input is not None:
            defaults = user_input
            if self._name_in_use(user_input[CONF_NAME], exclude_subentry_id=subentry.subentry_id):
                errors["base"] = "name_not_unique"
            else:
                errors.update(validate_method(user_input))
            if not errors:
                self.hass.config_entries.async_update_subentry(
                    self.config_entry,
                    subentry,
                    data=user_input,
                    title=_subentry_display_title(subentry_type, user_input),
                    unique_id=f"{subentry_type}:{_normalize_name(user_input[CONF_NAME])}",
                )
                return await self.async_step_init()

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema_factory(), defaults),
            errors=errors,
            description_placeholders=(
                {"slot_minutes": str(self._data[CONF_SLOT_MINUTES])}
                if subentry_type == SUBENTRY_TYPE_COMFORT
                else None
            ),
        )

    async def async_step_battery_remove_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select battery subentry to remove."""
        return await self._async_remove_item(
            SUBENTRY_TYPE_BATTERY,
            "battery_remove_select",
            user_input,
        )

    async def async_step_comfort_remove_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select comfort subentry to remove."""
        return await self._async_remove_item(
            SUBENTRY_TYPE_COMFORT,
            "comfort_remove_select",
            user_input,
        )

    async def async_step_optional_remove_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select optional subentry to remove."""
        return await self._async_remove_item(
            SUBENTRY_TYPE_OPTIONAL,
            "optional_remove_select",
            user_input,
        )

    async def _async_remove_item(
        self, subentry_type: str, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Remove subentry from selected type."""
        items = self._subentries_by_type(subentry_type)
        if not items:
            return self.async_abort(reason="nothing_configured")
        if user_input is not None:
            self.hass.config_entries.async_remove_subentry(
                self.config_entry,
                user_input["item_id"],
            )
            return await self.async_step_init()

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("item_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=item.subentry_id,
                                    label=item.title,
                                )
                                for item in items
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    def _subentries_by_type(self, subentry_type: str) -> list[Any]:
        """Return subentries filtered by type."""
        return [
            subentry
            for subentry in self.config_entry.subentries.values()
            if subentry.subentry_type == subentry_type
        ]

    def _name_in_use(self, name: str, *, exclude_subentry_id: str | None = None) -> bool:
        """Return if a subentry title is already in use."""
        wanted = name.casefold()
        for subentry in self.config_entry.subentries.values():
            if subentry.subentry_id == exclude_subentry_id:
                continue
            if _subentry_name(subentry).casefold() == wanted:
                return True
        return False
