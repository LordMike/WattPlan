# Plan outlook: implementation specification

Status: approved direction for implementation. Written September 23, 2026.

## Goal

Produce a local, deterministic, human-readable energy forecast combining observed
starting conditions, input forecasts, and WattPlan's accepted plan. No LLM,
external text service, additional optimizer solves, or new required data sources.
Always produce useful supported prose, including a lower-value summary on quiet
days. Expose how noteworthy the message is separately from data reliability.

Target style: concise forecast language, approximately 40–65 words in up to two
short paragraphs. This is a budget, not a minimum: do not pad. Three or four short
sentences are acceptable. Prefer a coherent progression over disconnected facts.

## Editorial contract

- Say **grid prices**, not just prices; use **expected use** or **load**, not demand.
- Prefer solar, battery reserves, grid charging, grid use, building, easing,
  remaining, rising, and expected.
- Observed state is timestamped; forecast slot zero is not live telemetry.
- Actions are planned recommendations, not confirmation of device execution.
- Describe supported relationships without inventing optimizer motives.
- Current battery percentage earns space only when relevant to the outlook.
- Persistent configured-source failures can qualify a useful plan; they should
  not consume the whole message while meaningful supported facts remain.
- Missing/unconfigured features are not news. Never report absent solar merely
  because the user has no PV configured.
- A quiet report remains available, tagged low information value.
- No savings, renewable-origin, weather/cloud, or exact-flow claims without the
  corresponding evidence. Battery policies are coarse and projections remain
  estimates, not execution guarantees.

## Architecture

Keep all outlook modeling, selection, and prose outside optimizer/. Current layout:

- plan_outlook_types.py: language-neutral fact and selected-report models plus
  bounded persistence serialization.
- plan_outlook.py: extraction, grouping, significance, selection, status-model
  construction, and retained-model refresh. It must not contain rendered prose.
- plan_outlook_renderer.py and its language modules: English and Danish wording,
  language-specific time and duration text, deterministic phrase variants, and
  semantic/report identifiers.
- coordinator_logic/projection.py: capture input/result-derived facts while both
  are available. Supply source configuration/health provenance explicitly.
- coordinator/snapshot lifecycle: retain bounded, versioned outlook data and
  selection history; evaluate presentation against current time and health.
- sensors/outlook.py plus sensor.py, sensor_specs.py and sensors/__init__.py:
  expose Plan Outlook and its supporting attributes.

Do not depend on enabled plan-details diagnostic sensors, rounded diagnostic
values, opaque optimizer state decoding, or rereading unrelated HA entities.
Use ordinary Python/timezone-aware data in the pure layer. Follow existing
registry/naming and persistence conventions. Treat absent outlook payloads in old
snapshots safely. Outlook-generation errors must not invalidate a usable plan.

## Inputs and fact model

Capture aligned series, plan start, slot duration, accepted result, configured
assets, initial observed battery energy/capacity/minimum, current comfort state,
targets, optional recommendations, source provenance, coverage, and validation.

Each fact needs a stable kind, subject, start/end, basis (observed/forecast/planned),
supporting values, required inputs, significance, and compatible related facts.
Keep payloads bounded and JSON-serializable. Use end-of-slot battery levels at
their actual end timestamps. Do not mistake a target for a predicted achievement.

Initial fact families:

1. Sustained grid-price rise/fall, cheaper intervals, negative-price intervals.
2. Solar increase/decrease and surplus over base plus scheduled comfort load.
3. Relevant battery reserve, grid-charge/preserve intervals, projected fullness.
4. Important comfort timing when connected to meaningful conditions.
5. Ranked optional-load start suggestions, independently advisory.
6. Approaching targets and predicted shortfalls.
7. Energy balance: limited or heavy grid use, export periods, concentration of
   grid use in charging, changes between daytime/evening.
8. Persistent source/plan problems and remaining useful coverage.

Group consecutive slots into intervals. Avoid reporting a tiny isolated change.
Use explicit, documented significance thresholds with sensible relative and
absolute/energy floors; handle arbitrary tariff scales, zero and negative prices.
Do not use a fixed currency-specific threshold as a universal economic rule.
Thresholds are internal initially, tested and adjustable without adding a settings
screen. A minimum or maximum alone does not establish a meaningful opportunity.

## Energy-flow API addition

The optimizer already accounts for imports/exports during final schedule scoring.
Expose additive per-slot physical fields from that SAME accepted accounting,
including load, grid import/export and grid-sourced battery charging as needed.
Avoid duplicated inconsistent flow reconstruction. Cover full/prefix/no-battery
and accepted comfort-replanning paths. No extra solve or alternative optimization.
Document field units/semantics in docs/optimizer-api.md and test conservation.

Use per-slot imports, not daily net imports minus exports, for grid dependence.
Count grid charging as grid use. Include scheduled comfort load. Optional appliance
runs are not silently added to the baseline. Low expected use alone is not low
grid dependence. High absolute imports and high share of use are distinct claims.

“Mostly self-sufficient” means little grid import within a named period, not proof
of renewable origin of stored energy. Prefer “little grid use expected” when that
is clearer. Never infer solar-only energy from unknown battery provenance.
Missing/untrusted usage or PV suppresses affected balance claims. Zero-filled
fallback PV is not evidence of a naturally low-solar day.

## Periods and time

Use HA local timezone, including DST, midnight and tomorrow/today labels. Describe
the next meaningful overnight/daytime sequence within actual forecast coverage;
do not impose a rigid 12-hour cutoff which separates charging from solar refill.
Make comparisons within a documented bounded reporting window (up to the next
local day where covered). Never call partial-day coverage a whole-day forecast.
Use “rest of today” where appropriate. Remove passed appliance start suggestions.
Age out observed starting readings rather than repeating them as current.

## Selection and controlled variety

1. Filter unsupported or insignificant claims.
2. Prioritize substantive approaching consequences and sustained reliability
   problems. Preserve useful energy content alongside qualified source issues.
3. Choose a main fact and related supporting facts within the text budget.
4. Among similarly useful valid alternatives, give a SMALL bounded preference to
   fact types used less recently, then use deterministic seeded tie-breaking.
5. Select among a few semantically equivalent wording variants.

Rare trivial facts must never displace material consequences. Do not randomize
warnings away, assert contradictions, or repeat the same information twice.
Compute a semantic identifier before rendering. If it matches the preceding
outlook, retain that outlook's phrase variants rather than rerolling on the next
15-minute refresh. New semantic reports use stable site identity, local date,
reporting period (morning/afternoon/evening), and the semantic identifier as the
wording seed. Never use process-random Python hash as the persistence seed.
Within a period retain selection when still valid; update values/times, and
reselect when materially more important facts appear or selected facts expire.
Record history on actual report selection changes, not every planner callback.
Support predictable seeded tests. Variety does not increase information_value.

## Persistent problems

Track each configured source and planning failure independently using timestamps
and distinct planning slots. Default proposed threshold: N=4 consecutive affected
slots. Four 15-minute slots is one hour; manual retries do not advance the count.
Distinguish no new plan, usable retained plan, cached source estimates, omitted
source contribution, and expired/unvalidated plan. Do not infer failure duration
from the most recent plan alone. Clear streaks on actual recovery. Define restart
behavior without inventing pre-restart history; persist bounded state if useful.

Short transient failures suppress unsupported claims without generating cruft.
After N slots, explain the relevant consequence with natural elapsed time.
Loss of usable recommendations is immediately reportable, even before N slots.
Never generate action advice from a restored snapshot until validated. A status
outlook about an unusable plan can still be available: do not indiscriminately
apply an availability gate that prevents the failure message being displayed.

## Entity contract

Create one entry-level Plan Outlook sensor per resolved language. Entity IDs are
always suffixed, for example `sensor.home_plan_outlook_da` and
`sensor.home_plan_outlook_en`; no unsuffixed entity is created. Use a rendered
report identifier as state, with complete prose in attributes. The identifier is
`<main-kind>_<information-value>_<digest>` and changes whenever the selected
semantic report or that language's chosen wording variant changes. Expose the
independent `semantic_id` attribute used to retain wording across equivalent
reports. Do not put prose in state or switch state shape according to prose
length.

- report_id, semantic_id, headline, line_1, line_2, text, language (second line may be empty)
- information_value: low | medium | high
- topic: routine | opportunity | battery | target | energy_balance | reliability
- selected fact identifiers / basis and covered interval for diagnosis
- plan_created_at / valid_until where appropriate

Resolve Plan Outlook languages when the config entry loads. Start with the Home
Assistant system language, add any selections from the advanced options flow,
discard languages without an implemented renderer, and fall back to English if
none remain. The initial setup flow must not ask for language preferences.
English and Danish renderers are supported.

Reliability and information value must remain distinct. Keep attributes compact;
do not duplicate full input arrays. Keep phrase keys and placeholders isolated so
wording is not tangled with predicates. Full Danish translation of the integration
configuration UI is outside the Plan Outlook renderer scope.

## Implementation sequence

1. Add pure models and representative fixtures; implement energy-flow API with
   invariant tests against the existing accepted cost accounting.
2. Implement factual extraction, time/interval handling and significance checks
   as a language-neutral model with no rendered sentence fragments.
3. Implement coherent selection, then render it through a separate phrase
   catalogue with low-value fallbacks and stable bounded wording variety. Review
   generated gallery output, not only individual strings.
4. Integrate snapshots, source-health provenance, failure duration, refresh and
   restart behavior; add sensor wiring and supporting attributes.
5. Update architecture, optimizer API, entities/services docs and README feature
   summary. Add a concise user-facing explanation of outlook semantics.
6. Run focused tests, then full repository suite and HACS packaging as appropriate.
   Follow AGENTS.md testing/environment requirements. Report real command/output,
   test counts, failures and blockers rather than inferring success.

## Verification requirements

- Snapshot-style expected text for representative scenarios, plus semantic tests
  proving required claims and omissions (avoid tests that merely copy code).
- Flat/signed/near-zero prices; no PV configured; broken PV defaulting to zero;
  absent usage; no batteries; multiple opposing battery policies; comfort load;
  independent optional suggestions; near-full, low and target-shortfall batteries.
- Correct physical accounting and no extra optimizer solve count from outlook.
- Midnight/DST, short coverage, end-of-slot SoC timestamps, expired suggestions.
- Retained plans after refresh failure, restored unvalidated and expired plans.
- N-slot debounce with same-slot manual retries, recovery and restart.
- Stable output under tiny input changes, seeded repeatability, variation across
  reporting periods, rare-fact eligibility and no starvation of important facts.
- Entity state length, attributes, persistence compatibility, no dependence on
  optional diagnostic entities, no optimizer Home Assistant imports.

## Sample gallery

Samples below are editorial targets for matching synthetic facts, not unconditional
templates. Times/numbers must come from inputs. Classification is metadata.

### 01 — Overnight top-up (high, battery)
Reserves near minimum, with grid prices easing before dawn and solar building tomorrow. The battery is expected to fill by early afternoon, ahead of the evening grid-price peak.
Grid charging is planned 03:45–04:15, with a favourable appliance start at 14:15.

### 02 — Summer balance (medium, energy_balance)
Mostly self-sufficient tomorrow, with solar covering expected use through much of the day. Battery reserves should build by midday, with surplus feeding the grid through the afternoon.
A favourable washing-machine start is 12:30.

### 03 — Summer battery emphasis (medium, battery)
Solar building through the morning, with the battery expected full by 13:00. Little grid use is forecast through the afternoon and evening.
No grid charging is planned; a favourable washing-machine start is 12:30.

### 04 — Winter grid reliance (high, energy_balance)
Heavy grid use expected tomorrow, with limited solar and higher expected use around breakfast. Grid prices ease overnight before climbing through the morning.
Charging is planned 02:00–05:00, with battery self-consumption afterwards.

### 05 — Quiet day (low, routine)
Grid prices remain fairly level tomorrow, with a modest solar contribution around midday. Expected use follows a steady pattern.
Battery self-consumption remains planned through most of the day.

### 06 — Quiet night (low, routine)
Expected use easing overnight, with little movement in grid prices.
No grid charging is planned before morning.

### 07 — Modest solar (low, routine)
Solar building gently towards midday, remaining below expected use.
Battery self-consumption is planned through the afternoon.

### 08 — Overnight opportunity (high, opportunity)
Grid prices falling overnight, rising sharply around breakfast.
Grid charging is planned 02:00–04:00 during the cheaper stretch.

### 09 — Afternoon appliance (medium, opportunity)
Solar building through the morning, with surplus expected around midday. Grid prices rise later in the afternoon.
A favourable dishwasher start is 12:30.

### 10 — Low reserves (high, battery)
Reserves near minimum, with little solar expected before noon.
A short grid charge is planned at 05:00, ahead of the morning grid-price rise.

### 11 — Evening transition (high, battery)
Solar fading towards evening as grid prices climb.
Battery preservation is planned until 17:00, then a return to self-consumption.

### 12 — Target shortfall (high, target)
Charging planned through the early morning, but the 07:00 target remains out of reach.
Battery expected at 65%, short of the requested 80%.

### 13 — Target reached (medium, target)
Grid prices easing overnight, with reserves expected to reach 80% by 07:00.
Charging is planned 03:00–06:00, followed by battery preservation until breakfast.

### 14 — Cached solar delayed (medium, reliability)
Grid prices rising towards evening, with battery preservation planned until 17:00.
Solar updates remain delayed; the afternoon outlook uses the earlier forecast.

### 15 — Solar omitted after failure (high, reliability)
Grid prices easing overnight, with charging planned 02:00–04:00 ahead of the morning rise.
Solar forecasts have been missing for an hour, so the plan currently allows no solar contribution.

### 16 — Plan refresh failure (high, reliability)
The retained plan schedules grid charging at 02:00, during the overnight dip in grid prices.
No fresh plan for an hour; the last valid schedule still covers tonight.

### 17 — Expired plan (high, reliability)
Planning remains interrupted; the previous schedule has expired.
No current charging or appliance recommendations are available.

### 18 — Restart validation gap (medium, reliability)
A fresh plan is still awaited after restart.
Stored charging and appliance suggestions are not yet available as current recommendations.

### 19 — Negative grid prices (high, opportunity)
Grid prices dipping below zero before dawn, recovering around breakfast.
Grid charging is planned 03:00–04:30 during the negative-price window.

### 20 — Surplus export (medium, energy_balance)
Solar expected to fill the battery by midday, with surplus feeding the grid through the afternoon.
Little grid use is forecast until evening; a favourable appliance start is 13:15.

### 21 — Evening grid dependence (medium, energy_balance)
Little grid use expected through the afternoon, increasing after sunset as reserves approach minimum.
Grid prices ease later in the evening, with charging planned from 23:00.

### 22 — Charging dominates imports (medium, energy_balance)
Most grid use is expected during the overnight battery top-up. Solar and stored energy cover much of the following daytime use.
Charging is planned 01:30–03:00 during lower grid prices.

### 23 — No battery (medium, opportunity)
Solar surplus expected around midday, followed by increasing grid use towards evening.
A favourable dishwasher start is 12:00, before the evening grid-price rise.

### 24 — No solar configured (medium, opportunity)
Grid prices easing after midnight, with higher expected use around breakfast.
Battery charging is planned 02:00–04:00, followed by self-consumption through the morning.

### 25 — Multiple batteries (medium, battery)
Grid prices falling overnight before the morning rise.
Garage battery charging is planned 02:00–03:00, while the house battery remains on preservation until 06:00.

### 26 — Comfort timing (medium, opportunity)
Solar building towards midday, with grid prices rising later.
Water heating is planned 11:00–13:00, overlapping the expected solar surplus.

### 27 — Partial-day coverage (low, routine)
Grid prices holding fairly steady for the rest of this afternoon.
Battery self-consumption remains planned until the current outlook ends at 18:00.

### 28 — Earlier appliance option (medium, opportunity)
Solar building tomorrow, with stronger production around midday.
The preferred appliance start is 14:15; an earlier option is 10:15.

### 29 — Low-information price-only setup (low, routine)
Grid prices remaining fairly level through the evening, with a slight easing overnight.

### 30 — Strong change supersedes routine story (high, opportunity)
Grid prices rising sharply at 17:00, then easing later tonight.
Battery preservation is planned until the rise, with grid charging deferred until 23:00.

## Previously observed real example (not a live fixture)

HA read around September 23, 2026 23:04 Europe/Copenhagen; accepted plan generated
23:00, 15-minute slots, 36-hour coverage. Battery 10 kWh, minimum 1 kWh; Deye
reported 10.5% and 0 W. Preserve until September 24 03:45; grid charge 03:45–04:15;
projected battery 3.30 kWh at 04:15 and 10 kWh by 14:15. Tomorrow PV approximately
28.08 kWh, base use 19.62 kWh (rounded diagnostic data). Minimum grid price 1.43
kr/kWh during 03:45–04:45; peak 3.59 at 19:45. Hvidevarer preferred start 14:15,
alternative 10:15, duration two hours. All four source statuses OK and plan usable
and validated. These readings support sample 01 but do not establish a precise
whole-day self-sufficiency percentage. Do not access live HA to actuate anything.

## Delegated execution contract

Terra implements code/docs/tests, runs functional pytest at least once (collection
and actual tests execute; passing is not required for handoff), and attempts the
full appropriate verification. Report only once finished, with exact commands,
environment details, test counts/results, modified files, and unresolved issues.
No commits or pushes. Do not send progress messages to the lead.

After Terra finishes and functional test execution is confirmed, the lead starts
Sol to independently run tests and fix failures iteratively, then verify the full
suite and packaging. Sol likewise reports only upon completion. Neither worker
is polled. Respect user changes, AGENTS.md, local test environment conventions,
and the optimizer/HA separation throughout.
