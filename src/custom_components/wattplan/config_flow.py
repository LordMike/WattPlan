"""Config flow entrypoints for the WattPlan integration."""

from __future__ import annotations

# ruff: noqa: F401,F403
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.const import CONF_NAME

from .flows import source_shared as _source_shared
from .flows.main import WattPlanConfigFlow, WattPlanOptionsFlow
from .flows.source_shared import *

# Re-exported subentry handlers still rely on many underscore-prefixed helpers.
globals().update(vars(_source_shared))

__all__ = [
    "BatterySubentryFlowHandler",
    "ComfortSubentryFlowHandler",
    "OptionalSubentryFlowHandler",
    "WattPlanConfigFlow",
    "WattPlanOptionsFlow",
    "async_get_energy_solar_forecast_entries",
]

def _subentry_name_in_use(entry: ConfigEntry, name: str) -> bool:
    """Return True if the name is already used by a subentry."""
    wanted = name.casefold()
    return any(
        _subentry_name(subentry).casefold() == wanted
        for subentry in entry.subentries.values()
    )


def _subentry_name_in_use_excluding(
    entry: ConfigEntry, name: str, exclude_subentry_id: str
) -> bool:
    """Return True if the name is used by another subentry."""
    wanted = name.casefold()
    return any(
        subentry.subentry_id != exclude_subentry_id
        and _subentry_name(subentry).casefold() == wanted
        for subentry in entry.subentries.values()
    )


def _validate_battery_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate battery values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    if float(data[CONF_MINIMUM_KWH]) > float(data[CONF_CAPACITY_KWH]):
        errors[CONF_MINIMUM_KWH] = "battery_minimum_exceeds_capacity"
    for field in (CONF_CHARGE_EFFICIENCY, CONF_DISCHARGE_EFFICIENCY):
        if not 0 < float(data[field]) <= 1:
            errors[field] = "battery_efficiency_invalid"
    return errors


def _normalize_battery_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten battery advanced settings into subentry data."""
    data = dict(user_input)
    data.update(data.pop(SECTION_BATTERY_ADVANCED, {}))
    data.setdefault(CONF_CHARGE_EFFICIENCY, 0.9)
    data.setdefault(CONF_DISCHARGE_EFFICIENCY, 0.9)
    data.setdefault(CONF_PREFER_PV_SURPLUS_CHARGING, False)
    return data


def _battery_form_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Return battery defaults shaped for the form schema."""
    defaults = dict(data)
    defaults[SECTION_BATTERY_ADVANCED] = {
        CONF_CHARGE_EFFICIENCY: defaults.get(CONF_CHARGE_EFFICIENCY, 0.9),
        CONF_DISCHARGE_EFFICIENCY: defaults.get(CONF_DISCHARGE_EFFICIENCY, 0.9),
        CONF_PREFER_PV_SURPLUS_CHARGING: defaults.get(
            CONF_PREFER_PV_SURPLUS_CHARGING, False
        ),
    }
    return defaults


def _validate_comfort_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate comfort values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    rolling_window_hours = float(data[CONF_ROLLING_WINDOW_HOURS])
    rolling_window_minutes = int(rolling_window_hours * 60)
    if float(data[CONF_TARGET_ON_HOURS_PER_WINDOW]) > rolling_window_hours:
        errors[CONF_TARGET_ON_HOURS_PER_WINDOW] = "comfort_target_on_hours_invalid"
    if int(data[CONF_MIN_CONSECUTIVE_ON_MINUTES]) > rolling_window_minutes:
        errors[CONF_MIN_CONSECUTIVE_ON_MINUTES] = "comfort_duration_exceeds_window"
    if int(data[CONF_MIN_CONSECUTIVE_OFF_MINUTES]) > rolling_window_minutes:
        errors[CONF_MIN_CONSECUTIVE_OFF_MINUTES] = "comfort_duration_exceeds_window"
    if int(data[CONF_MAX_CONSECUTIVE_OFF_MINUTES]) > rolling_window_minutes:
        errors[CONF_MAX_CONSECUTIVE_OFF_MINUTES] = "comfort_duration_exceeds_window"
    if float(data[CONF_EXPECTED_POWER_KW]) <= 0:
        errors[CONF_EXPECTED_POWER_KW] = "comfort_expected_power_invalid"
    return errors


def _validate_optional_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate optional load values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    energy_kwh = data.get(CONF_ENERGY_KWH)
    if energy_kwh is None:
        errors[CONF_ENERGY_KWH] = "energy_kwh_required"
    elif float(energy_kwh) <= 0:
        errors[CONF_ENERGY_KWH] = "optional_energy_must_be_positive"

    duration_minutes = int(data[CONF_DURATION_MINUTES])
    run_within_minutes = int(data[CONF_RUN_WITHIN_HOURS] * 60)
    min_gap_minutes = int(data[CONF_MIN_OPTION_GAP_MINUTES])
    options_count = int(data[CONF_OPTIONS_COUNT])

    if duration_minutes > run_within_minutes:
        errors[CONF_DURATION_MINUTES] = "optional_duration_exceeds_window"
        return errors

    max_options = _optional_max_distinct_options(
        run_within_minutes, duration_minutes, min_gap_minutes
    )
    if options_count > max_options:
        errors[CONF_OPTIONS_COUNT] = "optional_options_exceed_window"

    return errors


class BatterySubentryFlowHandler(ConfigSubentryFlow):
    """Handle battery subentry flow."""

    _pending_input: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create a battery subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized_input = _normalize_battery_input(user_input)
            if _subentry_name_in_use(self._get_entry(), normalized_input[CONF_NAME]):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_battery_data(normalized_input))
            if not errors:
                self._pending_input = normalized_input
                return await self.async_step_complete()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _battery_schema(), _battery_form_defaults(user_input or {})
            ),
            errors=errors,
        )

    async def async_step_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after creating a battery."""
        if self._pending_input is None:
            return await self.async_step_user()
        if user_input is not None:
            pending = self._pending_input
            self._pending_input = None
            return self.async_create_entry(
                title=_subentry_display_title(SUBENTRY_TYPE_BATTERY, pending),
                data=pending,
                unique_id=f"{SUBENTRY_TYPE_BATTERY}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure a battery subentry."""
        errors: dict[str, str] = {}
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)

        if user_input is not None:
            normalized_input = _normalize_battery_input(user_input)
            if _subentry_name_in_use_excluding(
                self._get_entry(), normalized_input[CONF_NAME], subentry.subentry_id
            ):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_battery_data(normalized_input))

            if not errors:
                self._pending_input = normalized_input
                return await self.async_step_reconfigure_complete()
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _battery_schema(), _battery_form_defaults(defaults)
            ),
            errors=errors,
        )

    async def async_step_reconfigure_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after editing a battery."""
        if self._pending_input is None:
            return await self.async_step_reconfigure()
        if user_input is not None:
            subentry = self._get_reconfigure_subentry()
            pending = self._pending_input
            self._pending_input = None
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                data=pending,
                title=_subentry_display_title(SUBENTRY_TYPE_BATTERY, pending),
                unique_id=f"{SUBENTRY_TYPE_BATTERY}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="reconfigure_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )


class ComfortSubentryFlowHandler(ConfigSubentryFlow):
    """Handle comfort subentry flow."""

    _pending_input: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create a comfort subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _subentry_name_in_use(self._get_entry(), user_input[CONF_NAME]):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_comfort_data(user_input))
            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_complete()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _comfort_schema(), user_input or {}
            ),
            errors=errors,
            description_placeholders={
                "slot_minutes": str(self._get_entry().data[CONF_SLOT_MINUTES])
            },
        )

    async def async_step_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after creating a comfort load."""
        if self._pending_input is None:
            return await self.async_step_user()
        if user_input is not None:
            pending = self._pending_input
            self._pending_input = None
            return self.async_create_entry(
                title=_subentry_display_title(SUBENTRY_TYPE_COMFORT, pending),
                data=pending,
                unique_id=f"{SUBENTRY_TYPE_COMFORT}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure a comfort subentry."""
        errors: dict[str, str] = {}
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)

        if user_input is not None:
            if _subentry_name_in_use_excluding(
                self._get_entry(), user_input[CONF_NAME], subentry.subentry_id
            ):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_comfort_data(user_input))

            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_reconfigure_complete()
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_comfort_schema(), defaults),
            errors=errors,
            description_placeholders={
                "slot_minutes": str(self._get_entry().data[CONF_SLOT_MINUTES])
            },
        )

    async def async_step_reconfigure_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after editing a comfort load."""
        if self._pending_input is None:
            return await self.async_step_reconfigure()
        if user_input is not None:
            subentry = self._get_reconfigure_subentry()
            pending = self._pending_input
            self._pending_input = None
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                data=pending,
                title=_subentry_display_title(SUBENTRY_TYPE_COMFORT, pending),
                unique_id=f"{SUBENTRY_TYPE_COMFORT}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="reconfigure_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )


class OptionalSubentryFlowHandler(ConfigSubentryFlow):
    """Handle optional load subentry flow."""

    _pending_input: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create an optional load subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _subentry_name_in_use(self._get_entry(), user_input[CONF_NAME]):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_optional_data(user_input))
            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_complete()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _optional_schema(), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after creating an optional load."""
        if self._pending_input is None:
            return await self.async_step_user()
        if user_input is not None:
            pending = self._pending_input
            self._pending_input = None
            return self.async_create_entry(
                title=_subentry_display_title(SUBENTRY_TYPE_OPTIONAL, pending),
                data=pending,
                unique_id=f"{SUBENTRY_TYPE_OPTIONAL}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure an optional subentry."""
        errors: dict[str, str] = {}
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)

        if user_input is not None:
            if _subentry_name_in_use_excluding(
                self._get_entry(), user_input[CONF_NAME], subentry.subentry_id
            ):
                errors["base"] = "name_not_unique"
            else:
                errors.update(_validate_optional_data(user_input))

            if not errors:
                self._pending_input = dict(user_input)
                return await self.async_step_reconfigure_complete()
            defaults = user_input

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_optional_schema(), defaults),
            errors=errors,
        )

    async def async_step_reconfigure_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after editing an optional load."""
        if self._pending_input is None:
            return await self.async_step_reconfigure()
        if user_input is not None:
            subentry = self._get_reconfigure_subentry()
            pending = self._pending_input
            self._pending_input = None
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                data=pending,
                title=_subentry_display_title(SUBENTRY_TYPE_OPTIONAL, pending),
                unique_id=f"{SUBENTRY_TYPE_OPTIONAL}:{_normalize_name(pending[CONF_NAME])}",
            )
        return self.async_show_form(
            step_id="reconfigure_complete",
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )
