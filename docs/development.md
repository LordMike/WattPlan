# Development

## Repository workflow

Work from `WattPlan` as the source of truth.

## Setup

Create a test virtualenv with the Home Assistant test dependencies.

```bash
python3.14 -m venv .venv-test
source .venv-test/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .[test]
```

Use Python 3.14.2 or newer. The current pinned test stack resolves to
`homeassistant==2026.3.1`, which requires that patch level.

Run tests directly from this repo:

```bash
PYTHONPATH=src pytest tests
```

## Testing

Run the full suite from the repo:

```bash
PYTHONPATH=src pytest
```

Run only optimizer tests:

```bash
PYTHONPATH=src pytest tests/optimizer
```

Run only integration tests from the standalone virtualenv:

```bash
PYTHONPATH=src pytest tests/integration
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
