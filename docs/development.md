# Development

## Repository workflow

Work from `WattPlan` as the source of truth.
The repository now uses the HACS-standard layout, so the integration lives at `custom_components/wattplan/` at the repo root and tests import `custom_components` directly from the project root.

## Setup

Run Home Assistant integration tests under Linux (WSL works on Windows): the
Windows Python runtime cannot load all of Home Assistant's Unix dependencies.
On Windows, enter your WSL Linux shell first (`wsl -d <distribution>`), then
`cd` to the repository (often under `/mnt/<drive>/`); do not use Git Bash for
Home Assistant tests. Install `uv`, then work from the WattPlan repository
root. The test wrapper uses a **predictable, worktree-specific venv under
`/tmp`**, not a repo-local `.venv`. Keep the venv on Linux's native filesystem
even when the repository is mounted under `/mnt` or on a network drive:

```bash
worktree_key="$(printf '%s' "$PWD" | cksum | awk '{print $1}')"
venv="/tmp/wattplan-venv-$worktree_key"
if [ ! -x "$venv/bin/python" ]; then
  uv venv --python 3.14 "$venv"
fi
uv pip install --python "$venv/bin/python" -r requirements-test.txt
./scripts/run_tests.sh
```

Use Python 3.14.2 or newer. The pinned Home Assistant test stack currently
requires that patch level. The `/tmp` path is predictable for the same
worktree, but it is temporary: recreate the venv when `/tmp` is cleared, then
rerun `uv pip install` after requirements change. If an existing venv has an
outdated or broken interpreter, use `uv venv --clear --python 3.14 "$venv"`
to replace that *specific* venv, then reinstall the requirements. On WSL, do
setup and testing in the same Linux invocation if it may stop and clear `/tmp`
between commands. If the package index is unavailable and the required wheels
are already cached, add `--offline` to the `uv pip install` command.

`./scripts/run_tests.sh` checks for a working venv and pytest and prints the
exact setup commands if either is missing. To use a different Linux-native
location, set `venv=/path/to/venv` before the installation commands above and
pass `WATTPLAN_TEST_VENV="$venv"` to the wrapper. No `hass-core` checkout or
symlink is required for these in-repo tests.

## Testing

Run the full suite from the repo after setup:

```bash
./scripts/run_tests.sh
```

If your virtualenv is not at the wrapper's default location, point the wrapper
at it:

```bash
WATTPLAN_TEST_VENV=/path/to/venv ./scripts/run_tests.sh
```

Run only optimizer tests:

```bash
./scripts/run_tests.sh tests/optimizer
```

Run only integration tests:

```bash
./scripts/run_tests.sh tests/integration
```

## Plan Outlook translations

Plan Outlook prose is stored in the checked-in Project Fluent catalogues at
`custom_components/wattplan/locales/en/plan_outlook.ftl` and
`custom_components/wattplan/locales/da/plan_outlook.ftl`. Runtime code owns
fact selection, typed arguments, and deterministic variant selection; each
catalogue owns complete localized grammar, punctuation, duration plurals, and
source-specific wording. Keep the two catalogues in semantic parity when adding
or changing a branch, including each declared variant count.

`fluent.runtime==0.4.0` is a runtime integration dependency and must remain in
the manifest, project dependency list, and test requirements (a test checks
that the three agree). `numpy` and `highspy` are bounded to the current major
because optimizer tests assert exact plans; raise the bound deliberately after
checking the tests. `requirements-test.txt` is the only install path, used by CI
and `run_tests.sh` alike. Run the Outlook
tests after catalog edits; they reject Fluent `Junk`, missing semantic messages,
formatting failures, and uncached per-render catalog reads:

```bash
./scripts/run_tests.sh tests/test_plan_outlook.py tests/test_plan_outlook_danish.py
```

## Optimizer benchmarks

Session 1 fixtures cover the asset shapes that should remain cheap as planner
behavior evolves:

- `no-assets-no-pv`
- `no-battery-comfort-no-pv`
- `no-battery-comfort-pv`
- `battery-zero-pv`
- `battery-zero-pv-signed-target`
- `charge-only-battery`
- `mixed-batteries-pv`
- `comfort-flexible`
- `comfort-tight`

The fixtures live in `tests/optimizer/benchmark_cases.py`. They are synthetic,
and `comfort-tight` is explicitly labeled as a synthetic historical substitute
because repository history contains no recoverable named slow-comfort input.
The existing `low-pv` and `live-export` scenarios remain the recovered captured
Home Assistant inputs.

Measure one full-plan distribution and repeated five-tick prefix trajectories:

```bash
python scripts/benchmark_optimizer.py --scenario mixed-batteries-pv --slots 96 --lookahead 48 --repeats 3 --serial
```

Run the Session 1 matrix sequentially. Do not parallelize these processes; CPU
and thermal contention invalidates comparisons:

```bash
for scenario in no-assets-no-pv no-battery-comfort-no-pv no-battery-comfort-pv battery-zero-pv charge-only-battery mixed-batteries-pv comfort-flexible comfort-tight; do
  python scripts/benchmark_optimizer.py --scenario "$scenario" --slots 96 --lookahead 48 --repeats 3 --serial
done
```

The JSON report records revision/dirty state, platform, Python, NumPy, HiGHS,
command settings, provenance, quality checks, and these timing layers:

- End-to-end input validation through serialized planner response.
- Input validation and explicit full-plan normalization.
- Planner time.
- `_solve_mpc_step` time, native `_solve_lp` wrapper time, and HiGHS native time.
- Model construction plus solve-result extraction, measured as step time outside
  `_solve_lp`; production code is not changed solely to split those two pieces.
- Planner time outside solver steps.
- Explicit primary/probe native call counts and maximum model dimensions.

Reports safely represent zero native calls with zero counts and zero model
dimensions. Use `--include-call-details` only when per-call horizons, roles, and
sizes are needed; aggregate output is the default to keep reports manageable.
The three no-battery fixture groups should report `total_calls: 0` after the
direct-flow bypass. Treat a nonzero count there as a regression even when the
wall-clock result remains small.

Use `preserve-probe` when the measurement must exercise counterfactual solves:

```bash
python scripts/benchmark_optimizer.py --scenario preserve-probe --slots 24 --lookahead 8 --repeats 15
```

The `live-export` scenario uses another captured Home Assistant forecast. The
deterministic `stress` scenario adds three heterogeneous batteries, deadbands,
signed tariffs, nonlinear power curves, and targets; increase its workload
explicitly when solver timing is too short to distinguish:

```bash
python scripts/benchmark_optimizer.py --scenario stress --slots 288 --lookahead 96 --repeats 1
```

Use the same Python environment and machine for paired comparisons. Report the
full sample list, not only the median, and do not extrapolate x86 timings to ARM.
Pass `--disable-mip-starts` to measure the cold solver path under the identical
scenario, full/prefix cadence, and forecast workload.

Production submits MIP starts only when the effective initial lookahead is at
least 40 slots. Below that boundary, `--compare-mip-starts` intentionally
compares two cold executions so the benchmark reflects deployed behavior. The
historical forced warm/cold eligibility matrix and its exact pre-gate revision
are recorded in the findings document.

See [Optimizer benchmark findings](optimizer-benchmarks.md) for the recorded
September 2026 measurements and rejected experiments.

## Packaging

Build a local HACS artifact:

```bash
python scripts/build_hacs_zip.py
```

Build with an explicit label:

```bash
python scripts/build_hacs_zip.py --version-label local-dev
```

Artifacts are written to `dist/`.

## Practical rules

- Keep the optimizer pure; do not add `homeassistant` imports under `optimizer/`.
- If integration behavior changes, update integration tests in the same change.
- If workflows or release behavior change, update `README.md`, `docs/release.md`, and `AGENTS.md`.
