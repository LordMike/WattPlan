# WattPlan ![CI](https://github.com/LordMike/WattPlan/actions/workflows/ci.yml/badge.svg) ![License](https://img.shields.io/github/license/LordMike/WattPlan)

![WattPlan logo](docs/pictures/logo.png)

WattPlan is a Home Assistant custom integration for planning energy use around price, usage, PV production, and battery storage. It is built for users who want the integration and the optimizer source to live together in one repository, with a release flow that produces HACS-ready artifacts.

The repository contains the integration under `src/custom_components/wattplan`, the internal optimizer under `src/custom_components/wattplan/optimizer`, and both Home Assistant-facing and optimizer-only tests. End users install WattPlan through HACS using this GitHub repository as a custom integration source.

## Quickstart
1. In HACS, open the menu in the top-right and choose `Custom repositories`.
2. Add `https://github.com/LordMike/WattPlan` with type `Integration`.
3. Search for `WattPlan` in HACS and install it.
4. Restart Home Assistant.
5. Go to `Settings` -> `Devices & Services` -> `Add Integration`, then add `WattPlan`.
6. Configure a price source and a load/usage source.
7. Configure a PV source if you have solar and want WattPlan to plan around it.
8. Add batteries, comfort loads, or optional loads if you want WattPlan to control more than forecasting.

## Features
- Home Assistant custom integration with HACS-ready release artifacts
- Internal optimizer kept in-repo, but isolated from Home Assistant imports
- Config-flow driven source setup for price, usage, and PV inputs
- Battery, comfort-load, and optional-load planning
- GitHub Actions for CI, tagged releases, and prereleases

## Documentation
- [docs/source-data.md](docs/source-data.md) — source modes, data model, and how to feed WattPlan price, usage, and PV data
- [docs/development.md](docs/development.md) — local setup, symlink workflow, tests, packaging
- [docs/architecture.md](docs/architecture.md) — code layout, runtime boundaries, planning flow
- [docs/release.md](docs/release.md) — tags, prereleases, GitHub release artifacts
- [docs/optimizer-api.md](docs/optimizer-api.md) — direct optimizer API notes

## Status
I make open source software in my free time and share it so others can use it, learn from it, or build on it. The project is provided as-is with no guarantees about quality, correctness, or support. Issues and pull requests are welcome, but there is no guarantee of response time, fixes, or ongoing maintenance.

[Project license](LICENSE.txt).
