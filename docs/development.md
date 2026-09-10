# Development

## Repository workflow

Work from `WattPlan` as the source of truth.
The repository now uses the HACS-standard layout, so the integration lives at `custom_components/wattplan/` at the repo root and tests import `custom_components` directly from the project root.

## Setup

Install `uv`, then create a local virtualenv with the test
dependencies.

```bash
uv venv --python python3.14 .venv
. .venv/bin/activate
uv pip install --python .venv/bin/python '.[test]'
```

Use Python 3.14.2 or newer. The pinned Home Assistant test stack currently
requires that patch level.

On systems where the repo lives on a mounted or network-backed filesystem, a
repo-local `.venv` may be less reliable with `uv` than a virtualenv created on
the native local filesystem. If that affects your setup, create the venv in
your preferred local location and point the test wrapper at it.

Run tests directly from this repo:

```bash
python -m pytest
```

The default collection covers the full `tests/` tree, including packaging,
integration, and optimizer tests.

## Testing

Run the full suite from the repo:

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
