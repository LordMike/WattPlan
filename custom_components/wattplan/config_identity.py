"""Stable identity helpers for validating persisted coordinator state."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def config_fingerprint(entry: Any, *, options: dict[str, Any] | None = None) -> str:
    """Return a stable hash of everything the plan is computed from."""
    payload = {
        "data": dict(entry.data),
        "options": dict(entry.options if options is None else options),
        "subentries": {
            subentry_id: [subentry.subentry_type, dict(subentry.data)]
            for subentry_id, subentry in entry.subentries.items()
        },
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
