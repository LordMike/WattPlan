# Development

## Repository workflow

Work from `WattPlan` as the source of truth.

The local `hass-core` checkout is used as:

- a Home Assistant runtime
- a prepared virtualenv for integration tests

The current development setup uses symlinks:

- `../hass-core/config/custom_components/wattplan` -> `src/custom_components/wattplan`
- `../hass-core/tests/custom_components/wattplan` -> `tests/integration`

There are backup directories in `hass-core` from before the symlink switch. Do not edit those as part of normal work.

## Setup

Install repo dependencies:

```bash
python -m pip install -e .[test]
```

For Home Assistant-oriented test runs, the prepared `hass-core` virtualenv is also valid:

```bash
PYTHONPATH=src \
  ../hass-core/.venv/bin/pytest \
  tests
```

## Testing

Run the full suite from the repo:

```bash
pytest
```

Run only optimizer tests:

```bash
pytest tests/optimizer
```

Run only integration tests through the `hass-core` virtualenv:

```bash
PYTHONPATH=src \
  ../hass-core/.venv/bin/pytest \
  tests/integration
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
