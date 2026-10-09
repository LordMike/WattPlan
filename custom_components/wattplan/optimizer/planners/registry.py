"""Lazy factories and implementation-owned opaque state for simulator calls."""

import base64
import importlib
import inspect
import json
from typing import Callable

from ..models import OptimizationParams, encode_state_blob
from .contract import PlannerCapabilities

BUILTINS = {
    "highs": "custom_components.wattplan.optimizer.planners.highs:factory",
    "self-consumption": "custom_components.wattplan.optimizer.planners.self_consumption:factory",
    "greedy": "custom_components.wattplan.optimizer.planners.greedy:factory",
    "local-search": "custom_components.wattplan.optimizer.planners.local_search:factory",
    "beam-search": "custom_components.wattplan.optimizer.planners.beam_search:factory",
    "dynamic-programming": "custom_components.wattplan.optimizer.planners.dynamic_programming:factory",
    "charge-window": "custom_components.wattplan.optimizer.planners.charge_window:factory",
    "genetic": "custom_components.wattplan.optimizer.planners.genetic:factory",
    "cross-entropy": "custom_components.wattplan.optimizer.planners.cross_entropy:factory",
}
ALIASES = {
    "custom_components.wattplan.optimizer.simulation.adapters:highs_factory": "highs",
    "custom_components.wattplan.optimizer.simulation.adapters:self_consumption_factory": "self-consumption",
}


def resolve_factory(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Planner must be a built-in name or module:factory")
    name = BUILTINS.get(ALIASES.get(name, name), name)
    if name.count(":") != 1 or not all(part.strip() for part in name.split(":")):
        raise ValueError("Planner must be a built-in name or module:factory")
    return name


def _decode(blob: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(blob.encode("ascii")))


def create_planner(name: str, options: dict | None = None) -> Callable:
    """Create an isolated callable; custom modules need no registry edits.

    Factories may expose ``state_version`` (positive int, default 1). Namespace
    covers factory path and options; identity is not proof a cached schedule is
    valid. Implementations must re-evaluate candidates with each current request.
    """
    path = resolve_factory(name)
    module, attribute = path.split(":")
    factory = getattr(importlib.import_module(module), attribute)
    options = {} if options is None else options
    if not isinstance(options, dict):
        raise ValueError("Planner options must be an object")
    inspect.signature(factory).bind(**options)
    option_key = json.dumps(
        options, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    implementation = factory(**options)
    if not callable(implementation):
        raise ValueError("Planner factory must return a callable")
    capabilities = getattr(implementation, "capabilities", PlannerCapabilities())
    if not isinstance(capabilities, PlannerCapabilities):
        raise ValueError("Planner capabilities must use PlannerCapabilities")
    version = getattr(implementation, "state_version", 1)
    if type(version) is not int or version < 1:
        raise ValueError("Planner state_version must be a positive integer")

    def plan(params: OptimizationParams) -> dict:
        notes = capabilities.validate(params)
        state, reset_reason = params.state, None
        if state is not None:
            obj = _decode(state)
            if "planner_state" in obj:
                envelope = obj["planner_state"]
                if isinstance(envelope, dict) and (
                    envelope.get("factory"),
                    envelope.get("version"),
                    envelope.get("options"),
                ) == (path, version, option_key):
                    state = envelope.get("payload")
                else:
                    state, reset_reason = (
                        None,
                        "different planner, state version, or options",
                    )
            elif path != BUILTINS["highs"]:
                state, reset_reason = None, "unowned legacy state"
        OptimizationParams._validate_state_blob(state)
        local = params.model_copy(update={"state": state})
        result = implementation(local)
        if not isinstance(result, dict):
            raise ValueError("Planner must return an object")
        result = dict(result)
        if result.get("state") is not None:
            # Validate the implementation's own opaque state before wrapping it.
            OptimizationParams._validate_state_blob(result["state"])
            result["state"] = encode_state_blob(
                {
                    "v": 1,
                    "planner_state": {
                        "factory": path,
                        "version": version,
                        "options": option_key,
                        "payload": result["state"],
                    },
                }
            )
        result["diagnostics"] = {
            **(result.get("diagnostics") or {}),
            "planner": {
                "factory": path,
                "state_version": version,
                "notes": notes,
                "state_reset_reason": reset_reason,
            },
        }
        return result

    plan.capabilities = capabilities
    return plan
