# Source Data

WattPlan plans around three source groups:

- price
- usage
- PV

Price and usage are the core inputs for most installations. PV is optional, but it is what allows WattPlan to recognize solar surplus and plan around self-consumption or charging opportunities.

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

Use this for:

- hourly or quarter-hourly spot pricing
- import tariffs
- dynamic retail pricing

## Usage

Usage is fundamental.

Use this for:

- forecasted household load
- expected consumption from a source entity or service
- built-in history-based forecasting from an energy sensor

## PV

PV is optional.

Use this for:

- rooftop solar production forecasts
- energy-provider forecasts exposed through Home Assistant Energy integrations
- your own template, entity, or service-based forecast source

If PV is unavailable or partially missing, WattPlan can still plan, but it may fall back to planning without solar contribution for the affected period.

## Supported source modes

WattPlan supports several source modes so the same planner can work across very different Home Assistant setups.

## Template

Template mode is the lowest-level escape hatch.

Use it when:

- you already know how to render the data you want in Jinja
- you want full control over payload shape
- there is no dedicated integration path for your data source

The template is expected to render native structured data, not an opaque string blob.

This mode is flexible, but it also puts more responsibility on the user to return the right shape.

## Entity adapter

Entity adapter mode reads structured forecast data from one or more entities.

Use it when:

- another integration already exposes forecast data as state attributes
- your data is already stored in Home Assistant entities
- you want WattPlan to adapt an existing entity payload without writing template logic

This mode supports adapter-driven extraction from attribute-based payloads and can auto-detect some mappings.

## Service adapter

Service adapter mode gets forecast data from a Home Assistant service response.

Use it when:

- your upstream data is exposed via a service call
- you want WattPlan to fetch fresh structured data on demand
- the service returns a response payload rather than writing to entities first

This is useful when the source integration is API-shaped rather than entity-shaped.

## Energy provider

Energy provider mode is currently relevant for PV.

Use it when:

- you already have a Home Assistant Energy solar forecast provider configured
- you want WattPlan to consume that provider directly instead of re-modeling it yourself

This path uses integrations that expose solar forecast data through the Home Assistant Energy platform.

## Built in

Built-in mode is currently relevant for usage.

Use it when:

- you have a proper energy sensor in Home Assistant
- the sensor represents energy in `kWh`
- you want WattPlan to build a usage forecast from stored history/statistics

This mode validates the source entity before use. In practice, that means the entity must behave like an energy sensor, not just a generic numeric sensor.

## Not used

PV can be marked as not used.

That is useful when:

- you do not have solar
- you are configuring the integration before the PV source exists
- you want to get price/load planning working first

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

## Practical guidance

If you are choosing a source mode for the first time:

- use `Built in` for usage if you already have a proper `kWh` energy sensor and want the easiest path
- use `Energy provider` for PV if your solar forecast is already integrated with Home Assistant Energy
- use `Entity adapter` when another integration already exposes structured forecast data in entity attributes
- use `Service adapter` when the data naturally comes from a service response
- use `Template` when you need full control or are adapting unusual data

## What to configure first

A practical setup order is:

1. Get price working
2. Get usage working
3. Add PV if you have solar
4. Only then add batteries, comfort loads, or optional loads

That keeps the first setup focused on getting valid planner input end to end.
