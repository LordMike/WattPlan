"""Planner output projection helpers for the coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from ..const import DOMAIN
from ..coordinator_parts import CoordinatorSnapshot, TimingEntry


def _duration_ms(started_at: float) -> int:
    """Return elapsed monotonic time in whole milliseconds."""
    return int(round((time.monotonic() - started_at) * 1000))


class PlannerProjectionBuilder:
    """Project optimizer output into coordinator-facing diagnostics and snapshots."""

    def __init__(self, hass: HomeAssistant, *, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id

    def planner_output_from_result(
        self,
        request: dict[str, Any],
        result: dict[str, Any],
        *,
        timings: list[TimingEntry],
    ) -> dict[str, Any]:
        """Map optimizer output to the coordinator planner-output shape."""
        start_at = request["window"].start_at
        slot_minutes = int(request["slot_minutes"])
        horizon_slots = len(request["optimizer_params"]["grid_import_price_per_kwh"])

        batteries: dict[str, dict[str, Any]] = {}
        comforts: dict[str, dict[str, Any]] = {}
        optionals: dict[str, dict[str, Any]] = {}
        battery_charge_source: dict[str, str] = {}

        name_maps = request["name_to_subentry"]
        for entity in result.get("entities", []):
            entity_name = str(entity.get("name"))
            entity_type = str(entity.get("type"))
            schedule = list(entity.get("schedule", []))
            if not schedule:
                continue

            if entity_type == "battery":
                subentry_id = name_maps["batteries"].get(entity_name)
                if subentry_id is None:
                    continue
                current = schedule[0]
                current_source = self._map_charge_source(int(current.get("charge_source", 0)))
                battery_charge_source[subentry_id] = current_source
                next_change = self._next_change_point(
                    schedule,
                    key="state",
                    start_at=start_at,
                    slot_minutes=slot_minutes,
                )
                next_action_timestamp = next_change[0] if next_change is not None else None
                next_point = next_change[1] if next_change is not None else None
                next_action = (
                    str(next_point.get("state", "hold"))
                    if isinstance(next_point, dict)
                    else None
                )
                next_action_source = (
                    self._map_charge_source(int(next_point.get("charge_source", 0)))
                    if isinstance(next_point, dict)
                    and str(next_point.get("state", "hold")) == "charge"
                    else None
                )
                batteries[subentry_id] = {
                    "action": str(current.get("state", "hold")),
                    "charge_source": current_source,
                    "next_action_timestamp": (
                        next_action_timestamp.isoformat()
                        if next_action_timestamp is not None
                        else None
                    ),
                    "next_action": next_action,
                    "next_charge_source": next_action_source,
                }
                continue

            if entity_type == "comfort":
                subentry_id = name_maps["comforts"].get(entity_name)
                if subentry_id is None:
                    continue
                current = schedule[0]
                next_change = self._next_change_point(
                    schedule,
                    key="enabled",
                    start_at=start_at,
                    slot_minutes=slot_minutes,
                )
                next_action_timestamp = next_change[0] if next_change is not None else None
                next_point = next_change[1] if next_change is not None else None
                comforts[subentry_id] = {
                    "action": "on" if bool(current.get("enabled")) else "off",
                    "next_action_timestamp": (
                        next_action_timestamp.isoformat()
                        if next_action_timestamp is not None
                        else None
                    ),
                    "next_action": (
                        "on" if bool(next_point.get("enabled")) else "off"
                        if isinstance(next_point, dict)
                        else None
                    ),
                }

        for optional in result.get("optional_entity_options", []):
            entity_name = str(optional.get("name"))
            subentry_id = name_maps["optionals"].get(entity_name)
            if subentry_id is None:
                continue
            options = list(optional.get("options", []))
            optional_diag: dict[str, Any] = {}
            if options:
                first_start_slot = int(options[0]["start_timeslot"])
                first_end_slot = int(options[0]["end_timeslot"])
                optional_diag["next_start_option"] = (
                    start_at + timedelta(minutes=first_start_slot * slot_minutes)
                ).isoformat()
                optional_diag["next_end_option"] = (
                    start_at + timedelta(minutes=first_end_slot * slot_minutes)
                ).isoformat()
            for index, option in enumerate(options, start=1):
                start_slot = int(option["start_timeslot"])
                end_slot = int(option["end_timeslot"])
                optional_diag[f"option_{index}_start"] = (
                    start_at + timedelta(minutes=start_slot * slot_minutes)
                ).isoformat()
                optional_diag[f"option_{index}_end"] = (
                    start_at + timedelta(minutes=end_slot * slot_minutes)
                ).isoformat()
            optionals[subentry_id] = optional_diag

        is_suboptimal = bool(result.get("suboptimal", False))
        reasons = list(result.get("suboptimal_reasons", []))
        status = "degraded" if is_suboptimal else "ok"
        message = (
            f"Plan solved with suboptimal constraints: {', '.join(reasons)}"
            if is_suboptimal
            else "Plan solved"
        )
        return {
            "status": status,
            "message": message,
            "battery_charge_source": battery_charge_source,
            "diagnostics": {
                "batteries": batteries,
                "comforts": comforts,
                "optionals": optionals,
                "sources": {
                    "usage_forecast": request.get("usage_forecast_points"),
                },
                "optimizer": {
                    "execution_time_s": result.get("execution_time"),
                    "fitness": result.get("fitness"),
                    "avg_price": result.get("avg_price"),
                    "projections": result.get("projections"),
                    "suboptimal": is_suboptimal,
                    "suboptimal_reasons": reasons,
                    "problems": result.get("problems", []),
                    "successful_solves": result.get("successful_solves"),
                    "reused_steps": result.get("reused_steps"),
                    "span_start": start_at.isoformat(),
                    "span_end": (
                        start_at + timedelta(minutes=horizon_slots * slot_minutes)
                    ).isoformat(),
                },
                **self._build_enabled_plan_details(request, result, timings=timings),
            },
        }

    def project_snapshot(self, planner_output: dict[str, Any]) -> CoordinatorSnapshot:
        """Project planner output to immutable coordinator snapshot."""
        return CoordinatorSnapshot(
            created_at=datetime.now(tz=UTC),
            planner_status=str(planner_output.get("status", "unknown")),
            planner_message=(
                str(planner_output["message"])
                if planner_output.get("message") is not None
                else None
            ),
            battery_charge_source=planner_output.get("battery_charge_source"),
            diagnostics=planner_output.get("diagnostics"),
        )

    def _build_enabled_plan_details(
        self,
        request: dict[str, Any],
        result: dict[str, Any],
        *,
        timings: list[TimingEntry],
    ) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {}
        raw_details: dict[str, Any] | None = None

        if self._plan_details_enabled("plan_details"):
            started_at = time.monotonic()
            raw_details = self._build_plan_details_payload(request, result)
            timings.append(("Plan details payload build", _duration_ms(started_at)))
            diagnostics["plan_details"] = raw_details

        if self._plan_details_enabled("plan_details_hourly"):
            if raw_details is None:
                started_at = time.monotonic()
                raw_details = self._build_plan_details_payload(request, result)
                timings.append(("Plan details payload build", _duration_ms(started_at)))
            diagnostics["plan_details_hourly"] = self._aggregate_plan_details(
                raw_details,
                target_slot_minutes=60,
            )

        return diagnostics

    def _plan_details_enabled(self, details_key: str) -> bool:
        entity_registry = er.async_get(self._hass)
        unique_id = f"{self._entry_id}:entry:{details_key}"
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            return False
        if (entry := entity_registry.async_get(entity_id)) is None:
            return False
        return entry.disabled_by is None

    def _build_plan_details_payload(
        self, request: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        optimizer_params = request["optimizer_params"]
        horizon_slots = len(optimizer_params["grid_import_price_per_kwh"])
        plan_details: dict[str, Any] = {
            "start_at": request["window"].start_at.isoformat(),
            "slot_minutes": int(request["slot_minutes"]),
            "slots": horizon_slots,
            "grid_import_price_per_kwh": self._rounded_series(
                optimizer_params["grid_import_price_per_kwh"]
            ),
            "grid_export_price_per_kwh": self._rounded_series(
                optimizer_params["grid_export_price_per_kwh"]
            ),
            "usage_kwh": self._rounded_series(optimizer_params["usage_kwh"]),
            "solar_input_kwh": self._rounded_series(
                optimizer_params["solar_input_kwh"]
            ),
        }

        projections = result.get("projections")
        if isinstance(projections, dict):
            per_slot = projections.get("per_slot")
            if isinstance(per_slot, list) and len(per_slot) == horizon_slots:
                plan_details["projected_cost"] = self._projection_series(
                    per_slot, "projected_cost"
                )
                plan_details["projected_savings_cost"] = self._projection_series(
                    per_slot, "projected_savings_cost"
                )
                plan_details["projected_savings_pct"] = self._projection_series(
                    per_slot, "projected_savings_pct"
                )

        for entity in result.get("entities", []):
            entity_name = str(entity.get("name"))
            entity_type = str(entity.get("type"))
            schedule = list(entity.get("schedule", []))
            if entity_type == "battery":
                key_base = f"battery_{slugify(entity_name) or 'asset'}"
                plan_details[f"{key_base}_action"] = [
                    self._map_action_code(str(action))
                    for action in self._series_from_schedule(
                        schedule, horizon_slots, "state", default="hold", stringify=True
                    )
                ]
                plan_details[f"{key_base}_level_kwh"] = self._rounded_series(
                    self._series_from_schedule(
                        schedule, horizon_slots, "level", default=0.0
                    )
                )
                plan_details[f"{key_base}_charge_source"] = [
                    self._map_charge_source(int(value))
                    for value in self._series_from_schedule(
                        schedule, horizon_slots, "charge_source", default=0.0
                    )
                ]
                continue

            if entity_type == "comfort":
                key_base = f"comfort_{slugify(entity_name) or 'asset'}"
                plan_details[f"{key_base}_enabled"] = [
                    bool(value)
                    for value in self._series_from_schedule(
                        schedule, horizon_slots, "enabled", default=False
                    )
                ]

        for optional in result.get("optional_entity_options", []):
            entity_name = str(optional.get("name"))
            key = f"optional_{slugify(entity_name) or 'asset'}_enabled"
            values = [False] * horizon_slots
            for option in list(optional.get("options", [])):
                try:
                    start_slot = int(option["start_timeslot"])
                    end_slot = int(option["end_timeslot"])
                except (KeyError, TypeError, ValueError):
                    continue
                bounded_start = max(0, min(horizon_slots, start_slot))
                bounded_end = max(bounded_start, min(horizon_slots, end_slot))
                for index in range(bounded_start, bounded_end):
                    values[index] = True
            plan_details[key] = values

        return plan_details

    def _aggregate_plan_details(
        self,
        plan_details: dict[str, Any],
        *,
        target_slot_minutes: int,
    ) -> dict[str, Any]:
        slot_minutes = int(plan_details.get("slot_minutes", 0))
        if (
            slot_minutes <= 0
            or slot_minutes >= target_slot_minutes
            or target_slot_minutes % slot_minutes != 0
        ):
            return dict(plan_details)

        slots_per_bucket = target_slot_minutes // slot_minutes
        aggregated: dict[str, Any] = {
            "start_at": plan_details["start_at"],
            "slot_minutes": target_slot_minutes,
            "slots": (int(plan_details["slots"]) + slots_per_bucket - 1)
            // slots_per_bucket,
        }

        for key, value in plan_details.items():
            if key in {"start_at", "slot_minutes", "slots"}:
                continue
            if not isinstance(value, list):
                aggregated[key] = value
                continue
            aggregated[key] = self._aggregate_plan_details_series(
                key, value, slots_per_bucket
            )

        if (
            isinstance(aggregated.get("projected_cost"), list)
            and isinstance(aggregated.get("projected_savings_cost"), list)
            and len(aggregated["projected_cost"])
            == len(aggregated["projected_savings_cost"])
        ):
            aggregated["projected_savings_pct"] = (
                self._recompute_aggregated_percentages(
                    aggregated["projected_cost"],
                    aggregated["projected_savings_cost"],
                )
            )

        return aggregated

    def _aggregate_plan_details_series(
        self, key: str, values: list[Any], slots_per_bucket: int
    ) -> list[Any]:
        aggregated: list[Any] = []
        for start in range(0, len(values), slots_per_bucket):
            chunk = values[start : start + slots_per_bucket]
            if not chunk:
                continue
            first = chunk[0]
            if isinstance(first, bool):
                aggregated.append(any(bool(value) for value in chunk))
            elif isinstance(first, (int, float)) and not isinstance(first, bool):
                if key.endswith("_pct"):
                    aggregated.append(0.0)
                elif key in {"grid_import_price_per_kwh", "grid_export_price_per_kwh"}:
                    aggregated.append(round(sum(float(value) for value in chunk) / len(chunk), 4))
                elif key.endswith("_level_kwh"):
                    aggregated.append(round(sum(float(value) for value in chunk) / len(chunk), 2))
                else:
                    aggregated.append(round(sum(float(value) for value in chunk), 2))
            elif key.endswith("_charge_source"):
                sources = {str(value) for value in chunk if isinstance(value, str)}
                active_sources = sorted(source for source in sources if source != "n")
                aggregated.append("".join(active_sources) if active_sources else "n")
            elif key.endswith("_action"):
                actions = {str(value) for value in chunk if isinstance(value, str)}
                active_actions = sorted(action for action in actions if action != "h")
                aggregated.append("".join(active_actions) if active_actions else "h")
            else:
                aggregated.append(chunk[-1])
        return aggregated

    def _recompute_aggregated_percentages(
        self,
        costs: list[Any],
        savings: list[Any],
    ) -> list[float]:
        percentages: list[float] = []
        for cost, saving in zip(costs, savings, strict=False):
            try:
                cost_value = float(cost)
                saving_value = float(saving)
            except (TypeError, ValueError):
                percentages.append(0.0)
                continue
            baseline_value = cost_value + saving_value
            if baseline_value == 0:
                percentages.append(0.0)
                continue
            percentages.append(round((saving_value / baseline_value) * 100.0, 2))
        return percentages

    def _projection_series(self, per_slot: list[Any], key: str) -> list[float]:
        values: list[float] = []
        for slot in per_slot:
            if not isinstance(slot, dict):
                values.append(0.0)
                continue
            try:
                values.append(round(float(slot[key]), 2))
            except (KeyError, TypeError, ValueError):
                values.append(0.0)
        return values

    def _rounded_series(self, values: list[Any]) -> list[float]:
        rounded: list[float] = []
        for value in values:
            try:
                rounded.append(round(float(value), 2))
            except (TypeError, ValueError):
                rounded.append(0.0)
        return rounded

    def _series_from_schedule(
        self,
        schedule: list[dict[str, Any]],
        horizon_slots: int,
        key: str,
        *,
        default: Any,
        stringify: bool = False,
    ) -> list[Any]:
        values: list[Any] = []
        for index in range(horizon_slots):
            slot = schedule[index] if index < len(schedule) else None
            if not isinstance(slot, dict):
                values.append(default)
                continue
            value = slot.get(key, default)
            values.append(str(value) if stringify else value)
        return values

    def _next_change_point(
        self,
        schedule: list[dict[str, Any]],
        *,
        key: str,
        start_at: datetime,
        slot_minutes: int,
    ) -> tuple[datetime, dict[str, Any]] | None:
        if not schedule:
            return None
        current = schedule[0].get(key)
        for index, point in enumerate(schedule[1:], start=1):
            if point.get(key) != current:
                return (
                    start_at + timedelta(minutes=index * slot_minutes),
                    point,
                )
        return None

    def _map_charge_source(self, charge_source: int) -> str:
        if charge_source == 1:
            return "g"
        if charge_source == 2:
            return "p"
        if charge_source == 3:
            return "gp"
        return "n"

    def _map_action_code(self, action: str) -> str:
        return {
            "charge": "c",
            "discharge": "d",
            "hold": "h",
        }.get(action, "h")
