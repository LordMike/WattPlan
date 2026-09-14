"""Independent tests for bounded comfort schedule placement."""

from __future__ import annotations

from itertools import product

import pytest

from custom_components.wattplan.optimizer.comfort_placement import (
    ComfortPlacementInput,
    check_comfort_schedule,
    place_comfort_schedules,
)


def _comfort(
    schedule,
    *,
    name="comfort",
    history=None,
    window=None,
    target=1,
    min_on=1,
    min_off=1,
    max_off=None,
    current=True,
    off_streak=0,
    lock_remaining=0,
    lock_mode=None,
):
    schedule = tuple(schedule)
    window = window or len(schedule)
    history = tuple([True] * (window - 1) if history is None else history)
    return ComfortPlacementInput(
        name=name,
        schedule=schedule,
        on_history=history,
        rolling_window_slots=window,
        target_on_slots_per_rolling_window=target,
        min_consecutive_on_slots=min_on,
        min_consecutive_off_slots=min_off,
        max_consecutive_off_slots=max_off or max(len(schedule), min_off),
        is_on_now=current,
        off_streak_slots_now=off_streak,
        lock_remaining=lock_remaining,
        lock_mode=lock_mode,
    )


def _direct_cost(*, powers, usage, solar, import_prices, export_prices):
    def evaluate(schedules):
        total = 0.0
        for slot in range(len(usage)):
            demand = usage[slot] + sum(
                power * float(schedule[slot])
                for power, schedule in zip(powers, schedules, strict=True)
            )
            net = demand - solar[slot]
            total += max(net, 0.0) * import_prices[slot]
            total -= max(-net, 0.0) * export_prices[slot]
        return total

    return evaluate


def _oracle_valid(comfort, schedule):
    horizon = len(schedule)
    lock_mode = comfort.is_on_now if comfort.lock_mode is None else comfort.lock_mode
    if any(
        schedule[slot] != lock_mode
        for slot in range(min(comfort.lock_remaining, horizon))
    ):
        return False

    previous = comfort.is_on_now
    for slot, enabled in enumerate(schedule):
        if enabled == previous:
            continue
        minimum = (
            comfort.min_consecutive_on_slots
            if enabled
            else comfort.min_consecutive_off_slots
        )
        if slot + minimum > horizon:
            return False
        if any(value != enabled for value in schedule[slot : slot + minimum]):
            return False
        previous = enabled

    off_streak = 0 if comfort.is_on_now else comfort.off_streak_slots_now
    for enabled in schedule:
        off_streak = 0 if enabled else off_streak + 1
        if off_streak > comfort.max_consecutive_off_slots:
            return False

    combined = comfort.on_history + tuple(schedule)
    return all(
        sum(combined[slot : slot + comfort.rolling_window_slots])
        >= comfort.target_on_slots_per_rolling_window
        for slot in range(horizon)
    )


def test_checker_matches_exhaustive_short_horizon_oracle():
    cases = [
        _comfort([True, False, False], history=[False, True], window=3),
        _comfort(
            [False, True, True, False],
            history=[True, False, False],
            window=4,
            target=2,
            min_on=2,
            max_off=3,
            current=False,
            off_streak=2,
        ),
        _comfort(
            [False, False, True, True],
            history=[True, True, False],
            window=4,
            min_on=2,
            min_off=2,
            max_off=3,
            current=False,
            off_streak=1,
            lock_remaining=1,
            lock_mode=False,
        ),
        _comfort(
            [True, True, False, False, False],
            history=[False, False],
            window=3,
            target=1,
            min_on=2,
            min_off=2,
            max_off=3,
            current=True,
            lock_remaining=1,
        ),
    ]
    for comfort in cases:
        for bits in product((False, True), repeat=len(comfort.schedule)):
            assert (not check_comfort_schedule(comfort, bits)) == _oracle_valid(
                comfort, bits
            )


def test_infeasible_baseline_is_preserved_without_costing():
    comfort = _comfort(
        [False, False, True, True],
        history=[True] * 9,
        window=10,
        min_on=3,
        min_off=2,
        max_off=2,
        current=False,
        off_streak=0,
        lock_remaining=2,
        lock_mode=False,
    )

    def unexpected(_schedules):
        raise AssertionError("infeasible schedules must not be priced")

    result = place_comfort_schedules([comfort], unexpected)

    assert result.status == "infeasible"
    assert result.schedules == (comfort.schedule,)
    assert {violation.code for violation in result.violations[0]} == {
        "terminal_min_on"
    }
    assert result.candidates_considered == result.candidates_evaluated == 0


def test_checker_reports_each_independent_max_off_violation():
    comfort = _comfort(
        [False, False, True, False, False],
        history=[True, True, True, True],
        window=5,
        max_off=1,
        current=True,
    )

    violations = check_comfort_schedule(comfort)

    assert [
        violation.slot for violation in violations if violation.code == "max_off"
    ] == [1, 4]


def test_no_comfort_and_no_flexibility_skip_cost_callback():
    calls = 0

    def count(_schedules):
        nonlocal calls
        calls += 1
        return 0.0

    empty = place_comfort_schedules([], count)
    locked = _comfort(
        [True, True, True],
        lock_remaining=3,
        lock_mode=True,
    )
    fixed = place_comfort_schedules([locked], count)

    assert empty.status == "no_comfort"
    assert fixed.status == "no_flexibility"
    assert calls == 0


def test_tight_lock_and_deadline_reject_every_local_move():
    comfort = _comfort(
        [False, True, True],
        history=[True, False],
        window=3,
        min_on=2,
        max_off=2,
        current=False,
        off_streak=1,
        lock_remaining=1,
        lock_mode=False,
    )
    calls = 0

    def count(_schedules):
        nonlocal calls
        calls += 1
        return 0.0

    result = place_comfort_schedules([comfort], count)

    assert result.status == "no_flexibility"
    assert result.candidates_considered == result.candidates_rejected == 1
    assert result.candidates_evaluated == calls == 0


def test_signed_tariffs_use_straightforward_net_site_accounting():
    comfort = _comfort([False, True])
    evaluate = _direct_cost(
        powers=[1.0],
        usage=[0.0, 0.0],
        solar=[1.0, 0.0],
        import_prices=[5.0, -2.0],
        export_prices=[-3.0, 0.0],
    )

    result = place_comfort_schedules([comfort], evaluate)

    assert result.status == "improved"
    assert result.schedules == ((True, False),)
    assert result.baseline_cost == pytest.approx(1.0)
    assert result.final_cost == pytest.approx(0.0)
    assert result.optimality == "heuristic"


def test_multiple_comforts_are_priced_against_previously_accepted_schedules():
    comforts = [
        _comfort([True, False, False, False], name="first"),
        _comfort([False, False, False, True], name="second"),
    ]
    evaluate = _direct_cost(
        powers=[1.0, 1.0],
        usage=[0.0] * 4,
        solar=[0.0, 1.0, 0.0, 0.0],
        import_prices=[10.0, 10.0, 1.0, 10.0],
        export_prices=[0.0] * 4,
    )

    result = place_comfort_schedules(comforts, evaluate)

    assert result.status == "improved"
    assert result.schedules == (
        (False, True, False, False),
        (False, False, True, False),
    )
    assert result.baseline_cost == pytest.approx(20.0)
    assert result.final_cost == pytest.approx(1.0)
    assert result.accepted_moves == 2


def test_single_run_quality_matches_exhaustive_same_runtime_oracle():
    horizon = 4
    for prices in product((-2.0, 0.0, 3.0), repeat=horizon):
        for initial_slot in range(horizon):
            baseline = tuple(slot == initial_slot for slot in range(horizon))
            comfort = _comfort(baseline)
            evaluate = _direct_cost(
                powers=[1.0],
                usage=[0.0] * horizon,
                solar=[0.0] * horizon,
                import_prices=prices,
                export_prices=[0.0] * horizon,
            )

            result = place_comfort_schedules([comfort], evaluate)
            feasible_same_runtime = [
                bits
                for bits in product((False, True), repeat=horizon)
                if sum(bits) == 1 and _oracle_valid(comfort, bits)
            ]
            oracle_cost = min(evaluate((bits,)) for bits in feasible_same_runtime)

            assert evaluate(result.schedules) == pytest.approx(oracle_cost)


def test_candidate_and_callback_work_are_deterministically_bounded():
    horizon = 40
    comfort = _comfort((True,) + (False,) * (horizon - 1))
    calls = 0

    def flat_cost(_schedules):
        nonlocal calls
        calls += 1
        return 1.0

    result = place_comfort_schedules(
        [comfort],
        flat_cost,
        max_candidates_per_comfort=5,
        max_total_candidates=5,
    )

    assert result.status == "unchanged"
    assert result.candidates_generated == 5
    assert result.candidates_considered == result.candidates_evaluated == 5
    assert result.candidates_not_improving == 5
    assert result.candidates_unvisited == 0
    assert calls == 6


def test_cancellation_stops_between_candidates_and_keeps_valid_state():
    comfort = _comfort([True, False, False, False])
    checks = 0
    calls = 0

    def cancel():
        nonlocal checks
        checks += 1
        return checks >= 3

    def cost(schedules):
        nonlocal calls
        calls += 1
        return float(next(index for index, value in enumerate(schedules[0]) if value))

    result = place_comfort_schedules([comfort], cost, should_cancel=cancel)

    assert result.status == "cancelled"
    assert result.candidates_generated == 3
    assert result.candidates_considered == result.candidates_evaluated == 1
    assert result.candidates_not_improving == 1
    assert result.candidates_unvisited == 2
    assert calls == 2
    assert not result.violations[0]
