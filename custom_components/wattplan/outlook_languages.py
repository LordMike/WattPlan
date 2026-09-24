"""Plan Outlook language selection helpers."""

from __future__ import annotations

from collections.abc import Iterable

from .const import SUPPORTED_OUTLOOK_LANGUAGES


def _base_language(language: object) -> str:
    """Normalize one language or locale to its lowercase base code."""
    return str(language or "").strip().lower().replace("_", "-").split("-", 1)[0]


def resolve_outlook_languages(
    additional_languages: Iterable[str] | None,
    system_language: str | None,
    *,
    supported_languages: tuple[str, ...] = SUPPORTED_OUTLOOK_LANGUAGES,
) -> tuple[str, ...]:
    """Return supported system and additional languages in stable order."""
    requested = {_base_language(system_language)}
    requested.update(_base_language(language) for language in additional_languages or ())
    resolved = tuple(
        language for language in supported_languages if language in requested
    )
    return resolved or ("en",)
