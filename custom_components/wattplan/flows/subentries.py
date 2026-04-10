"""Subentry flow handlers for WattPlan."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigSubentryFlow, SubentryFlowResult
from homeassistant.const import CONF_NAME

from .common import _normalize_name, _subentry_display_title
from .forms import (
    _battery_form_defaults,
    _normalize_battery_input,
    _subentry_name_in_use,
    _subentry_name_in_use_excluding,
    _validate_battery_data,
    _validate_comfort_data,
    _validate_optional_data,
)
from ..const import (
    CONF_SLOT_MINUTES,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from .source_shared import (
    _battery_schema,
    _comfort_schema,
    _final_setup_schema,
    _optional_schema,
)


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


__all__ = [
    "BatterySubentryFlowHandler",
    "ComfortSubentryFlowHandler",
    "OptionalSubentryFlowHandler",
]
