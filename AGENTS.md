## Purpose
WattPlan is the source of truth for the WattPlan Home Assistant custom integration and its in-repo optimizer.
Optimize for clean integration behavior, reliable Home Assistant tests, and release artifacts that stay HACS-compatible.
Prefer updating the integration, tests, and docs together when behavior changes.

## Do / Don't
- Do: treat `WattPlan` as the canonical repo and `hass-core` as the runtime/test harness via symlink.
- Do: keep `src/custom_components/wattplan/optimizer/` free of `homeassistant` imports.
- Do: update docs when workflows, release behavior, or architecture changes.
- Don't: edit the backup copies under `hass-core/*/wattplan.pre-symlink-backup-*`.
- Don't: reintroduce `vendor_poweroptim`; the optimizer lives under `optimizer/`.
- Don't: assume `hass-core` is clean before changing symlinks or running migrations.

## Pushback / quality bar
- Before starting new projects or automation, evaluate whether the effort is justified and push back if build time exceeds the time it would save.
- If a request would introduce hacks, unclear behavior, or long-term maintenance risk, push back and propose a safer alternative.
- Avoid obvious performance pitfalls; call them out and offer a better approach.
- Prefer clear, simple code over clever or verbose implementations.

## Core workflows
- Build: `python scripts/build_hacs_zip.py`
- Test: `pytest`
- Run: `PYTHONPATH=src pytest tests` or `PYTHONPATH=src ../hass-core/.venv/bin/pytest tests`

## Repo conventions
- Integration code lives in `src/custom_components/wattplan/`.
- Optimizer code lives in `src/custom_components/wattplan/optimizer/`.
- Home Assistant integration tests live in `tests/integration/`.
- Optimizer-only tests live in `tests/optimizer/`.
- `hass-core/config/custom_components/wattplan` and `hass-core/tests/custom_components/wattplan` are symlinks into this repo.
- Keep release packaging focused on the integration tree under `src/custom_components/wattplan/`.

## Documentation upkeep
- `README.md` — keep quickstart, release flow, and repo purpose aligned with the current structure
- `docs/development.md` — update when local workflow, symlink setup, or test commands change
- `docs/architecture.md` — update when code boundaries or planning/runtime flow changes
- `docs/release.md` — update when tag, prerelease, or artifact behavior changes
- `docs/optimizer-api.md` — update when optimizer import paths or request/response models change

## When to split
If this file grows beyond a page, or if the repo has distinct task areas (for example docs/release vs integration/runtime), ask whether to split into `AGENTS.<TASK>.md` files.
