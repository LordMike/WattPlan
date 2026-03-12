# Development

## Repository workflow

Work from `WattPlan` as the source of truth.

## Setup

Create a local virtualenv with the test dependencies.

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-test.txt
```

Use Python 3.14.2 or newer. The current pinned test stack resolves to
`homeassistant==2026.3.1`, which requires that patch level.

Run tests directly from this repo:

```bash
python -m pytest tests
```

## Testing

Run the full suite from the repo:

```bash
./scripts/run_tests.sh
```

Run only optimizer tests:

```bash
./scripts/run_tests.sh tests/optimizer
```

Run only integration tests:

```bash
./scripts/run_tests.sh tests/integration
```

Run the isolated hello-world Home Assistant smoke test:

```bash
./sandbox/ha_hello_world/run.sh
```

This smoke test is separate from the main `tests/` suite. It disables pytest
plugin autoload, blocks `pycares`/`aiodns` imports inside the smoke test, and
runs from `/tmp` so it is less sensitive to WSL and Windows-mounted paths.

If you want a dedicated local venv for just that smoke test, create
`sandbox/ha_hello_world/.venv` and point the runner at it with
`WATTPLAN_HA_VENV`.

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
