# Source Data

WattPlan plans around four source groups:
- **Price**
- **Export price**
- **Usage**
- **PV**

Price is the only required source. Usage and PV are optional, and export price is optional when PV is configured. These extra sources let WattPlan plan against expected consumption, solar surplus, and the value of exported energy instead of price alone.

## What WattPlan Needs
At planning time, WattPlan needs one numeric value per planner slot for each configured source:
- **Price:** Forecasted import price per kWh
- **Export price:** Forecasted export value per kWh
- **Usage:** Forecasted consumption in kWh per slot
- **PV:** Forecasted solar production in kWh per slot

The planner window starts at the beginning of the current slot (the current time rounded down to the slot length) and covers the configured planning horizon. Every source is normalized into exactly one finite value per slot of that window before planning starts; see [Normalization](#normalization) for how that happens.

## Source Groups
### Price
Price is fundamental.

This is specifically the cost of buying power from the grid.

**Supported Provider Styles:**
- **Entity Adapter:** Preferred when your pricing integration exposes structured forecast data on an entity.
- **Service Adapter:** Preferred when your pricing integration exposes data through a service response instead of entities.
- **Template:** Fallback when you need to reshape or build the price series yourself.

**Preferred Order:**
1. Entity Adapter
2. Service Adapter
3. Template

### Export price
Export price is optional and only matters when PV is configured.

If this source is not configured, WattPlan values exported power at zero.

**Supported Provider Styles:**
- **Entity Adapter:** Preferred when your pricing integration exposes export forecast data on an entity.
- **Service Adapter:** Preferred when your pricing integration exposes export forecast data through a service response.
- **Template:** Fallback when you need to reshape or build the export price series yourself.
- **Not used:** Keep exported power at zero value.

**Preferred Order:**
1. Entity Adapter
2. Service Adapter
3. Template
4. Not used

### Usage
Usage is optional.

**Supported Provider Styles:**
- **Built-in:** Preferred when you already have a proper `kWh` energy sensor and want WattPlan to build a forecast from recorded history/statistics (14 days of history by default).
- **Entity Adapter:** Preferred when another integration already exposes a structured usage forecast as entity data.
- **Service Adapter:** Preferred when usage forecast data is available through a service response.
- **Template:** Fallback when you need to model or reshape usage data yourself.

**Preferred Order:**
1. Built-in
2. Entity Adapter
3. Service Adapter
4. Template

### PV
PV is optional.

**Supported Provider Styles:**
- **Energy provider:** Preferred when a solar forecast integration (for example Forecast.Solar or Solcast) is registered with Home Assistant Energy. WattPlan reads the same forecast the Energy dashboard uses. This mode always uses the *Extend daily pattern* fixup profile and edge hold.
- **Entity Adapter:** Preferred when your PV integration exposes structured forecast data on an entity.
- **Service Adapter:** Preferred when your PV integration exposes data through a service response.
- **Template:** Fallback when you need to reshape or build the PV series yourself.

**Preferred Order:**
1. Energy provider
2. Entity Adapter
3. Service Adapter
4. Template

## Payload Formats
Entity, service, and template providers must resolve to a list in one of two shapes:

- **Timestamped objects**, for example `[{"start": "2026-01-01T00:00:00+01:00", "value": 0.31}, ...]`. The timestamp and value keys are configurable (auto detect fills them in). Each object starts an interval that normally lasts until the next object (see [Normalization](#normalization), step 4). Timestamps without a timezone are read in Home Assistant's configured time zone (as in most HA sensors that expose local time), not UTC.
- **Unlabelled numbers**, for example `[0.31, 0.29, ...]`. Index 0 is always the **current slot**, index 1 the next slot, and so on. A shorter list covers only the first slots. A longer list must be an exact multiple of the horizon length; its values are then spread evenly over each slot, so a list with two values per slot is read as half-slot intervals. Prefer timestamped objects whenever the source has timestamps, because a numeric list cannot express when its data actually starts.

Templates must return a native list, not a JSON string.

Energy provider forecasts are read as Wh per forecast period and converted to kWh. Periods that the forecast omits between two known periods (Forecast.Solar, for example, skips the night) count as zero energy rather than missing data.

## Multiple Providers
An entity-attribute source can select several entities, for example one entity for today's prices and one for tomorrow's. Each entity is read separately and all points are merged into one series before normalization, so the merged series follows exactly the same rules as a single provider. Where two providers report the same timestamp, the aggregation mode decides which value wins (*First*/*Last* follow the order the entities were selected in).

If one provider fails or returns no usable points while the others still produce data, the source is resolved from the remaining providers and a warning is logged. The *Direct only* profile applies to merged sources the same way as to single-provider sources.

## Normalization
Each source goes through these steps on every planning run:

1. **Parse** the provider payload into timestamp/value points. NaN and infinite values are rejected; signed finite values (such as negative tariffs) are kept.
2. **Align timestamps** according to *Timestamp alignment* (`clamp_mode`). *Strict* requires every timestamp to sit exactly on the alignment grid; *Nearest interval* moves each timestamp to the nearest grid point. The grid is the planner slot, or a whole fraction of it when the source is finer than the planner (for example 15-minute data on 60-minute slots keeps a 15-minute grid). Points from before the window start are dropped once their interval has ended; they never land on the current slot.
3. **Resolve duplicate timestamps** with the *Aggregation mode*. *First* and *Last* follow provider and payload order, not value order.
4. **Build intervals.** Each point lasts until the next point, but never longer than the source's typical spacing (the most common gap between points; the shorter one on a tie). A hole in the data therefore stays a gap instead of silently repeating the previous value.
5. **Map intervals onto planner slots.** How this works depends on what the source measures:
   - **Prices** (import and export) are per kWh. A price applies unchanged to every slot its interval covers, so an hourly price of 0.30 becomes 0.30 in each of four 15-minute slots. When several price intervals fall inside one slot, the aggregation mode combines them; *Mean* is weighted by how long each price applies.
   - **Energy** (usage and PV) is split in proportion to how much of the interval overlaps each slot, and the energy of several intervals inside one slot is summed. An hourly 1.0 kWh becomes 0.25 kWh per 15-minute slot, and four 15-minute values of 0.25 kWh become 1.0 kWh in a 60-minute slot. For energy the aggregation mode only matters for duplicate timestamps.
6. **Repair gaps** inside the series with *Gap repair* (`resample_mode`): *Forward fill* repeats the last known value, *Linear* interpolates between the surrounding known values.
7. **Fill edges** with *Edge fill* (`edge_fill_mode`): *Hold edge* repeats the first known value back to the window start and the last known value to the window end.
8. **Validate** that every slot now has a value. If not, the source fails with the number of usable slots and the fixup profile decides what happens next (see below).

### Value unit (usage and PV)
Template, entity-attribute, and service sources for usage and PV have a *Value unit* option under advanced processing:

- **Energy per interval (kWh)** (default): each value is the energy used or produced during its own interval.
- **Average power (kW)**: each value is the average power over its interval. WattPlan multiplies it by the interval length before step 5. Use this for Solcast `detailedForecast` `pv_estimate`, which is the average kW over each half hour.

Prices are always read per kWh, and Energy provider and built-in sources already deliver energy, so they have no unit option.

## Fixup Profiles
The fixup profile decides how far WattPlan may go to produce a full window.

| Profile | Gap repair and edge fill | When the window still cannot be filled |
|---|---|---|
| **Direct only** (`strict_input`) | Forced off, whatever the advanced settings say. This also applies to sources with several providers. | Reuse the last good window if it still covers the whole request (see below), otherwise fail. |
| **Repair local gaps** (`repair_gaps`, default) | As configured under advanced processing. | Same as Direct only. |
| **Extend daily pattern** (`extend_daily_pattern`) | As configured under advanced processing. | If at least the first 24 hours resolve completely, fill the missing tail by repeating the value from 24 hours earlier. Otherwise reuse the last good window, extended the same way. |

### Advanced processing defaults
The setup and options forms pre-fill these values:

| Setting | Form default | Value used when the key is missing from stored config |
|---|---|---|
| Aggregation mode | First | Mean |
| Timestamp alignment | Nearest interval | Strict |
| Gap repair | Disabled | Disabled |
| Edge fill | Hold edge | Disabled |
| Value unit (usage/PV) | Energy per interval (kWh) | Energy per interval (kWh) |
| Fixup profile | Repair local gaps (Energy provider: Extend daily pattern) | Repair local gaps |

When a source is reported as incomplete in Home Assistant Repairs, the repair action switches it to *Extend daily pattern* with First, Nearest interval, Linear, and Hold edge.

## Stale Reuse
After every successful run WattPlan remembers the normalized window. When a later refresh fails, it may reuse that window, shifted to the new start time, instead of failing straight away. The reuse is bounded:

- The slot length must be unchanged and the new window must start on a slot boundary inside the remembered window. Once the remembered window has fully elapsed it is never reused.
- With *Direct only* and *Repair local gaps*, the remembered values must still cover the whole requested window, so in practice reuse only bridges failures until the next slot starts.
- With *Extend daily pattern*, the remembered window is extended by repeating the value from 24 hours earlier, which needs at least 24 hours of remembered values. Reuse stops when the remembered window has fully elapsed.
- Only fully finite windows are remembered.

While stale data is in use, the source status and the Repairs issue report the source as unavailable or incomplete together with the time the remembered data runs out.
