# Planner Diagnostics and History

WattPlan provides a compact, reproducible record for investigating an
unexpected plan without recording the large Plan Details arrays.

## Planner Reproduction sensor

Each WattPlan setup has a **WattPlan Planner Reproduction** sensor. It is
disabled by default. Enable it from `Settings` -> `Devices & services` ->
`Entities`, then select the WattPlan Planner Reproduction entity for the setup
you want to investigate.

Once enabled, the sensor attempts to emit one compressed, self-contained, versioned replay
snapshot for every newly produced plan. Its `payload` attribute uses the
`wattplan-msgpack-v1+lzma+base85` format. The snapshot contains the information
needed to replay that plan, including its effective planner inputs, settings, and result. Treat
it as diagnostic data when sharing it, because it can contain details about
your energy setup and forecasts.

Enabling the entity does not recreate a snapshot for an existing plan. Enable
it before producing the plan you want to investigate, then run or wait for a
new planning cycle.

## Recorder behavior and storage

When the Planner Reproduction sensor is enabled, Home Assistant Recorder can
store its compact attributes. Recording is still subject to Home Assistant's
16 KiB attribute limit, your Recorder inclusion/exclusion configuration, and
its retention policy. If the compressed replay blob is too large, WattPlan
reports an explicit error instead of emitting a partial snapshot.

The existing **Plan Details** entity remains disabled by default. Its large
array attributes are excluded from Recorder history; use the Planner
Reproduction sensor for replayable diagnostic history instead. The Plan Details
and hourly Plan Details entities remain current inspection views rather than a
source of recorded replay data.

Recorder storage and retention remain your responsibility. Enable the Planner
Reproduction sensor only for the investigation period you need, ensure Recorder
does not exclude it, and account for the retained diagnostic records in your
database capacity planning.

## Decode a replay snapshot

Use the included decoder to open the `payload` attribute copied from the
sensor. The decoder uses the WattPlan installation (including its MessagePack
dependency) and does not execute archived values:

```python
from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer.reproduction_codec import decode_reproduction

payload = "..."  # Value of the sensor's payload attribute
snapshot = decode_reproduction(payload)
replayed = optimize(OptimizationParams(**snapshot["request"]["optimizer_params"]))

print(replayed["entities"] == snapshot["result"]["entities"])
```

The version contained in the decoded snapshot identifies its replay format.
Keep the complete payload intact when collecting diagnostics.
