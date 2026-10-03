"""Consistency checks for the config/options/subentry flow strings."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import section

from custom_components.wattplan.flows import source_shared
from custom_components.wattplan.flows.main import (
    WattPlanConfigFlow,
    WattPlanOptionsFlow,
)
from custom_components.wattplan.flows.source_shared import (
    ENERGY_SOURCE_KEYS,
    SOURCE_STEP_REGISTRY,
)
from custom_components.wattplan.flows.subentries import (
    BatterySubentryFlowHandler,
    ComfortSubentryFlowHandler,
    OptionalSubentryFlowHandler,
)

INTEGRATION_DIR = Path(__file__).parents[2] / "custom_components" / "wattplan"
FLOWS_DIR = INTEGRATION_DIR / "flows"
STRINGS_PATH = INTEGRATION_DIR / "strings.json"
TRANSLATIONS_DIR = INTEGRATION_DIR / "translations"

# flow name -> (flow class, path of the flow's block inside the strings file)
FLOWS: dict[str, tuple[type, tuple[str, ...]]] = {
    "config": (WattPlanConfigFlow, ("config",)),
    "options": (WattPlanOptionsFlow, ("options",)),
    "battery": (BatterySubentryFlowHandler, ("config_subentries", "battery")),
    "comfort": (ComfortSubentryFlowHandler, ("config_subentries", "comfort")),
    "optional": (OptionalSubentryFlowHandler, ("config_subentries", "optional")),
}

# Steps that only redirect to another step and never render a form.
REDIRECT_STEPS = {("config", "user")}

# Where each flow can produce error keys: (module, top-level names or None for
# the whole module). Source acquisition and core validation live in
# source_shared and are shared by the config and options flows.
ERROR_SCOPES: dict[str, list[tuple[str, list[str] | None]]] = {
    "config": [("source_shared", None), ("main", ["WattPlanConfigFlow"])],
    "options": [
        ("source_shared", None),
        ("main", ["WattPlanOptionsFlow"]),
        ("forms", ["_validate_core_lookahead_for_comforts"]),
    ],
    "battery": [
        ("source_shared", ["_validate_text_field"]),
        ("forms", ["_validate_battery_data"]),
        ("subentries", ["BatterySubentryFlowHandler"]),
    ],
    "comfort": [
        ("source_shared", ["_validate_text_field"]),
        ("forms", ["_validate_comfort_data"]),
        ("subentries", ["ComfortSubentryFlowHandler"]),
    ],
    "optional": [
        ("source_shared", ["_validate_text_field"]),
        ("forms", ["_validate_optional_data"]),
        ("subentries", ["OptionalSubentryFlowHandler"]),
    ],
}


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load(path: Path) -> dict[str, Any]:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates
    )


def _block(strings: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    for part in path:
        strings = strings[part]
    return strings


def _structure(value: Any, prefix: str = "") -> set[str]:
    """Return the set of key paths of a nested dict."""
    if not isinstance(value, dict):
        return set()
    paths: set[str] = set()
    for key, child in value.items():
        paths.add(f"{prefix}/{key}")
        paths |= _structure(child, f"{prefix}/{key}")
    return paths


def _referenced_keys(
    scopes: list[tuple[str, list[str] | None]],
) -> tuple[set[str], set[str]]:
    """Return (error keys, abort reasons) written in the given flow code."""
    errors: set[str] = set()
    reasons: set[str] = set()
    for module, names in scopes:
        tree = ast.parse((FLOWS_DIR / f"{module}.py").read_text(encoding="utf-8"))
        roots = [
            node
            for node in tree.body
            if names is None
            or (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and node.name in names
            )
        ]
        for root in roots:
            for node in ast.walk(root):
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "errors"
                        for target in node.targets
                    )
                ):
                    errors |= {
                        const.value
                        for const in ast.walk(node.value)
                        if isinstance(const, ast.Constant)
                        and isinstance(const.value, str)
                    }
                elif isinstance(node, ast.keyword) and node.arg == "reason":
                    if isinstance(node.value, ast.Constant):
                        reasons.add(node.value.value)
                elif (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Attribute)
                    and node.exc.func.attr == "Invalid"
                    and node.exc.args
                    and isinstance(node.exc.args[0], ast.Constant)
                ):
                    errors.add(node.exc.args[0].value)
                elif (
                    isinstance(root, ast.FunctionDef | ast.AsyncFunctionDef)
                    and root.name == "_invalid_key_from_source_error"
                    and isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Constant)
                ):
                    errors.add(node.value.value)
    return errors, reasons


def _flow_steps(flow_class: type) -> set[str]:
    """Return the step ids implemented by the integration's own flow code."""
    steps = set()
    for name in dir(flow_class):
        if not name.startswith("async_step_"):
            continue
        func = getattr(flow_class, name)
        if getattr(func, "__module__", "").startswith("custom_components.wattplan"):
            steps.add(name.removeprefix("async_step_"))
    return steps


def test_strings_and_translations_have_no_duplicate_keys() -> None:
    """Duplicate JSON keys silently drop the earlier definition."""
    for path in [STRINGS_PATH, *TRANSLATIONS_DIR.glob("*.json")]:
        _load(path)


def test_strings_json_matches_english_translation() -> None:
    """strings.json is the source; translations/en.json is its copy."""
    assert _load(STRINGS_PATH) == _load(TRANSLATIONS_DIR / "en.json")


@pytest.mark.parametrize(
    "path",
    sorted(p for p in TRANSLATIONS_DIR.glob("*.json") if p.name != "en.json"),
    ids=lambda p: p.name,
)
def test_other_translations_have_same_key_structure(path: Path) -> None:
    """Every translation file must define exactly the English key structure."""
    assert _structure(_load(path)) == _structure(_load(STRINGS_PATH))


@pytest.mark.parametrize("flow_name", list(FLOWS))
def test_flow_steps_are_defined_in_strings(flow_name: str) -> None:
    """Every step implemented in code needs a title and (for menus) options."""
    flow_class, path = FLOWS[flow_name]
    block = _block(_load(STRINGS_PATH), path)
    steps = _flow_steps(flow_class)
    assert steps, "no steps found"
    for step in sorted(steps):
        if (flow_name, step) in REDIRECT_STEPS:
            continue
        assert step in block["step"], f"{flow_name}: step {step} has no strings"
        assert block["step"][step].get("title"), f"{flow_name}: step {step} no title"


@pytest.mark.parametrize("flow_name", list(FLOWS))
def test_flow_errors_and_aborts_are_defined_in_strings(flow_name: str) -> None:
    """Every error key and abort reason written by the flow code is translated."""
    _, path = FLOWS[flow_name]
    block = _block(_load(STRINGS_PATH), path)
    errors, reasons = _referenced_keys(ERROR_SCOPES[flow_name])
    assert errors, "no error keys found"
    assert errors <= set(block.get("error", {})), (
        f"{flow_name}: missing error strings "
        f"{sorted(errors - set(block.get('error', {})))}"
    )
    assert reasons <= set(block.get("abort", {}))


@pytest.mark.parametrize("flow_name", ["config", "options"])
def test_menu_options_are_defined_steps(flow_name: str) -> None:
    """Menu entries must point at steps the flow implements."""
    flow_class, path = FLOWS[flow_name]
    block = _block(_load(STRINGS_PATH), path)
    steps = _flow_steps(flow_class)
    for step, definition in block["step"].items():
        for option in definition.get("menu_options", {}):
            assert option in steps, f"{flow_name}: {step} menu option {option}"


def _schema_fields(schema: vol.Schema) -> tuple[set[str], dict[str, set[str]]]:
    """Return top-level field names and section name -> field names."""
    fields: set[str] = set()
    sections: dict[str, set[str]] = {}
    for marker, value in schema.schema.items():
        name = str(marker.schema)
        if isinstance(value, section):
            sections[name] = {str(inner.schema) for inner in value.schema.schema}
        else:
            fields.add(name)
    return fields, sections


def _source_step_schemas() -> dict[str, vol.Schema]:
    """Build the real schema of every registered source input step."""
    builders = {
        "_template": source_shared._source_template_schema,
        "_adapter": source_shared._source_adapter_schema,
        "_service": source_shared._source_service_schema,
    }
    schemas: dict[str, vol.Schema] = {}
    for source_key, modes in SOURCE_STEP_REGISTRY.items():
        for step_id in modes.values():
            value_unit = source_key in ENERGY_SOURCE_KEYS
            for suffix, builder in builders.items():
                if step_id.endswith(suffix):
                    schemas[step_id] = builder(include_value_unit=value_unit)
            if step_id.endswith("_energy_provider"):
                schemas[step_id] = source_shared._source_energy_provider_schema(
                    None, provider_options=[]
                )
            if step_id.endswith("_built_in"):
                schemas[step_id] = source_shared._source_built_in_schema()
    return schemas


@pytest.mark.parametrize("flow_name", ["config", "options"])
def test_source_step_fields_and_sections_have_labels(flow_name: str) -> None:
    """Fields and section fields of each source step are labelled in strings."""
    _, path = FLOWS[flow_name]
    steps = _block(_load(STRINGS_PATH), path)["step"]
    schemas = _source_step_schemas()
    assert schemas
    for step_id, schema in schemas.items():
        assert step_id in steps, f"{flow_name}: {step_id} missing"
        fields, sections = _schema_fields(schema)
        definition = steps[step_id]
        assert set(definition["data"]) == fields, f"{flow_name}: {step_id} data"
        assert set(definition.get("sections", {})) == set(sections), (
            f"{flow_name}: {step_id} sections"
        )
        for name, inner in sections.items():
            labelled = definition["sections"][name]
            assert labelled.get("name"), f"{flow_name}: {step_id}.{name} no name"
            assert set(labelled["data"]) == inner, (
                f"{flow_name}: {step_id}.{name} fields"
            )


@pytest.mark.parametrize(
    ("flow_name", "builder"),
    [
        ("battery", source_shared._battery_schema),
        ("comfort", source_shared._comfort_schema),
        ("optional", source_shared._optional_schema),
    ],
)
def test_subentry_form_fields_have_labels(flow_name: str, builder) -> None:
    """Subentry forms (including battery efficiencies section) are labelled."""
    _, path = FLOWS[flow_name]
    steps = _block(_load(STRINGS_PATH), path)["step"]
    fields, sections = _schema_fields(builder())
    for step_id in ("user", "reconfigure"):
        definition = steps[step_id]
        assert fields <= set(definition["data"]), f"{flow_name}.{step_id}"
        assert set(definition.get("sections", {})) == set(sections)
        for name, inner in sections.items():
            assert set(definition["sections"][name]["data"]) == inner
