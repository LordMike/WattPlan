# Extras

WattPlan can do more than forecast price, usage, and PV. You can also add extra controllable assets so the planner can produce actions and suggestions for them.

See [entities-and-services.md](entities-and-services.md) for the full list of exposed entities and services, including battery target services.

The three extra asset types are:
- **Batteries**
- **Comfort Loads**
- **Optional Loads**

All of these are configured inside the WattPlan integration UI. WattPlan then exposes sensors that your own Home Assistant automations can read and translate into actions on your real devices.

## Batteries

### What They Are
Batteries model controllable storage. WattPlan exposes each battery's action as an inverter-control policy:
- `preserve`
- `self_consume`
- `grid_charge`

It also tracks battery targets and timing data so you can expose planned behavior in the UI and automations.

### When to Use Them
Use a battery when:
- You have a home battery or battery-backed inverter.
- Your inverter or control stack can be told to allow/block battery discharge and enable/disable scheduled grid charging.
- You want WattPlan to shift energy based on price, usage, and PV availability.

### How to Configure Them
Configure batteries through the WattPlan integration UI:
1. Open `Settings` -> `Devices & Services`
2. Open `WattPlan`
3. Add a battery asset
4. Fill in the battery parameters shown in the flow
5. Save the configuration

This is a WattPlan UI flow. You do not configure batteries by editing YAML.

### How to Use Them
WattPlan exposes battery-related entities such as:
- A battery action sensor
- A battery target sensor

The battery action sensor is the key one for control. Your automation should read that action and then translate it into the command model your inverter understands.

**Typical Pattern:**
1. Create an automation that triggers when the WattPlan battery action entity changes.
2. Read the action value from WattPlan.
3. Map `preserve`, `self_consume`, or `grid_charge` to your inverter's controls.
4. Call the real inverter service, script, switch, or helper sequence.

### Battery Policy States
The battery action sensor exposes policy, not raw measured or forecast battery flow. A slot where the plan shows no modeled battery delta often still emits `self_consume`, because the inverter should normally be allowed to cover real load that differs from the forecast.

| Policy | Meaning |
| --- | --- |
| `preserve` | Save stored energy because the model shows that spending it now would make the plan worse or violate constraints. Your automation should prevent this battery from discharging. PV charging may still be allowed by your inverter setup. |
| `self_consume` | Normal battery operation. Allow this battery to cover real load. Do not request grid charging. This is the default policy when the plan has no positive reason to preserve or grid-charge. |
| `grid_charge` | Request or allow grid charging for this battery and prevent the battery from being spent while doing so. |

PV surplus handling is not a battery action state in this version. PV export is a site-level decision, especially with multiple batteries, and is deferred for a future site-level policy design. Treat PV charging as normal inverter behavior unless your own automation needs a different device-specific rule.

The exact translation depends on your inverter integration. WattPlan does not directly control every battery platform; it publishes the intended action and lets your automations bridge that to your actual system.

### Solar Assistant/MQTT/Inverter Example
This example describes one practical setup: Home Assistant entities exposed by Solar Assistant over MQTT for a Deye-compatible inverter. It is not universal. Other inverter brands may map the same three WattPlan policies to different entities or services.

In this style of setup, the inverter time-of-use schedule controls can be more reliable than direct mode controls. A time-of-use capacity point is used to allow or block battery discharge, and a grid charge point switch is used to enable scheduled grid charging. Max charge/current entities may also exist, but grid charge current can often be treated as a configured cap instead of being changed on every policy update.

Generic policy mapping:

| WattPlan policy | Discharge allowed | Grid charging | Battery charging from PV |
| --- | --- | --- | --- |
| `preserve` | No | Off | Allowed/normal |
| `self_consume` | Yes | Off | Allowed/normal |
| `grid_charge` | No | On | Allowed |

Example time-of-use mapping:

| WattPlan policy | Time-of-use capacity point | Grid charge point | Charge current |
| --- | --- | --- | --- |
| `preserve` | High, for example `100%`, to prevent discharge | Off | Normal/static unless the setup needs otherwise |
| `self_consume` | Normal minimum, for example `10%` | Off | Normal/static |
| `grid_charge` | High, for example `100%`, to preserve while charging | On | Configured normal charging cap |

## Comfort Loads

### What They Are
Comfort loads are loads that must still run regularly but can be shifted. Typical examples include:
- Heating
- Hot water
- Circulation or utility pumps

WattPlan plans them as on/off decisions with comfort-related constraints.

### When to Use Them
Use a comfort load when:
- The device is important and cannot simply be skipped all day.
- You can defer it somewhat without losing the underlying function.
- You want WattPlan to help decide when it should be on or off.

### How to Configure Them
Configure comfort loads through the WattPlan integration UI:
1. Open `Settings` -> `Devices & Services`
2. Open `WattPlan`
3. Add a comfort load
4. Fill in the comfort timing and power-related fields in the flow
5. Save the configuration

This is also a WattPlan UI flow.

### How to Use Them
WattPlan exposes a comfort action sensor for each configured comfort load.

**Typical Pattern:**
1. Create an automation that triggers when the comfort action entity changes.
2. Read the WattPlan action value.
3. Translate `on` / `off` into the real device command.
4. Call the actual switch, climate entity, script, or helper that controls the load.

WattPlan decides when the load should be on or off. Your automation is what applies that recommendation to the real device.

## Optional Loads

### What They Are
Optional loads are flexible nice to run loads. Typical examples include:
- Dishwasher
- Washing machine
- Dryer
- EV charging sessions treated as optional runs

WattPlan does not force these into the main schedule. Instead, it returns one or more suggested start options.

### When to Use Them
Use an optional load when:
- The run can be delayed within a window.
- You want suggestions rather than a mandatory always-on/off decision.
- You want to pick from one or more candidate times.

### How to Configure Them
Configure optional loads through the WattPlan integration UI:
1. Open `Settings` -> `Devices & Services`
2. Open `WattPlan`
3. Add an optional load
4. Fill in duration, energy, run-within window, and option count
5. Save the configuration

Again, this is all done in the WattPlan config flow.

### How to Use Them
WattPlan exposes optional-load sensors such as:
- Next suggested start
- Next suggested end
- One or more option start timestamps

**Typical Pattern:**
1. Read the suggested start sensor.
2. Decide whether to accept it automatically or present it to the user.
3. Create an automation that starts the device at the selected suggested time.

Optional loads are recommendation-oriented. They are a good fit when you want WattPlan to suggest the best times without turning the device on immediately.

## Automation Pattern
The general rule for all extras is:
1. Configure the asset in WattPlan.
2. Let WattPlan publish action or suggestion entities.
3. Create your own Home Assistant automations that translate those entities into commands for the real device.

That separation is intentional:
- WattPlan focuses on planning.
- Your automations focus on device-specific control.

## FAQ
### Does WattPlan directly control my inverter, heater, or appliance?
Usually no. WattPlan publishes the intended action or time suggestion. You connect that to your real hardware using Home Assistant automations, scripts, helpers, or service calls.

### How do I control a battery inverter with WattPlan?
Use the WattPlan battery action entity as the planner output. Then create an automation that maps:
- `preserve`
- `self_consume`
- `grid_charge`
To whatever your inverter integration actually supports.

That might be:
- A service call
- A helper value
- A select entity
- A script that applies a complete inverter mode change.

### Can I start with forecasts only and add extras later?
Yes. That is the recommended path:
1. Get price and usage working.
2. Add PV if needed.
3. Only then add batteries, comfort loads, or optional loads.
