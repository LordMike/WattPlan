# Architecture

WattPlan is a single repository with two tightly related concerns:
- The Home Assistant custom integration in `src/custom_components/wattplan/`
- The optimizer implementation in `src/custom_components/wattplan/optimizer/`

The repository is structured so the integration can be released as a normal HACS artifact while the optimizer stays co-located and versioned with the integration.

## Layout
- `src/custom_components/wattplan/`
  - Home Assistant entry points, config flow, coordinator, entities, source handling, repairs
- `src/custom_components/wattplan/optimizer/`
  - Pure Python optimization models and solver code
- `tests/integration/`
  - Home Assistant integration tests
- `tests/optimizer/`
  - Optimizer-only tests that do not need Home Assistant runtime state

## Runtime Model
The main runtime center is the coordinator:
- `config_flow.py`: Collects source configuration and planner settings.
- `coordinator.py`: Builds planner input, runs planning, tracks stage errors, and updates runtime entities.
- `binary_sensor.py` / `sensor.py`: Expose planning state, diagnostics, and error scopes.
- `source_pipeline.py`, `source_provider.py`, `source_fixup.py`: Resolve raw source data and normalize it into planner-ready values.

## Source Handling
WattPlan resolves three main source groups:
- **Price**
- **Usage**
- **PV**

Each source can use different modes depending on the integration path. The source pipeline is responsible for:
- Creating the correct provider
- Validating data shape and entity compatibility
- Applying fixup/reuse behavior where supported
- Distinguishing fatal planning failures from degraded-but-usable input states

That distinction matters for PV in particular: some PV failures degrade planning quality without stopping the planner entirely.

## Optimizer Boundary
The optimizer package is intentionally kept free of `homeassistant` imports. The integration translates Home Assistant state into optimizer inputs and translates optimizer results back into entities, services, and diagnostics.

That boundary is the main extraction seam if the optimizer is ever split into its own package later.

## Integration Boundaries
The integration and optimizer interact through a defined boundary, ensuring that the optimizer operates independently of Home Assistant's runtime. This separation allows for easier testing and potential future enhancements, such as splitting the optimizer into its own package. The integration handles the translation of data between Home Assistant and the optimizer, ensuring that both components can evolve without tightly coupling their implementations.