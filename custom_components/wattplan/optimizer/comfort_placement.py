"""Bounded cost-aware placement for already-scheduled comfort loads.

This module deliberately does not build a joint optimization model. It makes a
single deterministic pass over comfort schedules and evaluates bounded local
moves through a caller-provided whole-system cost function.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math


Schedule = tuple[bool, ...]
CostCallback = Callable[[tuple[Schedule, ...]], float]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class ComfortPlacementInput:
    """One comfort constraint set and its existing schedule."""

    name: str
    schedule: Schedule
    on_history: Schedule
    rolling_window_slots: int
    target_on_slots_per_rolling_window: int
    min_consecutive_on_slots: int
    min_consecutive_off_slots: int
    max_consecutive_off_slots: int
    is_on_now: bool
    off_streak_slots_now: int
    lock_remaining: int = 0
    lock_mode: bool | None = None


@dataclass(frozen=True)
class ComfortViolation:
    """A hard comfort constraint violation at a forecast slot."""

    code: str
    slot: int


@dataclass(frozen=True)
class ComfortPlacementResult:
    """Schedules and bounded-search accounting, aligned with input order."""

    schedules: tuple[Schedule, ...]
    status: str
    violations: tuple[tuple[ComfortViolation, ...], ...]
    baseline_cost: float | None = None
    final_cost: float | None = None
    candidates_considered: int = 0
    candidates_evaluated: int = 0
    candidates_rejected: int = 0
    accepted_moves: int = 0
    optimality: str = "heuristic"


def check_comfort_schedule(
    comfort: ComfortPlacementInput,
    schedule: Sequence[bool] | None = None,
) -> tuple[ComfortViolation, ...]:
    """Return every hard violation in a proposed schedule."""

    _validate_comfort(comfort)
    proposed = comfort.schedule if schedule is None else tuple(schedule)
    if len(proposed) != len(comfort.schedule):
        raise ValueError("candidate schedule length must match the existing schedule")
    if any(not isinstance(value, bool) for value in proposed):
        raise ValueError("candidate schedules must contain booleans")
    return _schedule_violations(comfort, proposed)


def _schedule_violations(
    comfort: ComfortPlacementInput,
    proposed: Schedule,
) -> tuple[ComfortViolation, ...]:
    """Check a shape-validated schedule without repeating static validation."""

    violations: list[ComfortViolation] = []
    horizon = len(proposed)
    lock_mode = comfort.is_on_now if comfort.lock_mode is None else comfort.lock_mode
    for slot in range(min(comfort.lock_remaining, horizon)):
        if proposed[slot] != lock_mode:
            violations.append(ComfortViolation("initial_lock", slot))

    previous = comfort.is_on_now
    for slot, enabled in enumerate(proposed):
        if enabled == previous:
            continue
        minimum = (
            comfort.min_consecutive_on_slots
            if enabled
            else comfort.min_consecutive_off_slots
        )
        code = "min_on" if enabled else "min_off"
        if slot + minimum > horizon:
            violations.append(ComfortViolation(f"terminal_{code}", slot))
        elif any(value != enabled for value in proposed[slot : slot + minimum]):
            violations.append(ComfortViolation(code, slot))
        previous = enabled

    off_streak = 0 if comfort.is_on_now else comfort.off_streak_slots_now
    max_off_reported = False
    for slot, enabled in enumerate(proposed):
        off_streak = 0 if enabled else off_streak + 1
        if off_streak > comfort.max_consecutive_off_slots and not max_off_reported:
            violations.append(ComfortViolation("max_off", slot))
            max_off_reported = True

    combined = comfort.on_history + proposed
    window = comfort.rolling_window_slots
    target = comfort.target_on_slots_per_rolling_window
    rolling_on = sum(combined[:window])
    for slot in range(horizon):
        if rolling_on < target:
            violations.append(ComfortViolation("rolling_target", slot))
        if slot + 1 < horizon:
            rolling_on += int(combined[slot + window]) - int(combined[slot])
    return tuple(violations)


def place_comfort_schedules(
    comforts: Sequence[ComfortPlacementInput],
    evaluate_cost: CostCallback,
    *,
    max_candidates_per_comfort: int = 32,
    max_total_candidates: int = 128,
    minimum_improvement: float = 0.0,
    should_cancel: CancelCallback | None = None,
) -> ComfortPlacementResult:
    """Improve validated schedules with bounded single-run relocation moves.

    The callback receives all schedules in input order. Each comfort is visited
    once, and later comforts are evaluated against moves already accepted for
    earlier comforts. The result is therefore a heuristic, not a global optimum.
    """

    if max_candidates_per_comfort < 0 or max_total_candidates < 0:
        raise ValueError("candidate limits must be non-negative")
    if minimum_improvement < 0 or not math.isfinite(minimum_improvement):
        raise ValueError("minimum_improvement must be finite and non-negative")
    if not comforts:
        return ComfortPlacementResult((), "no_comfort", ())

    _validate_inputs(comforts)
    schedules = tuple(comfort.schedule for comfort in comforts)
    baseline_violations = tuple(
        _schedule_violations(comfort, comfort.schedule) for comfort in comforts
    )
    if any(baseline_violations):
        return ComfortPlacementResult(
            schedules,
            "infeasible",
            baseline_violations,
        )
    if max_candidates_per_comfort == 0 or max_total_candidates == 0:
        return ComfortPlacementResult(schedules, "no_flexibility", baseline_violations)

    accepted = list(schedules)
    baseline_cost: float | None = None
    current_cost: float | None = None
    considered = 0
    evaluated = 0
    rejected = 0
    accepted_moves = 0
    generated_any = False
    feasible_move_found = False
    cancelled = False

    for comfort_index, comfort in enumerate(comforts):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        remaining_budget = max_total_candidates - considered
        if remaining_budget <= 0:
            break
        entity_limit = min(max_candidates_per_comfort, remaining_budget)
        candidates = _relocation_candidates(
            accepted[comfort_index],
            lock_remaining=comfort.lock_remaining,
            limit=entity_limit,
        )
        best_schedule = accepted[comfort_index]
        best_cost = current_cost
        for candidate in candidates:
            generated_any = True
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            considered += 1
            if _schedule_violations(comfort, candidate):
                rejected += 1
                continue
            feasible_move_found = True
            candidate_schedules = tuple(
                candidate if index == comfort_index else schedule
                for index, schedule in enumerate(accepted)
            )
            if current_cost is None:
                current_cost = _finite_cost(evaluate_cost(tuple(accepted)))
                baseline_cost = current_cost
                best_cost = current_cost
            candidate_cost = _finite_cost(evaluate_cost(candidate_schedules))
            evaluated += 1
            assert best_cost is not None
            if candidate_cost < best_cost - minimum_improvement:
                best_cost = candidate_cost
                best_schedule = candidate
        if best_schedule != accepted[comfort_index]:
            accepted[comfort_index] = best_schedule
            current_cost = best_cost
            accepted_moves += 1
        if cancelled:
            break

    status = "cancelled" if cancelled else "unchanged"
    if not cancelled and accepted_moves:
        status = "improved"
    elif not cancelled and (not generated_any or not feasible_move_found):
        status = "no_flexibility"
    final_schedules = tuple(accepted)
    return ComfortPlacementResult(
        final_schedules,
        status,
        tuple(
            _schedule_violations(comfort, final_schedules[index])
            for index, comfort in enumerate(comforts)
        ),
        baseline_cost=baseline_cost,
        final_cost=current_cost,
        candidates_considered=considered,
        candidates_evaluated=evaluated,
        candidates_rejected=rejected,
        accepted_moves=accepted_moves,
    )


def _finite_cost(value: float) -> float:
    cost = float(value)
    if not math.isfinite(cost):
        raise ValueError("cost callback must return a finite number")
    return cost


def _validate_inputs(comforts: Sequence[ComfortPlacementInput]) -> None:
    names: set[str] = set()
    horizon = len(comforts[0].schedule)
    for comfort in comforts:
        _validate_comfort(comfort)
        if comfort.name in names:
            raise ValueError(f"duplicate comfort name: {comfort.name}")
        names.add(comfort.name)
        if len(comfort.schedule) != horizon:
            raise ValueError("all comfort schedules must have the same horizon")


def _validate_comfort(comfort: ComfortPlacementInput) -> None:
    if not comfort.name.strip():
        raise ValueError("comfort name must be non-empty")
    if any(not isinstance(value, bool) for value in comfort.schedule):
        raise ValueError("comfort schedules must contain booleans")
    if any(not isinstance(value, bool) for value in comfort.on_history):
        raise ValueError("comfort history must contain booleans")
    if comfort.rolling_window_slots < 1:
        raise ValueError("rolling_window_slots must be positive")
    if len(comfort.on_history) != comfort.rolling_window_slots - 1:
        raise ValueError("on_history must contain rolling_window_slots - 1 values")
    if not (
        1
        <= comfort.target_on_slots_per_rolling_window
        <= comfort.rolling_window_slots
    ):
        raise ValueError("rolling target must be within the rolling window")
    if (
        comfort.min_consecutive_on_slots < 1
        or comfort.min_consecutive_off_slots < 1
    ):
        raise ValueError("minimum run lengths must be positive")
    horizon = len(comfort.schedule)
    if horizon < 2:
        raise ValueError("comfort schedule horizon must contain at least two slots")
    if (
        comfort.min_consecutive_on_slots >= horizon
        or comfort.min_consecutive_off_slots >= horizon
    ):
        raise ValueError("minimum run lengths must be shorter than the horizon")
    if comfort.max_consecutive_off_slots < comfort.min_consecutive_off_slots:
        raise ValueError("max_consecutive_off_slots must be >= minimum off run")
    if comfort.off_streak_slots_now < 0 or comfort.lock_remaining < 0:
        raise ValueError("runtime counters must be non-negative")
    if comfort.is_on_now and comfort.off_streak_slots_now != 0:
        raise ValueError("off_streak_slots_now must be zero while comfort is on")
    if comfort.lock_mode is not None and not isinstance(comfort.lock_mode, bool):
        raise ValueError("lock_mode must be a boolean")


def _relocation_candidates(
    schedule: Schedule,
    *,
    lock_remaining: int,
    limit: int,
) -> tuple[Schedule, ...]:
    runs = [
        run
        for run in _on_runs(schedule)
        if run[0] >= min(lock_remaining, len(schedule))
    ]
    if not runs or limit == 0:
        return ()

    per_run_limit = max(1, math.ceil(limit / len(runs)))
    starts_by_run = [
        _sample_starts(len(schedule) - (end - start), start, per_run_limit)
        for start, end in runs
    ]
    candidates: list[Schedule] = []
    seen: set[Schedule] = {schedule}
    position = 0
    while len(candidates) < limit:
        added = False
        for (start, end), starts in zip(runs, starts_by_run, strict=True):
            if position >= len(starts):
                continue
            added = True
            new_start = starts[position]
            candidate = list(schedule)
            candidate[start:end] = [False] * (end - start)
            candidate[new_start : new_start + end - start] = [True] * (end - start)
            moved = tuple(candidate)
            if sum(moved) == sum(schedule) and moved not in seen:
                seen.add(moved)
                candidates.append(moved)
                if len(candidates) == limit:
                    break
        if not added:
            break
        position += 1
    return tuple(candidates)


def _on_runs(schedule: Schedule) -> tuple[tuple[int, int], ...]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, enabled in enumerate(schedule + (False,)):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            runs.append((start, index))
            start = None
    return tuple(runs)


def _sample_starts(max_start: int, original: int, limit: int) -> tuple[int, ...]:
    available = max_start + 1
    if available <= limit + 1:
        return tuple(start for start in range(available) if start != original)
    sampled = {
        round(index * max_start / max(limit, 1))
        for index in range(limit + 1)
    }
    sampled.discard(original)
    if len(sampled) < limit:
        for start in range(available):
            if start != original:
                sampled.add(start)
            if len(sampled) == limit:
                break
    return tuple(sorted(sampled)[:limit])
