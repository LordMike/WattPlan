"""Subentry flow handlers for WattPlan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
import voluptuous as vol

from ..const import (
    CONF_ROLLING_WINDOW_HOURS,
    CONF_SLOT_MINUTES,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from .common import _normalize_name, _subentry_display_title
from .forms import (
    _battery_form_defaults,
    _normalize_battery_input,
    _subentry_name_error,
    _validate_battery_data,
    _validate_comfort_data,
    _validate_optional_data,
)
from .source_shared import (
    _battery_schema,
    _comfort_schema,
    _final_setup_schema,
    _optional_schema,
)


def _identity(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the form data."""
    return dict(data)


@dataclass(frozen=True)
class _SubentrySpec:
    """What differs between the battery, comfort and optional subentry flows."""

    subentry_type: str
    schema: Callable[[], vol.Schema]
    # Turns raw form input into the data stored on the subentry.
    normalize: Callable[[dict[str, Any]], dict[str, Any]]
    # Shapes stored data or raw form input as suggested form values.
    form_defaults: Callable[[dict[str, Any]], dict[str, Any]]
    # Returns field errors for normalised data.
    validate: Callable[[dict[str, Any], ConfigEntry, str | None], dict[str, str]]
    placeholders: Callable[[ConfigEntry], dict[str, str] | None]


def _no_placeholders(entry: ConfigEntry) -> dict[str, str] | None:
    return None


def _comfort_placeholders(entry: ConfigEntry) -> dict[str, str] | None:
    # Comfort loads must share one window, so any existing load gives the value
    # shown by the window-mismatch error.
    window_hours = next(
        (
            f"{float(subentry.data[CONF_ROLLING_WINDOW_HOURS]):g}"
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_COMFORT
        ),
        "",
    )
    return {
        "slot_minutes": str(entry.data[CONF_SLOT_MINUTES]),
        "window_hours": window_hours,
    }


_BATTERY_SPEC = _SubentrySpec(
    SUBENTRY_TYPE_BATTERY,
    _battery_schema,
    _normalize_battery_input,
    _battery_form_defaults,
    lambda data, entry, exclude_id: _validate_battery_data(data),
    _no_placeholders,
)
_COMFORT_SPEC = _SubentrySpec(
    SUBENTRY_TYPE_COMFORT,
    _comfort_schema,
    _identity,
    _identity,
    lambda data, entry, exclude_id: _validate_comfort_data(
        data, entry=entry, exclude_subentry_id=exclude_id
    ),
    _comfort_placeholders,
)
_OPTIONAL_SPEC = _SubentrySpec(
    SUBENTRY_TYPE_OPTIONAL,
    _optional_schema,
    _identity,
    _identity,
    lambda data, entry, exclude_id: _validate_optional_data(data),
    _no_placeholders,
)


class _SubentryFlowHandler(ConfigSubentryFlow):
    """Create and reconfigure one subentry type, each followed by a summary step."""

    _spec: ClassVar[_SubentrySpec]
    _pending_input: dict[str, Any] | None = None

    def _validate(
        self, normalized: dict[str, Any], exclude_subentry_id: str | None = None
    ) -> dict[str, str]:
        """Return form errors for normalised input."""
        entry = self._get_entry()
        if error := _subentry_name_error(
            entry, normalized[CONF_NAME], exclude_subentry_id
        ):
            return {"base": error}
        return self._spec.validate(normalized, entry, exclude_subentry_id)

    def _show_form(
        self, step_id: str, defaults: dict[str, Any], errors: dict[str, str]
    ) -> SubentryFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                self._spec.schema(), self._spec.form_defaults(defaults)
            ),
            errors=errors,
            description_placeholders=self._spec.placeholders(self._get_entry()),
        )

    def _show_summary(self, step_id: str) -> SubentryFlowResult:
        assert self._pending_input is not None
        return self.async_show_form(
            step_id=step_id,
            data_schema=_final_setup_schema(),
            description_placeholders={"name": str(self._pending_input[CONF_NAME])},
            last_step=True,
        )

    def _unique_id(self, data: dict[str, Any]) -> str:
        return f"{self._spec.subentry_type}:{_normalize_name(data[CONF_NAME])}"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Create a subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized = self._spec.normalize(user_input)
            errors = self._validate(normalized)
            if not errors:
                self._pending_input = normalized
                return await self.async_step_complete()
        return self._show_form("user", user_input or {}, errors)

    async def async_step_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after creating a subentry."""
        if self._pending_input is None:
            return await self.async_step_user()
        if user_input is not None:
            pending = self._pending_input
            self._pending_input = None
            return self.async_create_entry(
                title=_subentry_display_title(self._spec.subentry_type, pending),
                data=pending,
                unique_id=self._unique_id(pending),
            )
        return self._show_summary("complete")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Reconfigure a subentry."""
        errors: dict[str, str] = {}
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)

        if user_input is not None:
            normalized = self._spec.normalize(user_input)
            errors = self._validate(normalized, subentry.subentry_id)
            if not errors:
                self._pending_input = normalized
                return await self.async_step_reconfigure_complete()
            defaults = user_input

        return self._show_form("reconfigure", defaults, errors)

    async def async_step_reconfigure_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show next actions after editing a subentry."""
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
                title=_subentry_display_title(self._spec.subentry_type, pending),
                unique_id=self._unique_id(pending),
            )
        return self._show_summary("reconfigure_complete")


class BatterySubentryFlowHandler(_SubentryFlowHandler):
    """Handle battery subentry flow."""

    _spec = _BATTERY_SPEC


class ComfortSubentryFlowHandler(_SubentryFlowHandler):
    """Handle comfort subentry flow."""

    _spec = _COMFORT_SPEC


class OptionalSubentryFlowHandler(_SubentryFlowHandler):
    """Handle optional load subentry flow."""

    _spec = _OPTIONAL_SPEC


__all__ = [
    "BatterySubentryFlowHandler",
    "ComfortSubentryFlowHandler",
    "OptionalSubentryFlowHandler",
]
