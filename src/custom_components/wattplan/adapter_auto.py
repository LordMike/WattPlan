"""Auto-detect helpers for adapter-backed forecast payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from numbers import Number
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class AdapterAutoDetectResult:
    """Resolved mapping inferred from one payload list."""

    root_key: str
    time_key: str
    value_key: str


def resolve_nested_value(root: Any, path: str) -> Any:
    """Resolve a dotted path from a payload root, allowing an empty path."""
    if not path:
        return root

    value = root
    for segment in path.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def iter_candidate_lists(root: Any, prefix: str = "") -> list[tuple[str, list[Any]]]:
    """Return dotted paths for every list found inside a root object."""
    candidates: list[tuple[str, list[Any]]] = []
    if isinstance(root, list):
        candidates.append((prefix, root))
        return candidates

    if not isinstance(root, dict):
        return candidates

    for key, value in root.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, list):
            candidates.append((path, value))
            continue
        if isinstance(value, dict):
            candidates.extend(iter_candidate_lists(value, path))

    return candidates


def _coerce_timestamp(value: Any) -> datetime | None:
    """Return parsed datetime when the value looks like an ISO timestamp."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _coerce_decimal(value: Any) -> float | None:
    """Return parsed float for numeric values, excluding booleans."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Number):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _select_numeric_value_key(row_values: set[str]) -> str | None:
    """Return one numeric key when the row has one clear primary value.

    Example:
    - `pv_estimate`
    - `pv_estimate10`
    - `pv_estimate90`

    Solcast-style payloads expose one central estimate plus percentile bands.
    We still want auto-detect to pick `pv_estimate`, but only when the other
    numeric keys are obvious suffix variants of that same base field.
    """
    if len(row_values) == 1:
        return next(iter(row_values))

    for candidate in row_values:
        siblings = row_values - {candidate}
        if siblings and all(
            re.fullmatch(rf"{re.escape(candidate)}[_0-9A-Za-z]+", sibling)
            for sibling in siblings
        ):
            return candidate

    return None


def detect_object_list_mapping(payload: list[Any]) -> tuple[str, str] | None:
    """Return detected time/value keys for a list of objects.

    The detector is intentionally shape-based rather than name-based so one
    implementation can support many providers. We only accept rows that reduce
    cleanly to one numeric value plus one or two timestamps.
    """
    matching_rows = 0
    timestamp_keys: set[str] | None = None
    value_keys: set[str] | None = None
    time_order_votes: dict[tuple[str, str], int] = {}

    for item in payload:
        if not isinstance(item, dict):
            continue

        parsed_timestamps = {
            key: parsed
            for key, value in item.items()
            if (parsed := _coerce_timestamp(value)) is not None
        }
        row_timestamps = set(parsed_timestamps)
        raw_value_keys = {
            key for key, value in item.items() if _coerce_decimal(value) is not None
        }
        value_key = _select_numeric_value_key(raw_value_keys)

        if len(row_timestamps) not in {1, 2} or value_key is None:
            continue

        # When a row carries both start and end timestamps, we keep a vote for
        # the earlier key so mixed payloads still settle on one start field.
        if len(parsed_timestamps) == 2:
            earlier_key, later_key = sorted(
                parsed_timestamps.items(),
                key=lambda item: item[1],
            )
            pair = (earlier_key[0], later_key[0])
            time_order_votes[pair] = time_order_votes.get(pair, 0) + 1

        matching_rows += 1
        timestamp_keys = (
            row_timestamps if timestamp_keys is None else timestamp_keys & row_timestamps
        )
        value_key_set = {value_key}
        value_keys = value_key_set if value_keys is None else value_keys & value_key_set

    if matching_rows == 0 or not timestamp_keys or len(value_keys or ()) != 1:
        return None

    if len(timestamp_keys) > 2:
        return None

    if len(timestamp_keys) == 2 and time_order_votes:
        start_key = max(time_order_votes.items(), key=lambda item: item[1])[0][0]
    else:
        start_key = next(iter(timestamp_keys))

    return (start_key, next(iter(value_keys)))


def auto_detect_mapping(root: Any) -> AdapterAutoDetectResult | None:
    """Walk a payload recursively and return the strongest compatible list.

    We score by the number of rows matched so the detector prefers the list that
    gives the planner the most usable data when multiple candidates are present.
    """
    best_match: tuple[int, AdapterAutoDetectResult] | None = None

    for path, payload in iter_candidate_lists(root):
        if not payload:
            continue
        match = detect_object_list_mapping(payload)
        if match is None:
            continue
        time_key, value_key = match
        score = len(payload)
        detected = AdapterAutoDetectResult(
            root_key=path,
            time_key=time_key,
            value_key=value_key,
        )
        if best_match is None or score > best_match[0]:
            best_match = (score, detected)

    return None if best_match is None else best_match[1]
