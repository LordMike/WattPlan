# Home Assistant Testbed

The WattPlan testbed is a disposable Home Assistant config directory with two
custom integrations loaded from this repo:

- `wattplan`, the integration under test.
- `wattplan_testbed`, a development-only fake data integration under
  `testbed/custom_components/wattplan_testbed`.

The testbed integration is config-flow driven. It supports subentries for
prices, PV, load, and batteries, so a running HA instance can have 0..N fake
assets without editing YAML or automating the browser UI.

The root testbed entry only stores a name and `update_interval_minutes`. The
old `slot_minutes` and `horizon_hours` fields are migrated or ignored. All
generators expose the current value plus a fixed 24-hour `future_values`
attribute; the future window is intentionally not configurable.

## Quick Start

Use a config directory under `/tmp` unless you have a reason to keep it
elsewhere:

```bash
python scripts/testbed.py --config /tmp/wattplan-ha-testbed/default bootstrap
python scripts/testbed.py --config /tmp/wattplan-ha-testbed/default add-demo-assets
python scripts/testbed.py --config /tmp/wattplan-ha-testbed/default stop
../hass-core/.venv/bin/python scripts/testbed_backfill.py --config /tmp/wattplan-ha-testbed/default --days 14
python scripts/testbed.py --config /tmp/wattplan-ha-testbed/default configure-wattplan-demo
```

Open `http://127.0.0.1:8124` and sign in with:

- Username: `wattplan`
- Password: `wattplan-testbed`

Set `WATTPLAN_TESTBED_PORT`, `WATTPLAN_TESTBED_USER`,
`WATTPLAN_TESTBED_PASSWORD`, or `WATTPLAN_HASS` if the defaults do not match
your local HA checkout.

## What Gets Created

`add-demo-assets` creates one `wattplan_testbed` config entry with these
subentries:

- `Demo Import Price` and `Demo Export Price`: separate one-price sensors named
  `sensor.wattplan_testbed_demo_import_price_price` and
  `sensor.wattplan_testbed_demo_export_price_price`. Their state is the current
  price and `future_values` contains 24 hours of price objects in the Home
  Assistant currency per kWh.
- `Demo PV`: a live PV power sensor in watts and an HA Energy solar forecast
  provider.
- `Light Load` and `Heavy Load`: live load power sensors in watts plus
  cumulative energy sensors suitable for WattPlan's built-in usage source.
- `Home Battery` and `EV Battery`: SoC sensors, availability binary sensors,
  and preset buttons.

Battery buttons are available at runtime:

- Set the battery available.
- Set the battery unavailable by turning the availability entity off.
- Set SoC to 10%, 80%, or 100%.

Source subentries also expose number entities for live generator tuning, such as
price offset, PV peak kW, cloud factor, load factor, and noise. Every source
subentry can be edited from the integration page.

## Historical Data

`scripts/testbed_backfill.py` must run while HA is stopped. It reads the
`wattplan_testbed` config entry and backfills every entity belonging to every
subentry in that entry:

```bash
python scripts/testbed.py --config /tmp/wattplan-ha-testbed/default stop
../hass-core/.venv/bin/python scripts/testbed_backfill.py --config /tmp/wattplan-ha-testbed/default --days 30
python scripts/testbed.py --config /tmp/wattplan-ha-testbed/default start
```

By default the script deletes existing recorder rows in the generated time
window before writing replacements. Use `--no-replace-window` to append instead,
or `--dry-run` to see the entry, window, and entity IDs without changing the
database.

The writer uses Home Assistant's recorder SQLAlchemy models and verifies the
database schema version before inserting data. It still writes directly to the
recorder database, so stop HA first and start HA once before backfilling if the
database needs migrations.

## WattPlan Demo Wiring

Run `configure-wattplan-demo` after historical data has been backfilled. It
configures a WattPlan entry through HA's config flow API:

- Import price from the fake import price entity adapter using `future_values`.
- Usage from the heavy load cumulative energy sensor using WattPlan's built-in
  usage source.
- PV from the fake HA Energy solar forecast provider.
- Export price from the fake export price entity adapter using `future_values`.
- Two WattPlan battery subentries wired to the fake battery SoC and availability
  entities.

The command is idempotent for the standard demo names and reloads the affected
config entries after changes.

## Adding More Assets

In the running HA instance, add more `wattplan_testbed` subentries from
Settings -> Devices & Services -> Integrations -> WattPlan Testbed. After
adding assets that need historical data:

1. Stop HA.
2. Run `scripts/testbed_backfill.py`.
3. Start HA again.
4. Configure WattPlan sources or subentries against the new entities.

Backfill is entry-wide by design. That keeps generated prices, PV, load, and
battery data aligned to the same slot window.
