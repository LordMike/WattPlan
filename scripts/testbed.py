#!/usr/bin/env python3
"""Manage a disposable Home Assistant instance for WattPlan testbed work."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
from urllib import error, parse, request

REPO_ROOT = Path(__file__).resolve().parents[1]
HASS_CORE = (REPO_ROOT.parent / "hass-core").resolve()
DEFAULT_CONFIG = Path(os.environ.get("WATTPLAN_TESTBED_CONFIG", "/tmp/wattplan-ha-testbed/default"))
DEFAULT_HASS = Path(os.environ.get("WATTPLAN_HASS", HASS_CORE / ".venv/bin/hass"))
DEFAULT_PORT = int(os.environ.get("WATTPLAN_TESTBED_PORT", "8124"))
DEFAULT_USER = os.environ.get("WATTPLAN_TESTBED_USER", "wattplan")
DEFAULT_PASSWORD = os.environ.get("WATTPLAN_TESTBED_PASSWORD", "wattplan-testbed")
CLIENT_ID = "http://localhost/"

WATTPLAN_TITLE = "WattPlan Testbed Plan"
TESTBED_TITLE = "WattPlan Testbed"

TESTBED_ENTITY_PREFIX = "wattplan_testbed"
IMPORT_PRICE_NAME = "Demo Import Price"
EXPORT_PRICE_NAME = "Demo Export Price"
PV_NAME = "Demo PV"
LIGHT_LOAD_NAME = "Light Load"
HEAVY_LOAD_NAME = "Heavy Load"
HOME_BATTERY_NAME = "Home Battery"
EV_BATTERY_NAME = "EV Battery"

CONF_ACCEPT_SOURCE_SUMMARY = "accept_source_summary"
CONF_ADAPTER_TYPE = "adapter_type"
CONF_AVAILABILITY_SOURCE = "availability_source"
CONF_CAN_CHARGE_FROM_GRID = "can_charge_from_grid"
CONF_CAN_CHARGE_FROM_PV = "can_charge_from_pv"
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_CHARGE_EFFICIENCY = "charge_efficiency"
CONF_CONFIG_ENTRY_ID = "config_entry_id"
CONF_DISCHARGE_EFFICIENCY = "discharge_efficiency"
CONF_FIXUP_PROFILE = "fixup_profile"
CONF_HISTORY_DAYS = "history_days"
CONF_HOURS_TO_PLAN = "hours_to_plan"
CONF_MAX_CHARGE_KW = "max_charge_kw"
CONF_MAX_DISCHARGE_KW = "max_discharge_kw"
CONF_MINIMUM_KWH = "minimum_kwh"
CONF_NAME = "name"
CONF_SLOT_MINUTES = "slot_minutes"
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"
CONF_SOC_SOURCE = "soc_source"
CONF_SOURCE_MODE = "source_mode"
CONF_WATTPLAN_ENTITY_ID = "entity_id"
SECTION_BATTERY_ADVANCED = "advanced"
SECTION_SOURCE_MANUAL = "manual"

ADAPTER_TYPE_ATTRIBUTE_OBJECTS = "attribute_objects"
FIXUP_PROFILE_EXTEND = "extend_daily_pattern"
SOURCE_MODE_BUILT_IN = "built_in"
SOURCE_MODE_ENTITY_ADAPTER = "entity_adapter"
SOURCE_MODE_ENERGY_PROVIDER = "energy_provider"


class TestbedHttpError(RuntimeError):
    """HTTP error with response details for API calls."""

    def __init__(self, method: str, path: str, code: int, body: str) -> None:
        """Initialize an HTTP error."""
        self.code = code
        self.body = body
        super().__init__(f"{method} {path} failed: HTTP {code}: {body}")


class OnboardingRestartRequired(RuntimeError):
    """Raised when HA needs a restart to finish source-checkout onboarding."""


def _slug(value: str) -> str:
    """Return the same simple slug shape HA uses for generated entity IDs."""
    slug = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower().replace("-", "_"))
    return re.sub(r"_+", "_", slug).strip("_") or "asset"


def _asset_entity(name: str, suffix: str, *, domain: str = "sensor") -> str:
    """Return the expected entity ID for one generated testbed entity."""
    return f"{domain}.{TESTBED_ENTITY_PREFIX}_{_slug(name)}_{suffix}"


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text("utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), "utf-8")


def _request(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_data: dict[str, Any] | None = None,
    form_data: dict[str, Any] | None = None,
) -> Any:
    headers: dict[str, str] = {}
    data: bytes | None = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_data).encode("utf-8")
    if form_data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = parse.urlencode(form_data).encode("utf-8")
    req = request.Request(_url(port, path), data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as response:
            body = response.read()
    except error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise TestbedHttpError(method, path, err.code, body) from err
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def _wait_for_ha(port: int, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _request(port, "GET", "/api/onboarding")
            return
        except TestbedHttpError as err:
            if err.code in {401, 403, 404}:
                return
            time.sleep(1)
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"Home Assistant did not respond on port {port}")


def _pid_path(config_dir: Path) -> Path:
    return config_dir / ".testbed" / "ha.pid"


def _token_path(config_dir: Path) -> Path:
    return config_dir / ".testbed" / "auth.json"


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _hass_command(hass_bin: Path) -> list[str]:
    """Return the HA command, falling back to source checkout module execution."""
    if hass_bin.exists():
        if hass_bin.name.startswith("python"):
            return [str(hass_bin), "-m", "homeassistant"]
        return [str(hass_bin)]
    python_bin = hass_bin.with_name("python")
    if python_bin.exists():
        return [str(python_bin), "-m", "homeassistant"]
    raise FileNotFoundError(
        f"Home Assistant executable not found at {hass_bin} or {python_bin}"
    )


def _hass_env() -> dict[str, str]:
    """Return an environment that can run HA from a sibling source checkout."""
    env = dict(os.environ)
    pythonpath = str(HASS_CORE)
    if existing := env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{existing}"
    env["PYTHONPATH"] = pythonpath
    return env


def ensure_config(config_dir: Path, port: int) -> None:
    """Create config files and custom component links."""
    custom_components = config_dir / "custom_components"
    custom_components.mkdir(parents=True, exist_ok=True)

    links = {
        "wattplan": REPO_ROOT / "custom_components/wattplan",
        "wattplan_testbed": REPO_ROOT / "testbed/custom_components/wattplan_testbed",
    }
    for name, target in links.items():
        link = custom_components / name
        if link.exists() or link.is_symlink():
            continue
        link.symlink_to(target, target_is_directory=True)

    config_yaml = config_dir / "configuration.yaml"
    if not config_yaml.exists():
        config_yaml.write_text(
            "\n".join(
                [
                    "config:",
                    "energy:",
                    "frontend:",
                    "onboarding:",
                    "",
                    "http:",
                    f"  server_port: {port}",
                    "  server_host: 127.0.0.1",
                    "",
                    "recorder:",
                    "  db_url: sqlite:///home-assistant_v2.db",
                    "",
                ]
            ),
            "utf-8",
        )


def start_ha(config_dir: Path, hass_bin: Path, port: int) -> None:
    """Start HA in the background."""
    ensure_config(config_dir, port)
    pid_file = _pid_path(config_dir)
    if pid_file.exists():
        pid = int(pid_file.read_text("utf-8"))
        if _is_running(pid):
            print(f"Home Assistant already running: pid={pid}")
            _wait_for_ha(port)
            return
    log_path = config_dir / ".testbed" / "ha.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    process = subprocess.Popen(
        [*_hass_command(hass_bin), "-c", str(config_dir)],
        cwd=str(config_dir),
        env=_hass_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file.write_text(str(process.pid), "utf-8")
    print(f"Started Home Assistant: pid={process.pid}, log={log_path}")
    _wait_for_ha(port)


def stop_ha(config_dir: Path) -> None:
    """Stop HA if this script started it."""
    pid_file = _pid_path(config_dir)
    if not pid_file.exists():
        print("No testbed pid file found")
        return
    pid = int(pid_file.read_text("utf-8"))
    if not _is_running(pid):
        pid_file.unlink(missing_ok=True)
        print("Home Assistant was not running")
        return
    os.killpg(pid, signal.SIGTERM)
    for _ in range(60):
        if not _is_running(pid):
            pid_file.unlink(missing_ok=True)
            print("Stopped Home Assistant")
            return
        time.sleep(1)
    raise RuntimeError(f"Home Assistant did not stop cleanly: pid={pid}")


def _exchange_auth_code(port: int, auth_code: str) -> dict[str, Any]:
    return _request(
        port,
        "POST",
        "/auth/token",
        form_data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
        },
    )


def _login(port: int, username: str, password: str) -> dict[str, Any]:
    providers = _request(port, "GET", "/auth/providers")
    provider = providers["providers"][0]
    flow = _request(
        port,
        "POST",
        "/auth/login_flow",
        json_data={
            "client_id": CLIENT_ID,
            "handler": [provider["type"], provider.get("id")],
            "redirect_uri": CLIENT_ID,
        },
    )
    result = _request(
        port,
        "POST",
        f"/auth/login_flow/{flow['flow_id']}",
        json_data={
            "client_id": CLIENT_ID,
            "username": username,
            "password": password,
        },
    )
    return _exchange_auth_code(port, result["result"])


def ensure_auth(config_dir: Path, port: int) -> str:
    """Complete onboarding if needed and return an access token."""
    auth_path = _token_path(config_dir)
    stored = _read_json(auth_path)
    if token := stored.get("access_token"):
        try:
            _request(port, "GET", "/api/config", token=str(token))
            return str(token)
        except Exception:
            pass

    try:
        onboarding = _request(port, "GET", "/api/onboarding")
    except TestbedHttpError as err:
        if err.code != 404:
            raise
        tokens = _login(port, DEFAULT_USER, DEFAULT_PASSWORD)
        token = str(tokens["access_token"])
        _write_json(auth_path, tokens)
        return token

    done = {item["step"] for item in onboarding if item.get("done")}
    if "user" not in done:
        try:
            created = _request(
                port,
                "POST",
                "/api/onboarding/users",
                json_data={
                    "name": "WattPlan Testbed",
                    "username": DEFAULT_USER,
                    "password": DEFAULT_PASSWORD,
                    "client_id": CLIENT_ID,
                    "language": "en",
                },
            )
            tokens = _exchange_auth_code(port, created["auth_code"])
        except TestbedHttpError as err:
            if err.code != 500:
                raise
            # Source checkouts can fail default-area translation lookup after
            # creating the user but before marking onboarding complete.
            try:
                tokens = _login(port, DEFAULT_USER, DEFAULT_PASSWORD)
            except TestbedHttpError as login_err:
                if login_err.code == 400 and "onboarding_required" in login_err.body:
                    raise OnboardingRestartRequired(
                        "Restart HA so onboarding can detect the owner account"
                    ) from login_err
                raise
    else:
        tokens = _login(port, DEFAULT_USER, DEFAULT_PASSWORD)

    token = str(tokens["access_token"])
    _write_json(auth_path, tokens)

    for step, path, payload in [
        ("core_config", "/api/onboarding/core_config", {}),
        ("integration", "/api/onboarding/integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
        ("analytics", "/api/onboarding/analytics", {}),
    ]:
        onboarding = _request(port, "GET", "/api/onboarding")
        done = {item["step"] for item in onboarding if item.get("done")}
        if step in done:
            continue
        try:
            _request(port, "POST", path, token=token, json_data=payload)
        except TestbedHttpError as err:
            if err.code not in {403, 409}:
                raise

    return token


def _flow_post(port: int, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _request(port, "POST", path, token=token, json_data=payload)


def _configure_flow(
    port: int,
    token: str,
    start_path: str,
    start_payload: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    flow = _flow_post(port, token, start_path, start_payload)
    return _flow_post(port, token, f"{start_path}/{flow['flow_id']}", data)


def _continue_flow(
    port: int, token: str, path: str, flow: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    """Post data to an existing config or subentry flow."""
    return _flow_post(port, token, f"{path}/{flow['flow_id']}", data)


def _entries(port: int, token: str, domain: str) -> list[dict[str, Any]]:
    """Return config entries for one domain."""
    return list(_request(port, "GET", f"/api/config/config_entries/entry?domain={domain}", token=token))


def _entry(port: int, token: str, domain: str, entry_id: str) -> dict[str, Any]:
    """Return one config entry, refreshed from the REST API."""
    for item in _entries(port, token, domain):
        if item["entry_id"] == entry_id:
            return item
    raise RuntimeError(f"No {domain} config entry found with id {entry_id}")


def _subentries(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return subentries from an entry payload."""
    return list(entry.get("subentries") or [])


def _stored_entries(config_dir: Path, domain: str) -> list[dict[str, Any]]:
    """Return stored config entries for one domain, including subentry data."""
    path = config_dir / ".storage/core.config_entries"
    if not path.exists():
        return []
    payload = json.loads(path.read_text("utf-8"))
    return [
        entry
        for entry in payload.get("data", {}).get("entries", [])
        if entry.get("domain") == domain
    ]


def _stored_entry(
    config_dir: Path, domain: str, entry_id: str
) -> dict[str, Any] | None:
    """Return one stored config entry by id."""
    for entry in _stored_entries(config_dir, domain):
        if entry.get("entry_id") == entry_id:
            return entry
    return None


def ensure_testbed_entry(port: int, token: str) -> str:
    """Ensure a WattPlan testbed config entry exists."""
    entries = _entries(port, token, "wattplan_testbed")
    if entries:
        return str(entries[0]["entry_id"])
    result = _configure_flow(
        port,
        token,
        "/api/config/config_entries/flow",
        {"handler": "wattplan_testbed"},
        {CONF_NAME: TESTBED_TITLE, CONF_UPDATE_INTERVAL_MINUTES: 15},
    )
    return str(result["result"]["entry_id"])


def _has_subentry(entry: dict[str, Any], subentry_type: str, name: str) -> bool:
    """Return whether an entry already has a named subentry."""
    wanted = name.casefold()
    return any(
        subentry.get("subentry_type") == subentry_type
        and str(subentry.get("data", {}).get(CONF_NAME, "")).casefold() == wanted
        for subentry in _subentries(entry)
    )


def _add_subentry(
    port: int,
    token: str,
    entry_id: str,
    subentry_type: str,
    data: dict[str, Any],
) -> None:
    result = _configure_flow(
        port,
        token,
        "/api/config/config_entries/subentries/flow",
        {"handler": [entry_id, subentry_type]},
        data,
    )
    print(f"Created {subentry_type}: {result.get('title', data.get('name'))}")


def add_demo_assets(config_dir: Path, port: int, token: str, entry_id: str) -> None:
    """Create a representative set of testbed assets."""
    entry = _stored_entry(config_dir, "wattplan_testbed", entry_id) or {}
    created_any = False
    specs: list[tuple[str, dict[str, Any]]] = [
        (
            "price",
            {
                CONF_NAME: IMPORT_PRICE_NAME,
                "base_offset": 0.35,
                "factor": 1.0,
                "noise": 0.02,
                "phase_hours": 0.0,
                "seed": 1,
            },
        ),
        (
            "price",
            {
                CONF_NAME: EXPORT_PRICE_NAME,
                "base_offset": 0.10,
                "factor": 0.8,
                "noise": 0.015,
                "phase_hours": 0.0,
                "seed": 2,
            },
        ),
        (
            "pv",
            {
                CONF_NAME: PV_NAME,
                "peak_kwh": 1.6,
                "cloud_factor": 0.2,
                "factor": 1.0,
                "phase_hours": 0.0,
                "seed": 11,
            },
        ),
        (
            "load",
            {
                CONF_NAME: LIGHT_LOAD_NAME,
                "profile": "light",
                "factor": 1.0,
                "noise": 0.04,
                "phase_hours": 0.0,
                "initial_total_kwh": 1000.0,
                "seed": 21,
            },
        ),
        (
            "load",
            {
                CONF_NAME: HEAVY_LOAD_NAME,
                "profile": "heavy",
                "factor": 1.0,
                "noise": 0.04,
                "phase_hours": 0.0,
                "initial_total_kwh": 1000.0,
                "seed": 22,
            },
        ),
        (
            "battery",
            {
                CONF_NAME: HOME_BATTERY_NAME,
                "capacity_kwh": 10.0,
                "default_soc_pct": 55.0,
                "seed": 101,
            },
        ),
        (
            "battery",
            {
                CONF_NAME: EV_BATTERY_NAME,
                "capacity_kwh": 10.0,
                "default_soc_pct": 80.0,
                "seed": 102,
            },
        ),
    ]
    for subentry_type, data in specs:
        if _has_subentry(entry, subentry_type, str(data[CONF_NAME])):
            print(f"Already exists: {subentry_type} {data[CONF_NAME]}")
            continue
        _add_subentry(port, token, entry_id, subentry_type, data)
        created_any = True
    if created_any:
        _request(port, "POST", f"/api/config/config_entries/entry/{entry_id}/reload", token=token, json_data={})
        print("Reloaded testbed entry")


def _state(port: int, token: str, entity_id: str) -> dict[str, Any] | None:
    """Return one entity state if it exists."""
    try:
        return dict(_request(port, "GET", f"/api/states/{entity_id}", token=token))
    except TestbedHttpError as err:
        if err.code == 404:
            return None
        raise


def wait_for_entities(port: int, token: str, entity_ids: list[str], timeout: int = 60) -> None:
    """Wait until all named entities exist in HA state."""
    deadline = time.monotonic() + timeout
    missing = list(entity_ids)
    while time.monotonic() < deadline:
        missing = [entity_id for entity_id in entity_ids if _state(port, token, entity_id) is None]
        if not missing:
            return
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for entities: {', '.join(missing)}")


def _require_flow(flow: dict[str, Any], *, flow_type: str, step_id: str | None = None) -> None:
    """Raise if a config flow is not at the expected type and step."""
    if flow.get("type") != flow_type:
        raise RuntimeError(f"Expected flow type {flow_type}, got {flow}")
    if step_id is not None and flow.get("step_id") != step_id:
        raise RuntimeError(f"Expected flow step {step_id}, got {flow}")


def _adapter_source(entity_id: str) -> dict[str, Any]:
    """Return source-adapter input for a testbed future-values entity."""
    return {
        CONF_WATTPLAN_ENTITY_ID: [entity_id],
        CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
        SECTION_SOURCE_MANUAL: {
            CONF_NAME: "future_values",
            "time_key": "start",
            "value_key": "value",
        },
        CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
    }


def _accept_source_review(
    port: int, token: str, path: str, flow: dict[str, Any]
) -> dict[str, Any]:
    """Accept the WattPlan source review step."""
    _require_flow(flow, flow_type="form", step_id="source_review")
    return _continue_flow(
        port,
        token,
        path,
        flow,
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )


def _wattplan_entry(port: int, token: str) -> dict[str, Any] | None:
    """Return the dedicated WattPlan demo entry if it exists."""
    for entry in _entries(port, token, "wattplan"):
        if entry.get("title") == WATTPLAN_TITLE:
            return entry
    return None


def ensure_wattplan_demo_entry(port: int, token: str, testbed_entry_id: str) -> str:
    """Create a WattPlan entry wired to the demo testbed assets."""
    if entry := _wattplan_entry(port, token):
        print(f"WattPlan entry already exists: {entry['entry_id']}")
        return str(entry["entry_id"])

    path = "/api/config/config_entries/flow"
    flow = _flow_post(port, token, path, {"handler": "wattplan"})
    _require_flow(flow, flow_type="form", step_id="requirements")
    flow = _continue_flow(port, token, path, flow, {})
    _require_flow(flow, flow_type="form", step_id="planner_setup")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {
            CONF_NAME: WATTPLAN_TITLE,
            CONF_SLOT_MINUTES: "15",
            CONF_HOURS_TO_PLAN: "48",
        },
    )

    _require_flow(flow, flow_type="form", step_id="source_price")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER},
    )
    _require_flow(flow, flow_type="form", step_id="source_price_adapter")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        _adapter_source(_asset_entity(IMPORT_PRICE_NAME, "price")),
    )
    flow = _accept_source_review(port, token, path, flow)

    _require_flow(flow, flow_type="form", step_id="source_usage")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN},
    )
    _require_flow(flow, flow_type="form", step_id="source_usage_built_in")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {
            CONF_WATTPLAN_ENTITY_ID: _asset_entity(HEAVY_LOAD_NAME, "load_energy"),
            CONF_HISTORY_DAYS: 14,
        },
    )
    flow = _accept_source_review(port, token, path, flow)

    _require_flow(flow, flow_type="form", step_id="source_pv")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER},
    )
    _require_flow(flow, flow_type="form", step_id="source_pv_energy_provider")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {
            CONF_CONFIG_ENTRY_ID: testbed_entry_id,
            CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
        },
    )
    flow = _accept_source_review(port, token, path, flow)

    _require_flow(flow, flow_type="form", step_id="source_export_price")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER},
    )
    _require_flow(flow, flow_type="form", step_id="source_export_price_adapter")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        _adapter_source(_asset_entity(EXPORT_PRICE_NAME, "price")),
    )
    flow = _accept_source_review(port, token, path, flow)

    _require_flow(flow, flow_type="form", step_id="setup_complete")
    flow = _continue_flow(port, token, path, flow, {})
    _require_flow(flow, flow_type="create_entry")
    entry_id = str(flow["result"]["entry_id"])
    print(f"Created WattPlan entry: {entry_id}")
    return entry_id


def _create_wattplan_battery(
    port: int,
    token: str,
    entry: dict[str, Any],
    *,
    name: str,
    capacity_kwh: float,
) -> None:
    """Create one WattPlan battery subentry if missing."""
    if _has_subentry(entry, "battery", name):
        print(f"Already exists: WattPlan battery {name}")
        return

    path = "/api/config/config_entries/subentries/flow"
    flow = _flow_post(
        port,
        token,
        path,
        {"handler": [entry["entry_id"], "battery"]},
    )
    _require_flow(flow, flow_type="form", step_id="user")
    flow = _continue_flow(
        port,
        token,
        path,
        flow,
        {
            CONF_NAME: name,
            CONF_SOC_SOURCE: _asset_entity(name, "soc"),
            CONF_AVAILABILITY_SOURCE: _asset_entity(name, "available", domain="binary_sensor"),
            CONF_CAPACITY_KWH: capacity_kwh,
            CONF_MINIMUM_KWH: 1.0,
            CONF_MAX_CHARGE_KW: 5.0,
            CONF_MAX_DISCHARGE_KW: 5.0,
            SECTION_BATTERY_ADVANCED: {
                CONF_CHARGE_EFFICIENCY: 0.9,
                CONF_DISCHARGE_EFFICIENCY: 0.9,
            },
            CONF_CAN_CHARGE_FROM_GRID: True,
            CONF_CAN_CHARGE_FROM_PV: True,
        },
    )
    _require_flow(flow, flow_type="form", step_id="complete")
    flow = _continue_flow(port, token, path, flow, {})
    _require_flow(flow, flow_type="create_entry")
    print(f"Created WattPlan battery: {name}")


def ensure_wattplan_demo_batteries(
    config_dir: Path, port: int, token: str, entry_id: str
) -> None:
    """Create WattPlan battery subentries for the generated batteries."""
    entry = _stored_entry(config_dir, "wattplan", entry_id) or _entry(
        port, token, "wattplan", entry_id
    )
    _create_wattplan_battery(port, token, entry, name=HOME_BATTERY_NAME, capacity_kwh=10.0)
    entry = _stored_entry(config_dir, "wattplan", entry_id) or _entry(
        port, token, "wattplan", entry_id
    )
    _create_wattplan_battery(port, token, entry, name=EV_BATTERY_NAME, capacity_kwh=10.0)
    _request(port, "POST", f"/api/config/config_entries/entry/{entry_id}/reload", token=token, json_data={})
    print("Reloaded WattPlan entry")


def configure_wattplan_demo(
    config_dir: Path, port: int, token: str, testbed_entry_id: str
) -> str:
    """Wire WattPlan to the representative testbed assets."""
    wait_for_entities(
        port,
        token,
        [
            _asset_entity(IMPORT_PRICE_NAME, "price"),
            _asset_entity(EXPORT_PRICE_NAME, "price"),
            _asset_entity(PV_NAME, "pv_power"),
            _asset_entity(HEAVY_LOAD_NAME, "load_power"),
            _asset_entity(HEAVY_LOAD_NAME, "load_energy"),
            _asset_entity(HOME_BATTERY_NAME, "soc"),
            _asset_entity(HOME_BATTERY_NAME, "available", domain="binary_sensor"),
            _asset_entity(EV_BATTERY_NAME, "soc"),
            _asset_entity(EV_BATTERY_NAME, "available", domain="binary_sensor"),
        ],
    )
    entry_id = ensure_wattplan_demo_entry(port, token, testbed_entry_id)
    ensure_wattplan_demo_batteries(config_dir, port, token, entry_id)
    return entry_id


def main() -> None:
    """Run the CLI."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--hass", type=Path, default=DEFAULT_HASS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("reset")
    sub.add_parser("add-demo-assets")
    sub.add_parser("configure-wattplan-demo")
    args = parser.parse_args()

    if args.command == "reset":
        stop_ha(args.config)
        shutil.rmtree(args.config, ignore_errors=True)
        print(f"Removed {args.config}")
        return
    if args.command == "stop":
        stop_ha(args.config)
        return
    if args.command in {"bootstrap", "start", "add-demo-assets", "configure-wattplan-demo"}:
        start_ha(args.config, args.hass, args.port)
    if args.command == "start":
        return

    try:
        token = ensure_auth(args.config, args.port)
    except OnboardingRestartRequired:
        print("Restarting Home Assistant to finish source-checkout onboarding")
        stop_ha(args.config)
        start_ha(args.config, args.hass, args.port)
        token = ensure_auth(args.config, args.port)
    entry_id = ensure_testbed_entry(args.port, token)
    print(f"Testbed entry: {entry_id}")
    if args.command in {"add-demo-assets", "configure-wattplan-demo"}:
        add_demo_assets(args.config, args.port, token, entry_id)
    if args.command == "configure-wattplan-demo":
        wattplan_entry_id = configure_wattplan_demo(
            args.config, args.port, token, entry_id
        )
        print(f"WattPlan entry: {wattplan_entry_id}")
        print(f"Home Assistant: http://127.0.0.1:{args.port}")


if __name__ == "__main__":
    main()
