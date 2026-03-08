# Source Data

WattPlan plans around three source groups:
- **Price**
- **Usage**
- **PV**

Price and usage are the core planner inputs. PV is optional, but it allows WattPlan to recognize solar surplus and plan around self-consumption or charging opportunities.

## What WattPlan Needs
At planning time, WattPlan needs one numeric value per time slot for each configured source:
- **Price:** Forecasted price per kWh
- **Usage:** Forecasted consumption per slot
- **PV:** Forecasted solar production per slot

Internally, the integration normalizes different source formats into a common time-series model before planning starts. This normalization process is crucial for ensuring data quality and consistency across different input sources.

## Source Groups
### Price
Price is fundamental.

**Supported Provider Styles:**
- **Entity Adapter:** Preferred when your pricing integration exposes structured forecast data on an entity.
- **Service Adapter:** Preferred when your pricing integration exposes data through a service response instead of entities.
- **Template:** Fallback when you need to reshape or build the price series yourself.

**Preferred Order:**
1. Entity Adapter
2. Service Adapter
3. Template

### Usage
Usage is fundamental.

**Supported Provider Styles:**
- **Built-in:** Preferred when you already have a proper `kWh` energy sensor and want WattPlan to build a forecast from recorded history/statistics.
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
- **Entity Adapter:** Preferred when your PV integration exposes structured forecast data on an entity.
- **Service Adapter:** Preferred when your PV integration exposes data through a service response.
- **Template:** Fallback when you need to reshape or build the PV series yourself.

**Preferred Order:**
1. Entity Adapter
2. Service Adapter
3. Template

## Normalization Process
The normalization process involves converting various input formats into a unified time-series model. This is essential for ensuring that WattPlan can effectively utilize the data provided by different sources. Users should be aware that discrepancies in input formats may lead to data quality issues, and troubleshooting may be necessary if the expected results are not achieved.
