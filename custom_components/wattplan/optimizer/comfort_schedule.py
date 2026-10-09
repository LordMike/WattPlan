"""Solver-neutral comfort history and constraint-driven schedule construction."""

import numpy as np


def initial_comfort_history(entity, rolling_window_slots):
    history_slots = max(int(rolling_window_slots) - 1, 0)
    if entity.on_history is None:
        # A legacy aggregate over W prior slots guarantees only max(count-k, 0)
        # ON observations after k oldest slots have left the window. Placing that
        # guaranteed credit at the oldest edge models those lower bounds without
        # claiming an order the caller did not provide.
        history = np.zeros(history_slots, dtype=np.int32)
        guaranteed_on = min(
            max(int(entity.on_slots_last_rolling_window) - 1, 0), history_slots
        )
        history[:guaranteed_on] = 1
        return history
    return np.asarray(entity.on_history, dtype=np.int32).copy()


def fixed_comfort_schedule(
    comfort_entities,
    rolling_window_slots,
    total_steps,
    initial_lock_modes,
    initial_lock_remaining,
):
    """Build a cheap, constraint-driven comfort schedule outside the battery MILP."""
    schedule = np.zeros((len(comfort_entities), total_steps), dtype=np.float64)
    history_slots = max(int(rolling_window_slots) - 1, 0)
    for i, entity in enumerate(comfort_entities):
        history = initial_comfort_history(entity, rolling_window_slots).tolist()
        previous = bool(entity.is_on_now)
        mode = bool(initial_lock_modes[i])
        remaining = max(int(initial_lock_remaining[i]), 0)
        off_streak = 0 if previous else int(entity.off_streak_slots_now)
        for t in range(total_steps):
            target = int(entity.target_on_slots_per_rolling_window)
            max_off = int(entity.max_consecutive_off_slots)
            if remaining > 0:
                enabled = mode or off_streak >= max_off
                if enabled != mode:
                    mode = enabled
                    remaining = max(int(entity.min_consecutive_on_slots) - 1, 0)
                else:
                    remaining -= 1
            else:
                must_start_on = sum(history) < target or off_streak >= max_off
                if not must_start_on and not previous:
                    projected = history.copy()
                    projected_off = off_streak
                    first_required = None
                    for offset in range(total_steps - t):
                        if sum(projected) < target or projected_off >= max_off:
                            first_required = offset
                            break
                        if history_slots:
                            projected.append(0)
                            projected = projected[-history_slots:]
                        projected_off += 1
                    if first_required is not None:
                        slots_after_start = total_steps - (t + first_required)
                        must_start_on = slots_after_start < int(
                            entity.min_consecutive_on_slots
                        )
                enabled = must_start_on
                if previous and not enabled:
                    minimum_off = int(entity.min_consecutive_off_slots)
                    if t + minimum_off > total_steps:
                        enabled = True
                    else:
                        projected = history.copy()
                        projected_off = off_streak
                        for _ in range(minimum_off):
                            if sum(projected) < target or projected_off >= max_off:
                                enabled = True
                                break
                            if history_slots:
                                projected.append(0)
                                projected = projected[-history_slots:]
                            projected_off += 1
                        if not enabled:
                            first_required = None
                            for offset in range(minimum_off, total_steps - t):
                                if sum(projected) < target or projected_off >= max_off:
                                    first_required = offset
                                    break
                                if history_slots:
                                    projected.append(0)
                                    projected = projected[-history_slots:]
                                projected_off += 1
                            if first_required is not None:
                                slots_after_start = total_steps - (t + first_required)
                                enabled = slots_after_start < int(
                                    entity.min_consecutive_on_slots
                                )
                if enabled != previous:
                    minimum = (
                        int(entity.min_consecutive_on_slots)
                        if enabled
                        else int(entity.min_consecutive_off_slots)
                    )
                    mode = enabled
                    remaining = max(minimum - 1, 0)
            schedule[i, t] = float(enabled)
            off_streak = 0 if enabled else off_streak + 1
            if history_slots:
                history.append(int(enabled))
                history = history[-history_slots:]
            previous = enabled
    return schedule
