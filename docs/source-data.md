# Source Data

WattPlan plans around three source groups:

- price
- usage
- PV

Price and usage are the core planner inputs. PV is optional, but it is what allows WattPlan to recognize solar surplus and plan around self-consumption or charging opportunities.

The source model is designed so users can get data into WattPlan in several different ways instead of forcing one specific upstream integration.

## What WattPlan needs

At planning time, WattPlan needs one numeric value per time slot for each configured source:

- price: forecasted price per kWh
- usage: forecasted consumption per slot
- PV: forecasted solar production per slot

Internally, the integration normalizes different source formats into a common time-series model before planning starts.

## Source groups

## Price

Price is fundamental.

Supported provider styles:

- Entity adapter
  Preferred when your pricing integration exposes structured forecast data on an entity.
- Service adapter
  Preferred when your pricing integration exposes data through a service response instead of entities.
- Template
  Fallback when you need to reshape or build the price series yourself.

Preferred order:

1. Entity adapter
2. Service adapter
3. Template

## Usage

Usage is fundamental.

Supported provider styles:

- Built in
  Preferred when you already have a proper `kWh` energy sensor and want WattPlan to build a forecast from recorded history/statistics.
- Entity adapter
  Preferred when another integration already exposes a structured usage forecast as entity data.
- Service adapter
  Preferred when usage forecast data is available through a service response.
- Template
  Fallback when you need to model or reshape usage data yourself.

Preferred order:

1. Built in
2. Entity adapter
3. Service adapter
4. Template

## PV

PV is optional.

Supported provider styles:

- Energy provider
  Preferred when your solar forecast already exists in Home Assistant Energy through a compatible forecast provider.
- Entity adapter
  Preferred when another integration exposes structured PV forecast data as entity data.
- Service adapter
  Preferred when PV forecast data is only available from a service response.
- Template
  Fallback when you need to reshape or construct the PV data yourself.
- Not used
  Valid when you do not have solar or want to get price/load planning working first.

Preferred order:

1. Energy provider
2. Entity adapter
3. Service adapter
4. Template
5. Not used

If PV is unavailable or partially missing, WattPlan can still plan, but it may fall back to planning without solar contribution for the affected period.

## Source modes

Choose the highest mode that naturally matches your data source. The further down the list you go, the more manual shaping you typically need to do.

- `Built in`
  Use a Home Assistant `kWh` energy sensor and let WattPlan build the usage forecast from stored history/statistics.
- `Energy provider`
  Use a Home Assistant Energy solar forecast provider directly.
- `Entity adapter`
  Read structured forecast data from entity state/attributes.
- `Service adapter`
  Read structured forecast data from a service response.
- `Template`
  Last-resort escape hatch when you need full control over the payload.
- `Not used`
  Only relevant for PV when solar is not part of the setup.

<details>
<summary><code>Built in</code> (preferred for usage when you have a proper energy sensor)</summary>

Use it when:

- you have a proper energy sensor in Home Assistant
- the sensor represents energy in `kWh`
- you want WattPlan to build a usage forecast from stored history/statistics

Notes:

- this mode is currently relevant for usage
- the source entity is validated before use
- in practice, the entity must behave like a real energy sensor, not just a generic numeric sensor

</details>

<details>
<summary><code>Energy provider</code> (preferred for PV when Home Assistant Energy already has the forecast)</summary>

Use it when:

- you already have a Home Assistant Energy solar forecast provider configured
- you want WattPlan to consume that provider directly instead of rebuilding the same source yourself

Notes:

- this mode is currently relevant for PV
- it uses integrations that expose solar forecast data through the Home Assistant Energy platform

</details>

<details>
<summary><code>Entity adapter</code> (preferred when forecast data is already on an entity)</summary>

Use it when:

- another integration already exposes forecast data as state attributes
- your data is already stored in Home Assistant entities
- you want WattPlan to adapt an existing entity payload without writing template logic

Notes:

- this mode supports adapter-driven extraction from attribute-based payloads
- WattPlan can auto-detect some mappings

</details>

<details>
<summary><code>Service adapter</code> (preferred when forecast data comes from a service call)</summary>

Use it when:

- your upstream data is exposed through a Home Assistant service
- you want WattPlan to fetch fresh structured data on demand
- the service returns a response payload rather than writing to entities first

Notes:

- this is useful when the source integration is API-shaped rather than entity-shaped

</details>

<details>
<summary><code>Template</code> (last resort, but always available)</summary>

Use it when:

- none of the higher-level modes fit
- you already know how to render the data you want in Jinja
- you want full control over payload shape

Notes:

- the template is expected to render native structured data, not an opaque string blob
- this mode is flexible, but it puts more responsibility on the user to return the right shape

Example template output shape:

```jinja2
[
  {"start": "2026-03-08T12:00:00+00:00", "value": 0.25},
  {"start": "2026-03-08T13:00:00+00:00", "value": 0.27},
  {"start": "2026-03-08T14:00:00+00:00", "value": 0.24}
]
```

Expected format:

- a list
- each item contains a timestamp and a numeric value
- timestamps should be parseable datetimes
- values should be numeric

</details>

<details>
<summary><code>Not used</code> (PV only)</summary>

Use it when:

- you do not have solar
- you are configuring the integration before the PV source exists
- you want to get price/load planning working first

</details>

## Data shape and normalization

The integration accepts several upstream payload styles, but the goal is always the same: turn the configured source into a slot-aligned numeric sequence for the planner.

That normalization layer handles things like:

- extracting values from templates, entities, services, or energy providers
- aligning timestamps to the planner slot size
- aggregating multiple values into a slot
- handling off-grid timestamps
- resampling missing intervals
- edge-filling where appropriate

The end result is that WattPlan does not require every upstream system to already use the exact planner time resolution.

## Why there are so many knobs

Forecast data in Home Assistant is messy in the real world.

Common problems:

- source data arrives hourly, but planning is done at a shorter slot size
- timestamps are slightly off-grid
- the payload has gaps
- the payload starts too late or ends too early
- one integration returns lists of values while another returns objects with timestamps

WattPlan’s source options exist so users can adapt their own data instead of being locked to one specific forecast schema.

## Fixup and repair behavior

For configurable source modes, WattPlan can repair or extend imperfect data depending on the selected fixup profile.

That is useful when:

- the source is slightly short
- the source has gaps
- you still want a usable plan without manually preprocessing every forecast upstream

This is especially important for making the integration practical across many different Home Assistant environments.

## FAQ

### My electricity price integration offers data through an entity with attributes

Use `Entity adapter`.

That is the preferred path when the integration already exposes structured forecast data on an entity. Let WattPlan adapt that entity instead of rewriting the same data in a template.

### My usage data is a normal Home Assistant energy sensor in `kWh`

Use `Built in`.

That is the preferred usage path when you already have a proper energy sensor and want WattPlan to derive the forecast from recorder history/statistics.

### My solar forecast already shows up in Home Assistant Energy

Use `Energy provider`.

That is the preferred PV path when a compatible Energy forecast provider already exists.

### My integration only gives me data through a service call

Use `Service adapter`.

That is the preferred path when there is no entity with structured forecast data, but there is a service response that returns it.

### The other options don't work for me

Use `Template`.

That is the fallback when you need full control. Start by returning a list of timestamp/value pairs like this:

```jinja2
[
  {"start": "2026-03-08T12:00:00+00:00", "value": 1.2},
  {"start": "2026-03-08T13:00:00+00:00", "value": 1.0},
  {"start": "2026-03-08T14:00:00+00:00", "value": 0.9}
]
```

Make sure the rendered data is:

- a native list structure
- timestamped
- numeric
- long enough for the configured planning horizon after normalization/fixup

### What should I configure first?

Use this order:

1. Get price working
2. Get usage working
3. Add PV if you have solar
4. Only then add batteries, comfort loads, or optional loads

That keeps the first setup focused on getting valid planner input end to end.
