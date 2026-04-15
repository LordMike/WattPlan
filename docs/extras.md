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
Batteries model controllable storage. WattPlan plans battery behavior as:
- `charge_grid`
- `charge_pv`
- `charge_grid_pv`
- `discharge`
- `hold`

It also tracks battery targets and timing data so you can expose planned behavior in the UI and automations.

### When to Use Them
Use a battery when:
- You have a home battery or battery-backed inverter.
- Your inverter or control stack can be told to charge from grid or PV, discharge, or hold.
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
3. Map `charge_grid`, `charge_pv`, `charge_grid_pv`, `discharge`, or `hold` to your inverter's controls.
4. Call the real inverter service, script, switch, or helper sequence.

**Example Mapping Concept:**
- `charge_grid` -> Set inverter/battery system to charge from the grid.
- `charge_pv` -> Set inverter/battery system to charge from PV surplus.
- `charge_grid_pv` -> Set inverter/battery system to allow charging from either grid or PV.
- `discharge` -> Set inverter/battery system to discharge/export/self-consume mode.
- `hold` -> Stop active charging/discharging and leave the battery neutral.

The exact translation depends on your inverter integration. WattPlan does not directly control every battery platform; it publishes the intended action and lets your automations bridge that to your actual system.

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
- `charge_grid`
- `charge_pv`
- `charge_grid_pv`
- `discharge`
- `hold`
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
