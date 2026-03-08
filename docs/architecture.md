# Architecture

WattPlan is a single repository with two tightly related concerns:

- the Home Assistant custom integration in `src/custom_components/wattplan/`
- the optimizer implementation in `src/custom_components/wattplan/optimizer/`

The repository is structured so the integration can be released as a normal HACS artifact while the optimizer stays co-located and versioned with the integration.

## Layout

- `src/custom_components/wattplan/`
  - Home Assistant entry points, config flow, coordinator, entities, source handling, repairs
- `src/custom_components/wattplan/optimizer/`
  - pure Python optimization models and solver code
- `tests/integration/`
  - Home Assistant integration tests
- `tests/optimizer/`
  - optimizer-only tests that do not need Home Assistant runtime state

## Runtime model

The main runtime center is the coordinator:

- `config_flow.py`
  - collects source configuration and planner settings
- `coordinator.py`
  - builds planner input, runs planning, tracks stage errors, and updates runtime entities
- `binary_sensor.py` / `sensor.py`
  - expose planning state, diagnostics, and error scopes
- `source_pipeline.py`, `source_provider.py`, `source_fixup.py`
  - resolve raw source data and normalize it into planner-ready values

## Source handling

WattPlan resolves three main source groups:

- price
- usage
- PV

Each source can use different modes depending on the integration path. The source pipeline is responsible for:

- creating the correct provider
- validating data shape and entity compatibility
- applying fixup/reuse behavior where supported
- distinguishing fatal planning failures from degraded-but-usable input states

That distinction matters for PV in particular: some PV failures degrade planning quality without stopping the planner entirely.

## Optimizer boundary

The optimizer package is intentionally kept free of `homeassistant` imports. The integration translates Home Assistant state into optimizer inputs, and translates optimizer results back into entities, services, and diagnostics.

That boundary is the main extraction seam if the optimizer is ever split into its own package later.
