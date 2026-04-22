import base64
import hashlib
import json
from dataclasses import dataclass
from enum import IntFlag
from typing import List

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SOLVE_HORIZON_SLOTS = 22


class ChargeSource(IntFlag):
    GRID = 0x01
    PV = 0x02


class BatteryEntityParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    class TargetParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

        timeslot: int = Field(
            ..., ge=0, description="Deadline timeslot (inclusive, end-of-timeslot)."
        )
        soc_kwh: float = Field(..., ge=0, description="Target state-of-charge in kWh.")
        mode: str = Field(
            "at_least",
            description="Target mode: at_least, at_most, or exact.",
        )
        tolerance_kwh: float = Field(
            0.0, ge=0, description="Allowed kWh tolerance around target level."
        )

        @field_validator("mode")
        @classmethod
        def _validate_mode(cls, value):
            allowed = {"at_least", "at_most", "exact"}
            if value not in allowed:
                raise ValueError(f"mode must be one of {sorted(allowed)}")
            return value

    name: str = Field(
        ...,
        min_length=1,
        description="Name of the battery-like entity (e.g., house battery, car).",
    )
    initial_kwh: float = Field(..., description="Initial charge level (in kWh).")
    minimum_kwh: float = Field(..., description="Minimum charge level (in kWh).")
    capacity_kwh: float = Field(..., description="Capacity of the battery (in kWh).")
    target: TargetParams | None = Field(
        default=None,
        description="Optional deadline target object.",
    )
    charge_curve_kwh: List[float] = Field(
        ..., description="Charge energy-per-slot curve (in kWh per slot)."
    )
    discharge_curve_kwh: List[float] = Field(
        ..., description="Discharge energy-per-slot curve (in kWh per slot)."
    )
    charge_efficiency: float = Field(
        1.0, description="Charge efficiency fraction (0, 1]."
    )
    discharge_efficiency: float = Field(
        1.0, description="Discharge efficiency fraction (0, 1]."
    )
    prefer_pv_surplus_charging: bool = Field(
        False,
        description="Internal/deferred PV surplus sink hint; not a public action state.",
    )
    can_charge_from: int = Field(
        int(ChargeSource.PV),
        description="Bitmask of allowed charge sources: GRID=1, PV=2.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value):
        if not value or not value.strip():
            raise ValueError("name must be non-empty")
        return value

    @field_validator("charge_curve_kwh", "discharge_curve_kwh")
    @classmethod
    def _validate_curve(cls, values):
        if len(values) == 0:
            raise ValueError("must contain at least one value")
        normalized = []
        for value in values:
            number = float(value)
            if not np.isfinite(number):
                raise ValueError("must contain only finite numbers")
            if number < 0:
                raise ValueError("must contain only values >= 0")
            normalized.append(number)
        return normalized

    @field_validator("can_charge_from")
    @classmethod
    def _validate_can_charge_from(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("can_charge_from must be an integer bitmask")
        if value < 0:
            raise ValueError("can_charge_from must be >= 0")
        if (value & ~0x03) != 0:
            raise ValueError(
                "can_charge_from may only use bits 0x01 (GRID) and 0x02 (PV)"
            )
        return value

    @model_validator(mode="after")
    def _validate_ranges(self):
        for field_name in ("initial_kwh", "minimum_kwh", "capacity_kwh"):
            if not np.isfinite(float(getattr(self, field_name))):
                raise ValueError(f"{field_name} must be finite")
        for field_name in ("charge_efficiency", "discharge_efficiency"):
            value = float(getattr(self, field_name))
            if not np.isfinite(value):
                raise ValueError(f"{field_name} must be finite")
            if value <= 0.0 or value > 1.0:
                raise ValueError(f"{field_name} must be within (0, 1]")
        if self.capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be > 0")
        if self.minimum_kwh < 0:
            raise ValueError("minimum_kwh must be >= 0")
        if self.minimum_kwh > self.capacity_kwh:
            raise ValueError("minimum_kwh must be <= capacity_kwh")
        if self.initial_kwh < 0 or self.initial_kwh > self.capacity_kwh:
            raise ValueError("initial_kwh must be within [0, capacity_kwh]")
        if self.target is not None and self.target.soc_kwh > self.capacity_kwh:
            raise ValueError("target.soc_kwh must be <= capacity_kwh")
        return self


class ComfortEntityParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        description="Name of the comfort-like entity (e.g., heat pump).",
    )
    target_on_slots_per_rolling_window: int = Field(
        ..., ge=1, description="Required ON slots over the rolling window."
    )
    min_consecutive_on_slots: int = Field(
        1, ge=1, description="Minimum consecutive ON slots once enabled."
    )
    min_consecutive_off_slots: int = Field(
        1, ge=1, description="Minimum consecutive OFF slots once disabled."
    )
    max_consecutive_off_slots: int = Field(
        ...,
        ge=1,
        description="Maximum consecutive OFF slots before ON is forced.",
    )
    power_usage_kwh: float = Field(
        ..., gt=0, description="Energy consumed when the entity is ON (kWh per slot)."
    )
    is_on_now: bool = Field(..., description="Current ON/OFF runtime state.")
    on_slots_last_rolling_window: int = Field(
        ..., ge=0, description="Observed ON slots in previous rolling window."
    )
    off_streak_slots_now: int = Field(
        ..., ge=0, description="Current consecutive OFF slots at planning start."
    )
    measured_power_source: str | None = Field(
        default=None,
        description="Optional source label for measured runtime power data.",
    )
    recent_avg_on_power_kw: float | None = Field(
        default=None,
        gt=0,
        description="Optional observed average ON power for future adaptation.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value):
        if not value or not value.strip():
            raise ValueError("name must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_ranges(self):
        if not np.isfinite(float(self.power_usage_kwh)):
            raise ValueError("power_usage_kwh must be finite")
        if self.max_consecutive_off_slots < self.min_consecutive_off_slots:
            raise ValueError(
                "max_consecutive_off_slots must be >= min_consecutive_off_slots"
            )
        return self


class OptionalEntityParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Name of the optional load.")
    duration_timeslots: int = Field(
        ..., gt=0, description="Duration in solver timeslots."
    )
    start_after_timeslot: int = Field(
        0, ge=0, description="Earliest candidate start timeslot (inclusive)."
    )
    start_before_timeslot: int = Field(
        ..., ge=1, description="Latest boundary timeslot (exclusive)."
    )
    energy_kwh: float | List[float] = Field(
        ..., description="Either total energy or per-timeslot energy profile."
    )
    options: int = Field(3, gt=0, description="Number of start options to return.")
    min_option_gap_timeslots: int = Field(
        0,
        ge=0,
        description="Minimum gap between returned start options in timeslots.",
    )
    allow_overlapping_options: bool = Field(
        False,
        description="Whether suggested starts are allowed to overlap in runtime.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value):
        if not value or not value.strip():
            raise ValueError("name must be non-empty")
        return value

    @field_validator("energy_kwh")
    @classmethod
    def _validate_energy_kwh(cls, value):
        if isinstance(value, list):
            if len(value) == 0:
                raise ValueError("energy_kwh list must not be empty")
            for item in value:
                number = float(item)
                if not np.isfinite(number):
                    raise ValueError("energy_kwh list must contain only finite values")
                if number < 0:
                    raise ValueError("energy_kwh list values must be >= 0")
            return value

        number = float(value)
        if not np.isfinite(number):
            raise ValueError("energy_kwh must be finite")
        if number < 0:
            raise ValueError("energy_kwh must be >= 0")
        return value

    @model_validator(mode="after")
    def _validate_and_normalize(self):
        if self.start_after_timeslot >= self.start_before_timeslot:
            raise ValueError("start_after_timeslot must be < start_before_timeslot")

        duration = int(self.duration_timeslots)
        if isinstance(self.energy_kwh, list):
            profile = np.asarray(self.energy_kwh, dtype=np.float64)
            if profile.shape[0] > duration:
                raise ValueError("energy_kwh list length must be <= duration_timeslots")
            if profile.shape[0] == duration:
                expanded = profile
            else:
                n = profile.shape[0]
                expanded = np.zeros(duration, dtype=np.float64)
                for i in range(n):
                    start = (i * duration) // n
                    end = ((i + 1) * duration) // n
                    expanded[start:end] = profile[i]
            self.energy_kwh = [float(v) for v in expanded.tolist()]
        else:
            total_energy = float(self.energy_kwh)
            self.energy_kwh = [float(total_energy / duration) for _ in range(duration)]

        return self


class OptimizationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grid_import_price_per_kwh: List[float] = Field(
        ...,
        description="List of grid import prices (in currency per kWh).",
    )
    grid_export_price_per_kwh: List[float] = Field(
        default_factory=list,
        description=(
            "Optional list of grid export prices (currency per kWh). "
            "Defaults to all-zero values over the planning horizon."
        ),
    )
    solar_input_kwh: List[float] = Field(
        default_factory=list, description="Expected solar input (in kWh)."
    )
    usage_kwh: List[float] = Field(
        default_factory=list, description="Household usage (in kWh)."
    )
    rolling_window_slots: int = Field(
        24,
        ge=1,
        description="Rolling window size in slots used by comfort ON-slot accounting.",
    )
    throughput_cost_per_kwh: float = Field(
        0.0, description="Additional cost applied to charging/discharging throughput."
    )
    action_deadband_kwh: float = Field(
        0.0, description="Commands smaller than this are treated as neutral flow."
    )
    mode_switch_cost: float = Field(
        0.0, description="Cost for switching between modeled charge/idle/discharge flow."
    )
    infer_battery_preserve_policy: bool = Field(
        True,
        description=(
            "When true, run the model-backed counterfactual used to emit battery "
            "preserve policy states. When false, battery_preserve output flags "
            "are always false."
        ),
    )
    battery_entities: List[BatteryEntityParams] = Field(
        ..., description="List of battery-like entities."
    )
    comfort_entities: List[ComfortEntityParams] = Field(
        ..., description="List of comfort-like entities."
    )
    optional_entities: List[OptionalEntityParams] = Field(
        default_factory=list,
        description="Optional loads for best-start-time suggestions.",
    )
    state: str | None = Field(
        default=None,
        description="Opaque optimizer state from a previous run (base64).",
    )

    @field_validator("grid_import_price_per_kwh", "grid_export_price_per_kwh")
    @classmethod
    def _validate_prices(cls, values):
        if len(values) == 0:
            return values
        if len(values) < 4 or len(values) > 672:
            raise ValueError("must contain between 4 and 672 timeslots")
        for value in values:
            number = float(value)
            if not np.isfinite(number):
                raise ValueError("must contain only finite numbers")
        return values

    @field_validator("solar_input_kwh", "usage_kwh")
    @classmethod
    def _validate_nonnegative_series(cls, values):
        for value in values:
            number = float(value)
            if not np.isfinite(number):
                raise ValueError("must contain only finite numbers")
            if number < 0:
                raise ValueError("must contain only values >= 0")
        return values

    @field_validator("state")
    @classmethod
    def _validate_state_blob(cls, value):
        if value is None:
            return None
        if not value.strip():
            raise ValueError("state must be a non-empty base64 string")
        try:
            raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
            obj = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("state must be valid base64-encoded JSON") from exc
        if not isinstance(obj, dict):
            raise ValueError("state JSON must decode to an object")
        if obj.get("v") != 1:
            raise ValueError("state version is unsupported")
        return value

    @model_validator(mode="after")
    def _validate_cross_field_consistency(self):
        for field_name in (
            "throughput_cost_per_kwh",
            "action_deadband_kwh",
            "mode_switch_cost",
        ):
            value = float(getattr(self, field_name))
            if not np.isfinite(value):
                raise ValueError(f"{field_name} must be finite")
            if value < 0.0:
                raise ValueError(f"{field_name} must be >= 0")
        horizon = len(self.grid_import_price_per_kwh)
        solve_horizon = min(SOLVE_HORIZON_SLOTS, horizon)
        if len(self.grid_export_price_per_kwh) == 0:
            self.grid_export_price_per_kwh = [0.0] * horizon
        elif len(self.grid_export_price_per_kwh) != horizon:
            raise ValueError(
                "grid_export_price_per_kwh length must match "
                f"grid_import_price_per_kwh length ({horizon})"
            )
        if len(self.solar_input_kwh) != horizon:
            raise ValueError(
                "solar_input_kwh length must match "
                f"grid_import_price_per_kwh length ({horizon})"
            )
        if len(self.usage_kwh) != horizon:
            raise ValueError(
                "usage_kwh length must match "
                f"grid_import_price_per_kwh length ({horizon})"
            )

        names = set()
        for group_name, entities in (
            ("battery_entities", self.battery_entities),
            ("comfort_entities", self.comfort_entities),
            ("optional_entities", self.optional_entities),
        ):
            for idx, entity in enumerate(entities):
                key = entity.name.strip().lower()
                if key in names:
                    raise ValueError(
                        f"{group_name}[{idx}].name duplicates another entity name: {entity.name}"
                    )
                names.add(key)

        for idx, entity in enumerate(self.optional_entities):
            if entity.start_before_timeslot > horizon:
                raise ValueError(
                    f"optional_entities[{idx}].start_before_timeslot must be <= horizon ({horizon})"
                )
            if entity.duration_timeslots > horizon:
                raise ValueError(
                    f"optional_entities[{idx}].duration_timeslots must be <= horizon ({horizon})"
                )

            latest_start = entity.start_before_timeslot - entity.duration_timeslots
            candidate_count = latest_start - entity.start_after_timeslot + 1
            if candidate_count <= 0:
                raise ValueError(
                    f"optional_entities[{idx}] has no feasible start timeslots in window"
                )

            gap = max(0, entity.min_option_gap_timeslots)
            if not entity.allow_overlapping_options:
                gap = max(gap, entity.duration_timeslots)
            else:
                gap = max(gap, 1)

            max_options_possible = 1 + (candidate_count - 1) // gap
            if entity.options > max_options_possible:
                raise ValueError(
                    f"optional_entities[{idx}].options={entity.options} is infeasible; max possible is {max_options_possible}"
                )

        for idx, entity in enumerate(self.battery_entities):
            if entity.target is None:
                continue
            if entity.target.timeslot >= horizon:
                raise ValueError(
                    f"battery_entities[{idx}].target.timeslot must be < horizon ({horizon})"
                )

        for idx, entity in enumerate(self.comfort_entities):
            if entity.target_on_slots_per_rolling_window > int(
                self.rolling_window_slots
            ):
                raise ValueError(
                    f"comfort_entities[{idx}].target_on_slots_per_rolling_window must be <= rolling_window_slots ({self.rolling_window_slots})"
                )
            if entity.on_slots_last_rolling_window > int(self.rolling_window_slots):
                raise ValueError(
                    f"comfort_entities[{idx}].on_slots_last_rolling_window must be <= rolling_window_slots ({self.rolling_window_slots})"
                )
            if entity.min_consecutive_on_slots >= solve_horizon:
                raise ValueError(
                    f"comfort_entities[{idx}].min_consecutive_on_slots must be < solve horizon ({solve_horizon})"
                )
            if entity.min_consecutive_off_slots >= solve_horizon:
                raise ValueError(
                    f"comfort_entities[{idx}].min_consecutive_off_slots must be < solve horizon ({solve_horizon})"
                )

        return self


@dataclass(frozen=True)
class BatteryTarget:
    timeslot: int
    soc_kwh: float
    mode: str
    tolerance_kwh: float


@dataclass(frozen=True)
class BatteryEntity:
    name: str
    initial_kwh: float
    minimum_kwh: float
    capacity_kwh: float
    target: BatteryTarget | None
    charge_curve_kwh: List[float]
    discharge_curve_kwh: List[float]
    charge_efficiency: float
    discharge_efficiency: float
    throughput_cost_per_kwh: float
    action_deadband_kwh: float
    mode_switch_cost: float
    prefer_pv_surplus_charging: bool
    can_charge_from: int


@dataclass(frozen=True)
class ComfortEntity:
    name: str
    target_on_slots_per_rolling_window: int
    min_consecutive_on_slots: int
    min_consecutive_off_slots: int
    max_consecutive_off_slots: int
    power_usage_kwh: float
    is_on_now: bool
    on_slots_last_rolling_window: int
    off_streak_slots_now: int
    measured_power_source: str | None
    recent_avg_on_power_kw: float | None


@dataclass(frozen=True)
class NormalizedOptionalEntity:
    name: str
    duration_timeslots: int
    start_min: int
    start_max: int
    options: int
    required_gap: int
    energy_profile: np.ndarray


@dataclass(frozen=True)
class NormalizedState:
    num_steps: int
    entity_fingerprint: str
    grid_import_prices: np.ndarray
    grid_export_prices: np.ndarray
    solar_input: np.ndarray
    usage: np.ndarray
    battery_charge: np.ndarray
    battery_charge_grid: np.ndarray
    battery_charge_pv: np.ndarray
    battery_discharge: np.ndarray
    battery_preserve: np.ndarray
    comfort_on: np.ndarray
    comfort_lock_mode: np.ndarray
    comfort_lock_remaining: np.ndarray


@dataclass(frozen=True)
class CalculationInput:
    total_steps: int
    grid_import_prices: np.ndarray
    grid_export_prices: np.ndarray
    solar_input: np.ndarray
    usage: np.ndarray
    rolling_window_slots: int
    infer_battery_preserve_policy: bool
    battery_entities: List[BatteryEntity]
    comfort_entities: List[ComfortEntity]
    optional_entities: List[NormalizedOptionalEntity]
    state: NormalizedState | None
    fingerprint: str


def _entity_fingerprint(
    battery_entities,
    comfort_entities,
    rolling_window_slots,
    infer_battery_preserve_policy,
):
    # Reuse must only happen when the optimization problem is materially the
    # same. Battery targets change feasible early-slot decisions, so they must
    # participate in the fingerprint used to accept a previous state blob.
    payload = {
        "battery": [
            {
                "name": str(e.name),
                "minimum_kwh": float(e.minimum_kwh),
                "capacity_kwh": float(e.capacity_kwh),
                "charge_curve_kwh": [float(v) for v in e.charge_curve_kwh],
                "discharge_curve_kwh": [float(v) for v in e.discharge_curve_kwh],
                "charge_efficiency": float(e.charge_efficiency),
                "discharge_efficiency": float(e.discharge_efficiency),
                "throughput_cost_per_kwh": float(e.throughput_cost_per_kwh),
                "action_deadband_kwh": float(e.action_deadband_kwh),
                "mode_switch_cost": float(e.mode_switch_cost),
                "prefer_pv_surplus_charging": bool(
                    e.prefer_pv_surplus_charging
                ),
                "can_charge_from": int(e.can_charge_from),
                "target": (
                    {
                        "timeslot": int(e.target.timeslot),
                        "soc_kwh": float(e.target.soc_kwh),
                        "mode": str(e.target.mode),
                        "tolerance_kwh": float(e.target.tolerance_kwh),
                    }
                    if e.target is not None
                    else None
                ),
            }
            for e in battery_entities
        ],
        "comfort": [
            {
                "name": str(e.name),
                "target_on_slots_per_rolling_window": int(
                    e.target_on_slots_per_rolling_window
                ),
                "min_consecutive_on_slots": int(e.min_consecutive_on_slots),
                "min_consecutive_off_slots": int(e.min_consecutive_off_slots),
                "max_consecutive_off_slots": int(e.max_consecutive_off_slots),
                "power_usage_kwh": float(e.power_usage_kwh),
            }
            for e in comfort_entities
        ],
        "rolling_window_slots": int(rolling_window_slots),
        "infer_battery_preserve_policy": bool(infer_battery_preserve_policy),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def encode_state_blob(state_obj):
    raw = json.dumps(state_obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _parse_state_blob(state_blob):
    if state_blob is None:
        return None

    raw = base64.urlsafe_b64decode(str(state_blob).encode("ascii"))
    obj = json.loads(raw.decode("utf-8"))

    try:
        num_steps = int(obj["num_steps"])
        grid_import_prices = np.asarray(
            obj["grid_import_price_per_kwh"], dtype=np.float64
        )
        grid_export_prices = np.asarray(
            obj.get("grid_export_price_per_kwh", np.zeros(num_steps, dtype=np.float64)),
            dtype=np.float64,
        )
        solar_input = np.asarray(obj["solar_input_kwh"], dtype=np.float64)
        usage = np.asarray(obj["usage_kwh"], dtype=np.float64)
        battery_charge = np.asarray(obj["battery_charge"], dtype=np.float64)
        battery_charge_grid = np.asarray(obj["battery_charge_grid"], dtype=np.float64)
        battery_charge_pv = np.asarray(obj["battery_charge_pv"], dtype=np.float64)
        battery_discharge = np.asarray(obj["battery_discharge"], dtype=np.float64)
        battery_preserve = np.asarray(
            obj.get("battery_preserve", np.zeros_like(battery_discharge)),
            dtype=np.bool_,
        )
        comfort_on = np.asarray(obj["comfort_on"], dtype=np.float64)
        comfort_lock_mode = np.asarray(obj["comfort_lock_mode"], dtype=np.float64)
        comfort_lock_remaining = np.asarray(
            obj["comfort_lock_remaining"], dtype=np.float64
        )
    except KeyError as exc:
        raise ValueError(f"state is missing required key: {exc.args[0]}") from exc

    if num_steps < 1:
        raise ValueError("state.num_steps must be >= 1")

    if battery_charge.ndim == 1 and battery_charge.size == 0:
        battery_charge = battery_charge.reshape(0, num_steps)
    if battery_discharge.ndim == 1 and battery_discharge.size == 0:
        battery_discharge = battery_discharge.reshape(0, num_steps)
    if battery_charge_grid.ndim == 1 and battery_charge_grid.size == 0:
        battery_charge_grid = battery_charge_grid.reshape(0, num_steps)
    if battery_charge_pv.ndim == 1 and battery_charge_pv.size == 0:
        battery_charge_pv = battery_charge_pv.reshape(0, num_steps)
    if battery_preserve.ndim == 1 and battery_preserve.size == 0:
        battery_preserve = battery_preserve.reshape(0, num_steps)
    if comfort_on.ndim == 1 and comfort_on.size == 0:
        comfort_on = comfort_on.reshape(0, num_steps)
    if comfort_lock_mode.ndim == 1 and comfort_lock_mode.size == 0:
        comfort_lock_mode = comfort_lock_mode.reshape(0, num_steps)
    if comfort_lock_remaining.ndim == 1 and comfort_lock_remaining.size == 0:
        comfort_lock_remaining = comfort_lock_remaining.reshape(0, num_steps)

    if grid_import_prices.shape != (num_steps,):
        raise ValueError("state.grid_import_price_per_kwh shape mismatch")
    if grid_export_prices.shape != (num_steps,):
        raise ValueError("state.grid_export_price_per_kwh shape mismatch")
    if solar_input.shape != (num_steps,):
        raise ValueError("state.solar_input_kwh shape mismatch")
    if usage.shape != (num_steps,):
        raise ValueError("state.usage_kwh shape mismatch")
    if battery_charge.ndim != 2 or battery_charge.shape[1] != num_steps:
        raise ValueError("state.battery_charge shape mismatch")
    if battery_discharge.ndim != 2 or battery_discharge.shape[1] != num_steps:
        raise ValueError("state.battery_discharge shape mismatch")
    if battery_charge_grid.ndim != 2 or battery_charge_grid.shape[1] != num_steps:
        raise ValueError("state.battery_charge_grid shape mismatch")
    if battery_charge_pv.ndim != 2 or battery_charge_pv.shape[1] != num_steps:
        raise ValueError("state.battery_charge_pv shape mismatch")
    if battery_preserve.ndim != 2 or battery_preserve.shape[1] != num_steps:
        raise ValueError("state.battery_preserve shape mismatch")
    if comfort_on.ndim != 2 or comfort_on.shape[1] != num_steps:
        raise ValueError("state.comfort_on shape mismatch")
    if comfort_lock_mode.ndim != 2 or comfort_lock_mode.shape[1] != num_steps:
        raise ValueError("state.comfort_lock_mode shape mismatch")
    if comfort_lock_remaining.ndim != 2 or comfort_lock_remaining.shape[1] != num_steps:
        raise ValueError("state.comfort_lock_remaining shape mismatch")

    for series_name, series in (
        ("grid_import_price_per_kwh", grid_import_prices),
        ("grid_export_price_per_kwh", grid_export_prices),
        ("solar_input_kwh", solar_input),
        ("usage_kwh", usage),
        ("battery_charge", battery_charge),
        ("battery_charge_grid", battery_charge_grid),
        ("battery_charge_pv", battery_charge_pv),
        ("battery_discharge", battery_discharge),
        ("comfort_on", comfort_on),
        ("comfort_lock_mode", comfort_lock_mode),
        ("comfort_lock_remaining", comfort_lock_remaining),
    ):
        if not np.all(np.isfinite(series)):
            raise ValueError(f"state.{series_name} must contain finite values")

    return NormalizedState(
        num_steps=num_steps,
        entity_fingerprint=str(obj["entity_fingerprint"]),
        grid_import_prices=grid_import_prices,
        grid_export_prices=grid_export_prices,
        solar_input=solar_input,
        usage=usage,
        battery_charge=battery_charge,
        battery_charge_grid=battery_charge_grid,
        battery_charge_pv=battery_charge_pv,
        battery_discharge=battery_discharge,
        battery_preserve=battery_preserve,
        comfort_on=comfort_on,
        comfort_lock_mode=comfort_lock_mode,
        comfort_lock_remaining=comfort_lock_remaining,
    )


def normalize_calculation_input(params: OptimizationParams):
    total_steps = len(params.grid_import_price_per_kwh)
    grid_import_prices = np.asarray(params.grid_import_price_per_kwh, dtype=np.float64)
    grid_export_prices = np.asarray(params.grid_export_price_per_kwh, dtype=np.float64)
    solar_input = np.asarray(params.solar_input_kwh, dtype=np.float64)
    usage = np.asarray(params.usage_kwh, dtype=np.float64)

    battery_entities = []
    for entity in params.battery_entities:
        target = None
        if entity.target is not None:
            target = BatteryTarget(
                timeslot=int(entity.target.timeslot),
                soc_kwh=float(entity.target.soc_kwh),
                mode=str(entity.target.mode),
                tolerance_kwh=float(entity.target.tolerance_kwh),
            )

        battery_entities.append(
            BatteryEntity(
                name=entity.name,
                initial_kwh=float(entity.initial_kwh),
                minimum_kwh=float(entity.minimum_kwh),
                capacity_kwh=float(entity.capacity_kwh),
                target=target,
                charge_curve_kwh=[float(v) for v in entity.charge_curve_kwh],
                discharge_curve_kwh=[float(v) for v in entity.discharge_curve_kwh],
                charge_efficiency=float(entity.charge_efficiency),
                discharge_efficiency=float(entity.discharge_efficiency),
                throughput_cost_per_kwh=float(params.throughput_cost_per_kwh),
                action_deadband_kwh=float(params.action_deadband_kwh),
                mode_switch_cost=float(params.mode_switch_cost),
                prefer_pv_surplus_charging=bool(
                    entity.prefer_pv_surplus_charging
                ),
                can_charge_from=int(entity.can_charge_from),
            )
        )

    comfort_entities = [
        ComfortEntity(
            name=entity.name,
            target_on_slots_per_rolling_window=int(
                entity.target_on_slots_per_rolling_window
            ),
            min_consecutive_on_slots=int(entity.min_consecutive_on_slots),
            min_consecutive_off_slots=int(entity.min_consecutive_off_slots),
            max_consecutive_off_slots=int(entity.max_consecutive_off_slots),
            power_usage_kwh=float(entity.power_usage_kwh),
            is_on_now=bool(entity.is_on_now),
            on_slots_last_rolling_window=int(entity.on_slots_last_rolling_window),
            off_streak_slots_now=int(entity.off_streak_slots_now),
            measured_power_source=entity.measured_power_source,
            recent_avg_on_power_kw=(
                float(entity.recent_avg_on_power_kw)
                if entity.recent_avg_on_power_kw is not None
                else None
            ),
        )
        for entity in params.comfort_entities
    ]

    optional_entities = []
    for entity in params.optional_entities:
        start_min = int(entity.start_after_timeslot)
        start_max = int(entity.start_before_timeslot) - int(entity.duration_timeslots)
        required_gap = max(0, int(entity.min_option_gap_timeslots))
        if not entity.allow_overlapping_options:
            required_gap = max(required_gap, int(entity.duration_timeslots))
        optional_entities.append(
            NormalizedOptionalEntity(
                name=entity.name,
                duration_timeslots=int(entity.duration_timeslots),
                start_min=start_min,
                start_max=start_max,
                options=int(entity.options),
                required_gap=required_gap,
                energy_profile=np.asarray(entity.energy_kwh, dtype=np.float64),
            )
        )

    fingerprint = _entity_fingerprint(
        battery_entities,
        comfort_entities,
        params.rolling_window_slots,
        params.infer_battery_preserve_policy,
    )
    state = _parse_state_blob(params.state)

    return CalculationInput(
        total_steps=total_steps,
        grid_import_prices=grid_import_prices,
        grid_export_prices=grid_export_prices,
        solar_input=solar_input,
        usage=usage,
        rolling_window_slots=int(params.rolling_window_slots),
        infer_battery_preserve_policy=bool(params.infer_battery_preserve_policy),
        battery_entities=battery_entities,
        comfort_entities=comfort_entities,
        optional_entities=optional_entities,
        state=state,
        fingerprint=fingerprint,
    )
