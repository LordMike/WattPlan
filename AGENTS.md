# WattPlan Repository Guide

This repository is the source of truth for the WattPlan Home Assistant custom
integration.

## Intent

WattPlan combines two concerns in one repository:

- The Home Assistant integration under `src/custom_components/wattplan/`
- The internal optimizer library under `src/custom_components/wattplan/optimizer/`

The optimizer is intentionally kept as a pure Python subpackage with no Home
Assistant dependencies so it can be extracted later if that becomes useful.

## Repository Layout

- `src/custom_components/wattplan/`
  - The integration code that will be packaged for HACS releases
- `src/custom_components/wattplan/optimizer/`
  - Internal optimization engine and models
- `tests/integration/`
  - Home Assistant-facing tests
- `tests/optimizer/`
  - Pure optimizer tests
- `docs/`
  - Project and release documentation
- `scripts/`
  - Utility scripts, including HACS artifact packaging

## Development Workflow

Primary development should happen from this repository.

For live Home Assistant development, symlink the integration into a
`hass-core` checkout:

```bash
ln -s /mnt/n/Personal/WattPlan/src/custom_components/wattplan \
  /mnt/n/Personal/hass-core/config/custom_components/wattplan
```

If a real directory already exists at the target path, move or remove it first.
Do not blindly overwrite user changes in `hass-core`.

## Commands

### Build HACS Artifact

Build a local release zip:

```bash
python scripts/build_hacs_zip.py
```

Build with an explicit label:

```bash
python scripts/build_hacs_zip.py --version-label local-dev
```

The generated artifact is written to `dist/`.

### Optimizer Tests

Run only the pure optimizer suite:

```bash
PYTHONPATH=src /mnt/n/Personal/hass-core/.venv/bin/pytest tests/optimizer
```

### Integration Tests

The integration tests depend on the Home Assistant test harness. The most
reliable way to run them is from a prepared `hass-core` environment:

```bash
PYTHONPATH=/mnt/n/Personal/WattPlan/src \
  /mnt/n/Personal/hass-core/.venv/bin/pytest \
  /mnt/n/Personal/WattPlan/tests/integration
```

If the repo-local test harness is expanded later, update this file.

### Full Test Suite

Run everything using the `hass-core` virtualenv:

```bash
PYTHONPATH=/mnt/n/Personal/WattPlan/src \
  /mnt/n/Personal/hass-core/.venv/bin/pytest \
  /mnt/n/Personal/WattPlan/tests
```

## GitHub Actions Intent

- `CI`
  - Run tests on pushes and pull requests
  - Build a zip artifact for `main`
- `Release`
  - Build a HACS-ready zip on version tags
  - Publish GitHub releases
  - Mark tags containing `-` as prereleases

## Documentation Maintenance

The documentation set is intentionally small at the start. Keep it current as
behavior stabilizes.

Expected docs:

- `docs/architecture.md`
  - Placeholder: describe integration boundaries, optimizer boundaries, data
    flow, and major runtime concepts
- `docs/development.md`
  - Placeholder: describe local setup, symlink workflow, test workflow, and
    packaging workflow
- `docs/release.md`
  - Placeholder: describe versioning, tag format, prereleases, and release
    artifact expectations
- `docs/optimizer-api.md`
  - Keep aligned with the optimizer input/output model when those interfaces
    change

When changing behavior, update docs in the same change if the behavior is
user-facing, architectural, or operational.

## Constraints

- Keep `optimizer/` free of `homeassistant` imports.
- Keep HACS packaging focused on `src/custom_components/wattplan/`.
- Prefer updating tests alongside behavior changes.
- Do not assume the `hass-core` checkout is clean; check before making symlink
  or copy-based changes.
