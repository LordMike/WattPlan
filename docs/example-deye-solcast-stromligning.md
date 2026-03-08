# Example Setup: Deye + Solcast + Strømligning

This walkthrough shows one realistic WattPlan setup from a Home Assistant user's point of view.

It is based on:
- **Price** from Strømligning
- **Usage** from a Deye energy sensor
- **PV** from Solcast
- one battery
- one optional load

Use it as a concrete example, not as a universal recipe. The exact entity names and the right values for your setup may differ.

## What This Example Tries To Achieve

This example aims for a practical first setup:
- get WattPlan running with a price forecast first
- add usage so planning reflects expected household consumption
- add PV so planning can take solar production into account
- add a battery and an optional load afterward

It intentionally favors a setup path that is easy to reason about in the UI.

## Suggested Planner Settings

For many homes, these are sensible starter values:

- **Resolution:** `15 minutes`
- **Planning horizon:** `48 hours`

Why `15 minutes`:
- many Home Assistant energy and forecast integrations naturally expose hourly or quarter-hourly data
- 15-minute plans are detailed enough to drive batteries and discretionary loads without becoming too coarse
- it matches common tariff and inverter control use cases better than hourly planning

Why `48 hours`:
- many price and PV sources provide today + tomorrow data
- 48 hours gives the optimizer enough context to plan around the next morning, next evening peak, and the next solar window

If your sources only cover 24 hours reliably, reduce the horizon first instead of forcing templates too early.

## Before You Start

For the integration setup itself:
- **Required:** a price forecast source
- **Optional:** a usage forecast source
- **Optional:** a PV forecast source

WattPlan can plan from price alone. Usage and PV improve the plan quality.

## Step 1: Add The WattPlan Integration

In Home Assistant:
1. Go to `Settings` -> `Devices & Services`
2. Choose `Add Integration`
3. Search for `WattPlan`
4. Start the setup

Use:
- **Setup name:** `WattPlan Home` or another name that makes sense in your installation
- **Resolution:** `15 minutes`
- **Planning horizon:** `48 hours`

## Step 2: Configure The Price Source

Recommended source mode for this setup:
- **Entity attribute**

Good entity in this example:
- `sensor.stromligning_current_price_vat`

Why this is a good fit:
- it exposes a structured price forecast in entity attributes
- the forecast already contains time/value objects
- it covers a full short-term planning horizon well

Suggested setup path:
1. Choose **Entity attribute**
2. Keep **Adapter type** on **Auto detect**
3. Select `sensor.stromligning_current_price_vat`
4. Continue to review

Expected result:
- auto detect should find the correct list path and keys
- review should show coverage for the full selected horizon

## Step 3: Configure The Usage Source

Recommended source mode for this setup:
- **Built in**

Good entity in this example:
- `sensor.deye_load_energy`

Why this is a good fit:
- it represents household/load energy in `kWh`
- the built-in usage model can use Home Assistant history/statistics from this kind of source
- it avoids templates if the sensor already reflects whole-home or inverter-reported load appropriately

What to consider before choosing a usage sensor:
- prefer a sensor that reflects total household consumption, not just grid import
- prefer `kWh` energy history over instantaneous `W` power unless you have already converted it properly
- if multiple sensors exist, start with the one that most closely represents the real load WattPlan should plan against

Suggested setup path:
1. Choose **Built in**
2. Select `sensor.deye_load_energy`
3. Continue to review

Expected result:
- WattPlan should accept the source if enough history exists
- the forecast is synthesized from history, so the review may reflect historical coverage and generated forecast behavior rather than a raw future list from the source

## Step 4: Configure The PV Source

Recommended source modes for this setup:
1. **Energy provider** if Solcast is available there for your Home Assistant setup
2. **Entity attribute** if you want a direct entity-based path

Good entity in this example:
- `sensor.solcast_pv_forecast_forecast_today`

Why this is a good fit:
- it exposes forecast objects in attributes
- WattPlan can map the Solcast structure directly
- it is a realistic source for a 48-hour solar-aware plan

Suggested setup path:
1. Choose **Entity attribute**
2. Keep **Adapter type** on **Auto detect**
3. Select `sensor.solcast_pv_forecast_forecast_today`
4. Continue to review

If you instead use explicit fields, the relevant Solcast structure is typically:
- **Attribute path:** `detailedForecast`
- **Timestamp key:** `period_start`
- **Value key:** `pv_estimate`

Expected result:
- review should show enough usable intervals to cover the selected horizon

## Step 5: Finish The Initial Setup

Once price, usage, and PV are configured:
1. finish the source setup
2. open the created WattPlan integration

At this point, the integration details page is the best place to continue.

## Step 6: Add A Battery

Example battery values used in this walkthrough:
- **Name:** `Home Battery`
- **Capacity:** `10 kWh`
- **Minimum:** `1 kWh`
- **Max charge power:** `5 kW`
- **Max discharge power:** `5 kW`
- **SoC sensor:** `sensor.deye_battery_state_of_charge`
- **Can charge from grid:** enabled

Why these are reasonable starter values:
- `Minimum = 1 kWh` keeps a modest reserve
- `5 kW` charge/discharge limits are easy starter values for a medium residential battery
- allowing grid charging lets WattPlan exploit low-price periods and battery targets

What to adjust for your real system:
- reserve level if you want backup energy left in the battery
- power limits to match inverter and battery hardware
- whether grid charging should be allowed in your tariff or operating strategy

## Step 7: Add An Optional Load

Example optional load:
- **Name:** `Washing Machine`
- **Duration:** `120 minutes`
- **Run within:** `24 hours`
- **Energy:** `1.2 kWh`
- **Options to return:** `3`
- **Minimum option gap:** `120 minutes`

Why these are reasonable starter values:
- 2 hours is a practical wash-cycle approximation
- 1.2 kWh is a reasonable first estimate for many machines
- a 24-hour run window gives the planner room to choose a cheaper period
- 3 options gives the user meaningful choice without too much clutter

## After Setup

What you should expect:
- planner entities become available under your setup name
- battery and optional-load entities are created once those extras are added
- some suggestion or action entities may remain `unknown` until WattPlan has produced a successful plan

What to do next:
1. run the planner
2. inspect the generated entities
3. create automations that translate planned actions into real device commands

WattPlan publishes the plan. It does not automatically operate your battery, washer, HVAC, or other devices unless you wire those actions into automations yourself.

## Entities Used In This Example

Example entities from the reviewed flow:
- **Price:** `sensor.stromligning_current_price_vat`
- **Usage:** `sensor.deye_load_energy`
- **PV:** `sensor.solcast_pv_forecast_forecast_today`
- **Battery SoC:** `sensor.deye_battery_state_of_charge`

## If You Are Unsure Which Source To Pick

Start simple:
- **Price:** prefer an entity with a forecast list already in its attributes
- **Usage:** prefer the built-in source with a good `kWh` load-energy sensor
- **PV:** prefer Energy provider or a forecast entity that already exposes time/value objects

Only move to templates when the source you need is not already consumable through:
- Entity attribute
- Service call
- Built in
- Energy provider

