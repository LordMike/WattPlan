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
    supported = set(supported_languages)
    resolved: list[str] = []
    for language in (
        _base_language(system_language),
        *(_base_language(language) for language in additional_languages or ()),
    ):
        if language in supported and language not in resolved:
            resolved.append(language)
    return tuple(resolved) or ("en",)
