---
name: test-review
description: Review a WattPlan test module, a test directory, or the tests in a diff; propose which tests and lines to delete or merge, and which contracts and behaviours lack a test. Use when tests are added or changed, or when asked to trim the test suite.
---

# Test review

The goal is fewer tests, fewer lines in the tests that remain, and the right tests: the review also names the contracts and behaviours that no test guards. A test earns its place by guarding a behaviour of a public surface that no other test already guards. Large and many tests make a slow, brittle suite and add little value. The reviewer only finds and proposes; the author makes deletions after verifying them.

Code coverage is not the measure, but it is a gate: CI runs `pytest --cov` and fails below `[tool.coverage.report] fail_under` in `pyproject.toml`. Dropping tests of guard clauses on purpose is fine; never keep or add a test only to cover a line. If the proposed deletions would take coverage below the floor, say so and propose either a smaller cut or lowering the floor as a separate, explicit decision for the owner.

## Tiers

The suite has three tiers, and each rule is tested in the tier that owns it.

- **Optimizer** (`tests/optimizer/`): models, planners, physics, simulation, and the planner contract, with no Home Assistant. `planner_conformance.py` runs the public planner contract over every built-in planner; that matrix is the contract in `docs/optimizer-api.md`, not redundancy.
- **Helpers** (`tests/*.py` at the root): pure modules with a defined contract, such as Plan Outlook rendering, presenters, presets, datetime utilities, and packaging. Unit tests here surface faults early and cheaply.
- **Integration** (`tests/integration/`): the integration running in a `hass` fixture, so config and subentry flows, the source pipeline, the coordinator and runtime, entities, services, and the end-to-end scenarios in `test_integration_e2e.py`.

A test that sits between tiers, re-walking a lower tier's cases through a higher one, is the usual candidate for removal. Typical cases are an integration test that re-proves optimizer arithmetic, or a runtime test that re-enumerates provider, fixup, or normalisation cases that `test_source_provider.py`, `test_source_fixup.py`, or `test_source_normalisation.py` already hold. Integration tests should prove the wiring and the user-visible outcome once.

## Principles

1. **Test surfaces, not internals.** The surface is a public function or class, a planner through `create_planner` and its request/response models, a config or options flow step, an entity's state and attributes, a service call, or a stored/exported file format. A test that pins a private helper, a `coordinator_parts`/`coordinator_logic` step, or call order breaks on refactors that change nothing a caller sees.
2. **Setup is cost.** `MagicMock`/`patch` stacks, hand-built fake coordinators, and scripted scenarios that exist only to make one test reachable are a sign the test is at the wrong tier. Prefer real states set on `hass` and the real component over mocks; patch only at external boundaries (recorder history, the planner worker process, other integrations' entities). Setup longer than the assertion is a finding. Prefer shared helpers in `tests/common.py`, `tests/plan_invariants.py`, and the tier's `conftest.py` over per-module copies.
3. **One behaviour, one test; one scenario for a stateful component.** Two tests that fail for the same defect are one test. A stateful component (a config entry's setup/reload/unload lifecycle, history caches, target persistence, historical cost tracking) is better covered by one scenario that advances time or state and asserts at each step than by a test per method that rebuilds the same entry. Keep separate only the cases that need a different starting state, such as a failing source or corrupt stored data.
4. **Chosen cases, not matrices.** A positive case, a negative case, and the edge cases that matter. `parametrize` grids, per-branch tests, and enumerated inputs go unless each row guards a different defect. The planner conformance matrix and the en/da language parity in the Plan Outlook tests are contracts and stay (principle 11).
5. **Test the contract, not input validation.** For optimizer models and planner options, assume the `ValueError` checks work: keep one test per model or boundary proving invalid input is refused, and delete per-field rows (e.g. `-1`, `nan`, `inf`, `True` for each field) and exact-message assertions. Config and subentry flows are different: the error key a user sees is behaviour, so keep one test per distinct error the UI can show, but not one per input that produces the same error. Test what the contract promises for valid input: plans, costs, ordering, limits, entity states, and stored formats.
6. **No framework or library tests.** Do not test voluptuous, dataclass equality, Home Assistant core behaviour (entity registry, restore state mechanics, flow manager plumbing), Fluent itself, numpy, or HiGHS, and do not test a trivial getter, pass-through, constant, or option default.
7. **Do not re-prove the producer.** A test that checks what the code under test established a line earlier, or what a lower tier's own tests already cover, duplicates that tier. Optimizer rules live in `tests/optimizer/`; source acquisition rules live with `source_pipeline.py`, `source_provider.py`, and `source_fixup.py`.
8. **Exceptions only for exceptional states.** `pytest.raises` is right when the state is exceptional. Use `match=` only for a fragment that identifies the case, never for the full message text.
9. **Fast and deterministic.** No real sleeps, network, or wall clock. Frozen time (`freezer`, `frozen_clock`) and `async_fire_time_changed` are the right tools, as is a short `await asyncio.sleep(0)` or `hass.async_block_till_done()` to drain the loop. A test that routinely takes about three seconds or more is a finding: name what makes it slow (a real planner search, a long horizon, the worker process) and propose a smaller input.
10. **Surviving tests stay small.** The name states the behaviour. Arrange only what the assertion needs, assert the one outcome, and avoid assertions on incidental attributes or whole-dict snapshots when one field is the point.
11. **Specification tests are not redundant.** A test that guards a documented contract or a user-visible behaviour is kept even when its code is simple. Say which document owns it: `docs/optimizer-api.md`, `docs/entities-and-services.md`, `docs/source-data.md`, `docs/error-handling.md`, `docs/historical-cost-tracking.md`, `docs/planner-diagnostics.md`, `docs/simulator.md`, or the translation catalogues and `strings.json`. Packaging and HACS checks (`test_build_hacs_zip.py`, `test_packaging_metadata.py`) guard the release artifact and are kept.

## Procedure

1. Scope: the test module(s), the directory, or the diff's test files. Read the code under test only as far as needed to judge which surface each test exercises.
2. For each module, note per test: the surface it exercises, the behaviour it guards, its size, and its setup cost.
3. Classify each test as `keep`, `trim` (same guard, fewer lines or less setup), `merge` (into a named test), or `delete` (name the test or tier that still covers the behaviour, or the principle that makes it worthless). A deletion needs a reason a reader can check; "looks redundant" is not one. When unsure, `keep`.
4. Verify before proposing a deletion: grep for the other test that covers the behaviour, or for evidence that an end-to-end test exercises the path. Coverage may be checked with `./scripts/run_tests.sh --cov --cov-report=term-missing <paths>`; do not add tooling for it.
5. Find gaps. For each public surface in scope (helper modules with a contract, a planner's contract, a config entry's lifecycle, each flow step, each entity and service, each documented behaviour in `docs/`), check that something guards its main behaviour, one failure that matters, and the edge cases that matter. A gap is a behaviour a caller, user, or document relies on that no test would notice breaking. Do not report missing guard-clause or validation tests, or uncovered lines for their own sake. Name the behaviour, the tier that should hold it, and the smallest test that would guard it. Prefer extending an existing scenario test to adding a new one.
6. Report findings only, per module: path; test count (counting `parametrize` rows, with the length of any parametrized collection such as `PRESETS` or `BUILTINS` resolved by importing it, not guessed) and line count; `delete` / `merge` / `trim` entries with the reason; the estimated tests and lines removed; what coverage is lost, if any; then the `add` entries for gaps (behaviour, tier, smallest test). Close with totals for the scope and whether the coverage floor is at risk.

## Applying

The author makes the deletions in the same change, then runs the full suite with coverage (`./scripts/run_tests.sh --cov` from Linux/WSL, as described in `docs/development.md`) and confirms it stays above the floor. The commit or pull request states how many tests and lines were removed and what, if anything, now lacks coverage. Removal that would lose a documented contract goes to the owner as a question and is not made.
