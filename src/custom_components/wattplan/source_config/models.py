"""Typed internal models for source configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceProviderModel:
    """One provider definition inside a source config."""

    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SourceConfigModel:
    """Canonical internal view of one persisted source config."""

    data: dict[str, Any]
    providers: tuple[SourceProviderModel, ...]
    mode: str
