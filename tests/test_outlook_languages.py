"""Plan Outlook language resolution tests."""

from custom_components.wattplan.outlook_languages import resolve_outlook_languages


def test_system_language_is_used_without_additional_languages() -> None:
    assert resolve_outlook_languages([], "en") == ("en",)


def test_unsupported_system_language_falls_back_to_english() -> None:
    assert resolve_outlook_languages([], "fr") == ("en",)


def test_language_resolution_normalizes_deduplicates_and_intersects() -> None:
    assert resolve_outlook_languages(
        ["EN", "fr", "da-DK"],
        "en-US",
        supported_languages=("da", "en"),
    ) == ("da", "en")


def test_future_supported_system_language_is_always_included() -> None:
    assert resolve_outlook_languages(
        ["en"],
        "da_DK",
        supported_languages=("da", "en"),
    ) == ("da", "en")
