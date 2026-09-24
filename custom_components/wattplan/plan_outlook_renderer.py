"""Localized rendering for language-neutral Plan Outlook models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from .plan_outlook_types import OutlookFact, OutlookModel

VALUE_RANK = {"low": 0, "medium": 1, "high": 2}
DANISH_WEEKDAYS = (
    "mandag",
    "tirsdag",
    "onsdag",
    "torsdag",
    "fredag",
    "lørdag",
    "søndag",
)


def _time(value: datetime) -> str:
    return value.strftime("%H:%M")


def _range_text_english(start: datetime, end: datetime) -> str:
    return f"from {_time(start)} to {_time(end)}"


def _range_text_danish(start: datetime, end: datetime) -> str:
    return f"fra kl. {_time(start)} til kl. {_time(end)}"


def _period_label(value: datetime, *, now: datetime) -> str:
    if value.date() == now.date():
        return "today"
    if value.date() == (now + timedelta(days=1)).date():
        return "tomorrow"
    return f"on {value.strftime('%A')}"


def _period_label_danish(value: datetime, *, now: datetime) -> str:
    if value.date() == now.date():
        return "i dag"
    if value.date() == (now + timedelta(days=1)).date():
        return "i morgen"
    return f"på {DANISH_WEEKDAYS[value.weekday()]}"


def _stable_number(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _values(fact: OutlookFact) -> dict[str, Any]:
    return dict(fact.values)


def _value_time(values: dict[str, Any], key: str, fallback: datetime) -> datetime:
    value = values.get(key)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _duration_text(minutes: int) -> str:
    minutes = max(1, minutes)
    if minutes >= 120:
        return f"{minutes // 60} hours"
    if minutes >= 60:
        return "an hour"
    if minutes == 1:
        return "1 minute"
    return f"{minutes} minutes"


def _duration_text_danish(minutes: int) -> str:
    minutes = max(1, minutes)
    if minutes >= 120:
        return f"{minutes // 60} timer"
    if minutes >= 60:
        return "en time"
    if minutes == 1:
        return "1 minut"
    return f"{minutes} minutter"


def _phrase_variant(
    fact: OutlookFact,
    *,
    model: OutlookModel,
    now: datetime,
    option_count: int,
    variation_seed: str,
    language: str,
) -> int:
    """Choose wording only after the semantic report model is complete."""
    local_hour = (
        now.astimezone(model.start.tzinfo).hour if model.start.tzinfo else now.hour
    )
    period = (
        "morning" if local_hour < 12 else "afternoon" if local_hour < 18 else "evening"
    )
    period_offset = {"morning": 0, "afternoon": 1, "evening": 2}[period]
    seed = (
        f"{model.seed_prefix}:{now.date().isoformat()}:{fact.fact_id}:"
        f"{variation_seed}:{language}"
    )
    return (_stable_number(seed) + period_offset) % option_count


def _select_variant(
    variants: tuple[str, ...],
    *,
    fact: OutlookFact,
    model: OutlookModel,
    now: datetime,
    variation_seed: str,
    language: str,
    forced_variant: int | None,
) -> tuple[str, int]:
    """Return one deterministic wording choice from equivalent variants."""
    variant = (
        forced_variant % len(variants)
        if forced_variant is not None
        else _phrase_variant(
            fact,
            model=model,
            now=now,
            option_count=len(variants),
            variation_seed=variation_seed,
            language=language,
        )
    )
    return variants[variant], variant


def render_fact_english(
    fact: OutlookFact,
    *,
    model: OutlookModel,
    now: datetime,
    variation_seed: str,
    forced_variant: int | None = None,
) -> tuple[str, int]:
    """Render one fact and return its deterministic wording variant."""
    values = _values(fact)
    kind = fact.kind
    variants: tuple[str, ...]
    if kind == "grid_price_rise":
        variants = (
            "Import prices are expected to rise later in the period.",
            "Import prices are likely to be higher later in the period.",
            "Higher import prices are expected toward the end of the period.",
            "The forecast shows import prices increasing later.",
        )
    elif kind == "grid_price_fall":
        variants = (
            "Import prices are expected to fall later in the period.",
            "Import prices are likely to be lower later in the period.",
            "Lower import prices are expected toward the end of the period.",
            "The forecast shows import prices decreasing later.",
        )
    elif kind == "limited_grid_use":
        variants = (
            "The home is expected to use little electricity from the grid.",
            "Electricity drawn from the grid should stay low in this period.",
            "Only a small amount of grid electricity is expected in this period.",
            "The plan expects low electricity use from the grid.",
        )
    elif kind == "flat_grid_prices":
        covered_from = max(fact.start, now)
        label = (
            "for the rest of today"
            if covered_from.date() == now.date() and fact.start < now
            else _period_label(covered_from, now=now)
        )
        variants = (
            f"Import prices are expected to stay about the same {label}.",
            f"Import prices should remain broadly steady {label}.",
            f"Only small import-price changes are expected {label}.",
            f"The import-price forecast shows little movement {label}.",
        )
    elif kind == "negative_grid_price":
        range_text = _range_text_english(fact.start, fact.end)
        variants = (
            f"Forecast import prices are below zero {range_text}.",
            f"Negative import prices are expected {range_text}.",
            f"Import prices are forecast to fall below zero {range_text}.",
            f"The import-price forecast is negative {range_text}.",
        )
    elif kind == "cheaper_grid_prices":
        range_text = _range_text_english(fact.start, fact.end)
        variants = (
            f"Lower import prices are expected {range_text}.",
            f"A lower-price period is forecast {range_text}.",
            f"Import prices should be lower {range_text}.",
            f"The forecast shows a cheaper import period {range_text}.",
        )
    elif kind == "grid_price_swing":
        turn_at = _value_time(values, "turn_at", fact.start)
        if values.get("direction") == "ease_then_rise":
            variants = (
                f"Import prices are expected to fall around {_time(turn_at)}, then rise.",
                f"Around {_time(turn_at)}, import prices turn lower before rising again.",
                f"Import prices dip near {_time(turn_at)} and increase later.",
            )
        else:
            variants = (
                f"Import prices are expected to rise around {_time(turn_at)}, then fall.",
                f"Around {_time(turn_at)}, import prices turn higher before falling again.",
                f"Import prices peak near {_time(turn_at)} and decrease later.",
            )
    elif kind == "solar_surplus":
        peak_at = _value_time(values, "peak_at", fact.start)
        range_text = _range_text_english(fact.start, fact.end)
        variants = (
            f"Solar generation is forecast to exceed expected use {range_text}, peaking around {_time(peak_at)}.",
            f"More solar power than the home is expected to use is forecast {range_text}, with a peak near {_time(peak_at)}.",
            f"Solar output should be higher than expected use {range_text} and peak around {_time(peak_at)}.",
        )
    elif kind == "solar_modest":
        peak_at = _value_time(values, "peak_at", fact.start)
        variants = (
            f"Solar output rises slowly toward {_time(peak_at)} and stays below expected use.",
            f"A small solar peak is expected around {_time(peak_at)}, below the home's expected use.",
            f"Solar generation increases modestly toward {_time(peak_at)} but does not cover expected use.",
        )
    elif kind == "solar_fading":
        variants = (
            f"Solar output is expected to fall after about {_time(fact.start)}.",
            f"Solar generation starts decreasing around {_time(fact.start)}.",
            f"Less solar power is expected after roughly {_time(fact.start)}.",
        )
    elif kind == "low_reserve":
        variants = (
            f"The current charge in {fact.subject} is close to its minimum.",
            f"{fact.subject} currently has little charge above its minimum level.",
            f"{fact.subject}'s battery level is currently near its minimum.",
        )
    elif kind == "grid_charge":
        range_text = _range_text_english(fact.start, fact.end)
        variants = (
            f"{fact.subject} is scheduled to charge from the grid {range_text}.",
            f"The plan charges {fact.subject} from the grid {range_text}.",
            f"Grid charging for {fact.subject} is planned {range_text}.",
            f"{fact.subject} is expected to use grid power for charging {range_text}.",
        )
    elif kind == "battery_preserve":
        range_text = _range_text_english(fact.start, fact.end)
        variants = (
            f"{fact.subject} is scheduled to hold its charge {range_text}.",
            f"The plan keeps the stored energy in {fact.subject} {range_text}.",
            f"{fact.subject} is planned not to discharge {range_text}.",
        )
    elif kind == "battery_self_consume":
        variants = (
            f"{fact.subject} has no scheduled grid-charging or charge-holding periods.",
            f"The plan has no grid charging or charge-holding periods for {fact.subject}.",
            f"{fact.subject} remains in its normal self-use mode throughout the period.",
        )
    elif kind == "battery_full":
        variants = (
            f"{fact.subject} is expected to be fully charged by {_time(fact.start)}.",
            f"{fact.subject} should reach full charge by {_time(fact.start)}.",
            f"The forecast shows {fact.subject} fully charged by {_time(fact.start)}.",
        )
    elif kind == "target_shortfall":
        expected = values.get("expected_percent")
        requested = values.get("requested_percent")
        if isinstance(expected, int | float) and isinstance(requested, int | float):
            variants = (
                f"{fact.subject} is expected to reach {expected:.0f}% by {_time(fact.start)}, below the {requested:.0f}% target.",
                f"By {_time(fact.start)}, {fact.subject} is forecast at {expected:.0f}% instead of the {requested:.0f}% target.",
                f"{fact.subject} may reach only {expected:.0f}% by {_time(fact.start)}; the target is {requested:.0f}%.",
                f"The forecast for {fact.subject} is {expected:.0f}% at {_time(fact.start)}, below the {requested:.0f}% target.",
            )
        else:
            variants = (
                f"{fact.subject} is not expected to reach its target by {_time(fact.start)}.",
                f"The forecast shows {fact.subject} below its target at {_time(fact.start)}.",
                f"{fact.subject} is likely to miss its {_time(fact.start)} target.",
            )
    elif kind == "target_reached":
        requested = values.get("requested_percent")
        if isinstance(requested, int | float):
            variants = (
                f"{fact.subject} is expected to reach the {requested:.0f}% target by {_time(fact.start)}.",
                f"The plan should charge {fact.subject} to {requested:.0f}% by {_time(fact.start)}.",
                f"{fact.subject} is forecast to be at {requested:.0f}% by {_time(fact.start)}.",
            )
        else:
            variants = (
                f"{fact.subject} is expected to reach its target by {_time(fact.start)}.",
                f"The plan should bring {fact.subject} to its target by {_time(fact.start)}.",
                f"{fact.subject} is forecast to meet its {_time(fact.start)} target.",
            )
    elif kind == "comfort_timing":
        range_text = _range_text_english(fact.start, fact.end)
        variants = (
            f"{fact.subject} is scheduled {range_text}, while solar power is expected.",
            f"The plan runs {fact.subject} {range_text} during expected solar generation.",
            f"{fact.subject} is planned {range_text} to overlap with forecast solar power.",
        )
    elif kind == "optional_start":
        alternative = values.get("alternative_at")
        alternative_at = None
        if isinstance(alternative, str):
            try:
                alternative_at = datetime.fromisoformat(alternative)
            except ValueError:
                pass
        if alternative_at is not None:
            variants = (
                f"The best time to start {fact.subject} is {_time(fact.start)}; {_time(alternative_at)} is another option.",
                f"Start {fact.subject} at {_time(fact.start)}, or at {_time(alternative_at)} if needed.",
                f"The plan prefers {_time(fact.start)} for {fact.subject}, with {_time(alternative_at)} as an alternative.",
                f"For {fact.subject}, use {_time(fact.start)} as the first choice and {_time(alternative_at)} as the second.",
            )
        else:
            variants = (
                f"The best time to start {fact.subject} is {_time(fact.start)}.",
                f"Start {fact.subject} at {_time(fact.start)}.",
                f"The plan recommends {_time(fact.start)} for {fact.subject}.",
            )
    elif kind == "grid_export":
        range_text = _range_text_english(fact.start, fact.end)
        variants = (
            f"Extra electricity is expected to be sent to the grid {range_text}.",
            f"The forecast shows electricity being exported to the grid {range_text}.",
            f"The plan expects surplus electricity to flow to the grid {range_text}.",
        )
    elif kind == "heavy_grid_use":
        variants = (
            "The home is expected to draw a lot of electricity from the grid.",
            "Electricity use from the grid is forecast to be high in this period.",
            "The plan expects a high amount of electricity to come from the grid.",
        )
    elif kind == "charging_dominates_imports":
        variants = (
            "Most electricity drawn from the grid is expected to charge batteries.",
            "Battery charging is expected to account for most grid electricity use.",
            "Most imported electricity is forecast to go into battery charging.",
        )
    elif kind == "grid_use_increase":
        variants = (
            "Electricity use from the grid is expected to increase later.",
            "The home is forecast to draw more electricity from the grid later.",
            "Grid electricity use should be higher later in the period.",
        )
    elif kind == "grid_use_decrease":
        variants = (
            "Electricity use from the grid is expected to decrease later.",
            "The home is forecast to draw less electricity from the grid later.",
            "Grid electricity use should be lower later in the period.",
        )
    elif kind == "source_problem":
        elapsed = _duration_text(int(values.get("elapsed_minutes", 1) or 1))
        if fact.subject.endswith("pv"):
            if values.get("stale") is True:
                variants = (
                    f"Solar data has not updated for {elapsed}. The plan uses the previous forecast.",
                    f"Solar updates are delayed by {elapsed}. This plan uses older forecast data.",
                    f"The solar forecast is {elapsed} old, so the plan relies on earlier data.",
                    f"No new solar data has arrived for {elapsed}. The previous forecast remains in use.",
                )
            else:
                variants = (
                    f"No solar forecast has been available for {elapsed}. The plan currently assumes no solar power.",
                    f"Solar forecast data has been missing for {elapsed}, so the plan does not use solar power right now.",
                    f"WattPlan has had no solar forecast for {elapsed}. Solar power is excluded from the current plan.",
                )
        else:
            labels = {
                "source_import_price": "import price data",
                "source_export_price": "export price data",
                "source_usage": "usage forecast data",
            }
            label = labels.get(
                fact.subject,
                fact.subject.removeprefix("source_").replace("_", " "),
            )
            if values.get("stale") is True:
                variants = (
                    f"{label.capitalize()} has not updated for {elapsed}. The plan uses earlier data.",
                    f"Updates to {label} are delayed by {elapsed}. The previous data remains in use.",
                    f"The available {label} is {elapsed} old, so the plan may be less current.",
                )
            else:
                variants = (
                    f"No new {label} has been available for {elapsed}.",
                    f"WattPlan has been unable to get {label} for {elapsed}.",
                    f"Updates to {label} have been unavailable for {elapsed}.",
                )
    elif kind == "plan_refresh_failure":
        elapsed = _duration_text(int(values.get("elapsed_minutes", 1) or 1))
        variants = (
            f"No new plan has been available for {elapsed}. The previous plan is still in use.",
            f"The plan has not refreshed for {elapsed}. WattPlan continues with the previous plan.",
            f"An updated plan has been missing for {elapsed}. The last usable plan remains active.",
            f"WattPlan has used the same plan for {elapsed} because a new one is not ready.",
        )
    elif kind == "plan_unavailable":
        variants = (
            "Planning is unavailable right now. There is no usable plan.",
            "WattPlan cannot create a plan right now, so no schedule is available.",
            "There is currently no plan that WattPlan can use.",
        )
    elif kind == "plan_unusable":
        variants = (
            "Planning is interrupted. The saved plan cannot be used now.",
            "WattPlan is paused because the saved plan is not usable.",
            "The saved plan cannot currently be used, so planning is paused.",
        )
    elif kind == "plan_expired":
        variants = (
            "Planning is interrupted because the previous plan has expired.",
            "The previous plan has expired, so WattPlan is waiting for a new one.",
            "WattPlan cannot continue with the old plan because it has expired.",
        )
    elif kind == "restored_unvalidated":
        variants = (
            "A new plan is not ready after restart.",
            "WattPlan restarted and is waiting for a new usable plan.",
            "Schedule advice remains paused until a new plan is ready after restart.",
        )
    elif kind == "recommendations_unavailable":
        variants = (
            "There are no current recommendations for charging or scheduled devices.",
            "Current charging and device suggestions are unavailable.",
            "WattPlan has no current charging or device recommendations to show.",
        )
    elif kind == "stored_recommendations_unvalidated":
        variants = (
            "Saved charging and device suggestions are not current yet.",
            "Stored suggestions are waiting for a new plan before they can be used.",
            "The saved charging and device advice has not yet been confirmed by a new plan.",
        )
    elif kind == "quiet":
        variants = (
            "No important plan changes are expected for the rest of the period.",
            "The plan is expected to stay much the same for the remaining period.",
            "Nothing significant is expected to change in the rest of the plan.",
            "The remaining plan is expected to stay stable.",
        )
    else:
        variants = (
            kind.replace("_", " ").capitalize() + ".",
            f"Plan update: {kind.replace('_', ' ')}.",
            f"The plan reports {kind.replace('_', ' ')}.",
        )

    return _select_variant(
        variants,
        fact=fact,
        model=model,
        now=now,
        variation_seed=variation_seed,
        language="en",
        forced_variant=forced_variant,
    )


def render_fact_danish(
    fact: OutlookFact,
    *,
    model: OutlookModel,
    now: datetime,
    variation_seed: str,
    forced_variant: int | None = None,
) -> tuple[str, int]:
    """Render one fact in Danish and return its deterministic wording variant."""
    values = _values(fact)
    kind = fact.kind
    variants: tuple[str, ...]
    if kind == "grid_price_rise":
        variants = (
            "Importpriserne forventes at stige senere i perioden.",
            "Importpriserne bliver sandsynligvis højere senere i perioden.",
            "Der forventes højere importpriser hen mod slutningen af perioden.",
            "Prognosen viser stigende importpriser senere i perioden.",
        )
    elif kind == "grid_price_fall":
        variants = (
            "Importpriserne forventes at falde senere i perioden.",
            "Importpriserne bliver sandsynligvis lavere senere i perioden.",
            "Der forventes lavere importpriser hen mod slutningen af perioden.",
            "Prognosen viser faldende importpriser senere i perioden.",
        )
    elif kind == "limited_grid_use":
        variants = (
            "Boligen forventes kun at bruge lidt strøm fra elnettet.",
            "Forbruget fra elnettet forventes at være lavt i perioden.",
            "Der forventes kun en lille mængde strøm fra elnettet i perioden.",
            "Planen forventer et lavt forbrug fra elnettet.",
        )
    elif kind == "flat_grid_prices":
        covered_from = max(fact.start, now)
        label = (
            "resten af dagen"
            if covered_from.date() == now.date() and fact.start < now
            else _period_label_danish(covered_from, now=now)
        )
        variants = (
            f"Importpriserne forventes at være omtrent uændrede {label}.",
            f"Importpriserne holder sig overordnet stabile {label}.",
            f"Der forventes kun små ændringer i importpriserne {label}.",
            f"Prognosen viser kun få udsving i importpriserne {label}.",
        )
    elif kind == "negative_grid_price":
        range_text = _range_text_danish(fact.start, fact.end)
        variants = (
            f"De forventede importpriser er under nul {range_text}.",
            f"Der forventes negative importpriser {range_text}.",
            f"Importpriserne forventes at falde under nul {range_text}.",
            f"Prognosen viser negative importpriser {range_text}.",
        )
    elif kind == "cheaper_grid_prices":
        range_text = _range_text_danish(fact.start, fact.end)
        variants = (
            f"Der forventes lavere importpriser {range_text}.",
            f"En periode med lavere importpriser forventes {range_text}.",
            f"Importpriserne bliver sandsynligvis lavere {range_text}.",
            f"Prognosen viser en billigere importperiode {range_text}.",
        )
    elif kind == "grid_price_swing":
        turn_at = _value_time(values, "turn_at", fact.start)
        if values.get("direction") == "ease_then_rise":
            variants = (
                f"Importpriserne forventes at falde omkring kl. {_time(turn_at)} og stige senere.",
                f"Omkring kl. {_time(turn_at)} bliver importpriserne lavere, før de stiger igen.",
                f"Importpriserne dykker omkring kl. {_time(turn_at)} og stiger senere.",
            )
        else:
            variants = (
                f"Importpriserne forventes at stige omkring kl. {_time(turn_at)} og falde senere.",
                f"Omkring kl. {_time(turn_at)} bliver importpriserne højere, før de falder igen.",
                f"Importpriserne topper omkring kl. {_time(turn_at)} og falder senere.",
            )
    elif kind == "solar_surplus":
        peak_at = _value_time(values, "peak_at", fact.start)
        range_text = _range_text_danish(fact.start, fact.end)
        variants = (
            f"Solproduktionen forventes at være større end forbruget {range_text} og toppe omkring kl. {_time(peak_at)}.",
            f"Der forventes mere solstrøm end boligen bruger {range_text}, med en top omkring kl. {_time(peak_at)}.",
            f"Solproduktionen bør overstige det forventede forbrug {range_text} og toppe omkring kl. {_time(peak_at)}.",
        )
    elif kind == "solar_modest":
        peak_at = _value_time(values, "peak_at", fact.start)
        variants = (
            f"Solproduktionen stiger langsomt frem mod kl. {_time(peak_at)} og forbliver under det forventede forbrug.",
            f"Der forventes en mindre soltop omkring kl. {_time(peak_at)}, som ikke dækker boligens forventede forbrug.",
            f"Solproduktionen vokser lidt frem mod kl. {_time(peak_at)}, men er lavere end det forventede forbrug.",
        )
    elif kind == "solar_fading":
        variants = (
            f"Solproduktionen forventes at falde efter cirka kl. {_time(fact.start)}.",
            f"Solproduktionen begynder at falde omkring kl. {_time(fact.start)}.",
            f"Der forventes mindre solstrøm efter cirka kl. {_time(fact.start)}.",
        )
    elif kind == "low_reserve":
        variants = (
            f"Den aktuelle opladning i {fact.subject} er tæt på minimum.",
            f"{fact.subject} har kun lidt opladning over minimumsniveauet lige nu.",
            f"Batteriniveauet i {fact.subject} er i øjeblikket tæt på minimum.",
        )
    elif kind == "grid_charge":
        range_text = _range_text_danish(fact.start, fact.end)
        variants = (
            f"{fact.subject} er planlagt til opladning fra elnettet {range_text}.",
            f"Planen oplader {fact.subject} fra elnettet {range_text}.",
            f"Opladning fra elnettet er planlagt for {fact.subject} {range_text}.",
            f"{fact.subject} forventes at bruge strøm fra elnettet til opladning {range_text}.",
        )
    elif kind == "battery_preserve":
        range_text = _range_text_danish(fact.start, fact.end)
        variants = (
            f"{fact.subject} er planlagt til at holde på strømmen {range_text}.",
            f"Planen gemmer energien i {fact.subject} {range_text}.",
            f"{fact.subject} er planlagt til ikke at aflade {range_text}.",
        )
    elif kind == "battery_self_consume":
        variants = (
            f"{fact.subject} har ingen planlagte perioder med opladning fra elnettet eller fastholdt opladning.",
            f"Planen har ingen perioder med netopladning eller fastholdt opladning for {fact.subject}.",
            f"{fact.subject} forbliver i normal egenforbrugstilstand gennem perioden.",
        )
    elif kind == "battery_full":
        variants = (
            f"{fact.subject} forventes at være fuldt opladet senest kl. {_time(fact.start)}.",
            f"{fact.subject} bør nå fuld opladning senest kl. {_time(fact.start)}.",
            f"Prognosen viser {fact.subject} fuldt opladet kl. {_time(fact.start)}.",
        )
    elif kind == "target_shortfall":
        expected = values.get("expected_percent")
        requested = values.get("requested_percent")
        if isinstance(expected, int | float) and isinstance(requested, int | float):
            variants = (
                f"{fact.subject} forventes kun at nå {expected:.0f} % senest kl. {_time(fact.start)}; målet er {requested:.0f} %.",
                f"Kl. {_time(fact.start)} forventes {fact.subject} at være på {expected:.0f} % i stedet for målet på {requested:.0f} %.",
                f"{fact.subject} når muligvis kun {expected:.0f} % kl. {_time(fact.start)}, under målet på {requested:.0f} %.",
                f"Prognosen for {fact.subject} er {expected:.0f} % kl. {_time(fact.start)}, under målet på {requested:.0f} %.",
            )
        else:
            variants = (
                f"{fact.subject} forventes ikke at nå sit mål senest kl. {_time(fact.start)}.",
                f"Prognosen viser {fact.subject} under målet kl. {_time(fact.start)}.",
                f"{fact.subject} når sandsynligvis ikke målet kl. {_time(fact.start)}.",
            )
    elif kind == "target_reached":
        requested = values.get("requested_percent")
        if isinstance(requested, int | float):
            variants = (
                f"{fact.subject} forventes at nå målet på {requested:.0f} % senest kl. {_time(fact.start)}.",
                f"Planen bør oplade {fact.subject} til {requested:.0f} % senest kl. {_time(fact.start)}.",
                f"{fact.subject} forventes at være på {requested:.0f} % kl. {_time(fact.start)}.",
            )
        else:
            variants = (
                f"{fact.subject} forventes at nå sit mål senest kl. {_time(fact.start)}.",
                f"Planen bør bringe {fact.subject} op på målet senest kl. {_time(fact.start)}.",
                f"{fact.subject} forventes at opfylde målet kl. {_time(fact.start)}.",
            )
    elif kind == "comfort_timing":
        range_text = _range_text_danish(fact.start, fact.end)
        variants = (
            f"{fact.subject} er planlagt {range_text}, mens der forventes solproduktion.",
            f"Planen kører {fact.subject} {range_text}, mens solen forventes at producere strøm.",
            f"{fact.subject} er planlagt {range_text} for at overlappe med den forventede solproduktion.",
        )
    elif kind == "optional_start":
        alternative = values.get("alternative_at")
        alternative_at = None
        if isinstance(alternative, str):
            try:
                alternative_at = datetime.fromisoformat(alternative)
            except ValueError:
                pass
        if alternative_at is not None:
            variants = (
                f"Det bedste tidspunkt at starte {fact.subject} er kl. {_time(fact.start)}; kl. {_time(alternative_at)} er et andet valg.",
                f"Start {fact.subject} kl. {_time(fact.start)} eller kl. {_time(alternative_at)}, hvis det passer bedre.",
                f"Planen foretrækker kl. {_time(fact.start)} for {fact.subject}, med kl. {_time(alternative_at)} som alternativ.",
                f"For {fact.subject} er kl. {_time(fact.start)} første valg og kl. {_time(alternative_at)} andet valg.",
            )
        else:
            variants = (
                f"Det bedste tidspunkt at starte {fact.subject} er kl. {_time(fact.start)}.",
                f"Start {fact.subject} kl. {_time(fact.start)}.",
                f"Planen anbefaler kl. {_time(fact.start)} for {fact.subject}.",
            )
    elif kind == "grid_export":
        range_text = _range_text_danish(fact.start, fact.end)
        variants = (
            f"Overskydende strøm forventes sendt ud på elnettet {range_text}.",
            f"Prognosen viser eksport af strøm til elnettet {range_text}.",
            f"Planen forventer, at overskydende strøm sendes til elnettet {range_text}.",
        )
    elif kind == "heavy_grid_use":
        variants = (
            "Boligen forventes at hente meget strøm fra elnettet.",
            "Forbruget af strøm fra elnettet forventes at være højt i perioden.",
            "Planen forventer, at en stor del af strømmen kommer fra elnettet.",
        )
    elif kind == "charging_dominates_imports":
        variants = (
            "Det meste af strømmen fra elnettet forventes at gå til batteriopladning.",
            "Batteriopladning forventes at stå for det meste af forbruget fra elnettet.",
            "Størstedelen af den importerede strøm forventes brugt til at oplade batterier.",
        )
    elif kind == "grid_use_increase":
        variants = (
            "Forbruget af strøm fra elnettet forventes at stige senere.",
            "Boligen forventes at hente mere strøm fra elnettet senere.",
            "Forbruget fra elnettet bør være højere senere i perioden.",
        )
    elif kind == "grid_use_decrease":
        variants = (
            "Forbruget af strøm fra elnettet forventes at falde senere.",
            "Boligen forventes at hente mindre strøm fra elnettet senere.",
            "Forbruget fra elnettet bør være lavere senere i perioden.",
        )
    elif kind == "source_problem":
        elapsed = _duration_text_danish(int(values.get("elapsed_minutes", 1) or 1))
        if fact.subject.endswith("pv"):
            if values.get("stale") is True:
                variants = (
                    f"Soldata er ikke blevet opdateret i {elapsed}. Planen bruger den tidligere prognose.",
                    f"Solopdateringer er forsinkede med {elapsed}. Planen bruger ældre prognosedata.",
                    f"Solprognosen er {elapsed} gammel, så planen bygger på tidligere data.",
                    f"Der er ikke kommet nye soldata i {elapsed}. Den tidligere prognose bruges stadig.",
                )
            else:
                variants = (
                    f"Der har ikke været en solprognose i {elapsed}. Planen regner derfor ikke med solenergi lige nu.",
                    f"Solprognosen har manglet i {elapsed}, så planen ikke bruger solenergi i øjeblikket.",
                    f"WattPlan har manglet en solprognose i {elapsed}. Solenergi er ikke med i den aktuelle plan.",
                )
        else:
            source_labels = {
                "source_import_price": "data om importpriser",
                "source_export_price": "data om eksportpriser",
                "source_usage": "forbrugsprognosen",
            }
            label = source_labels.get(
                fact.subject,
                fact.subject.removeprefix("source_").replace("_", " "),
            )
            if values.get("stale") is True:
                variants = (
                    f"{label.capitalize()} er ikke blevet opdateret i {elapsed}. Planen bruger tidligere data.",
                    f"Opdateringer af {label} er forsinkede med {elapsed}. De tidligere data bruges stadig.",
                    f"De tilgængelige {label} er {elapsed} gamle, så planen kan være mindre aktuel.",
                )
            else:
                variants = (
                    f"Der har ikke været nye {label} i {elapsed}.",
                    f"WattPlan har ikke kunnet hente {label} i {elapsed}.",
                    f"Opdateringer af {label} har været utilgængelige i {elapsed}.",
                )
    elif kind == "plan_refresh_failure":
        elapsed = _duration_text_danish(int(values.get("elapsed_minutes", 1) or 1))
        variants = (
            f"Der har ikke været en ny plan i {elapsed}. Den tidligere plan bruges stadig.",
            f"Planen er ikke blevet opdateret i {elapsed}. WattPlan fortsætter med den tidligere plan.",
            f"En opdateret plan har manglet i {elapsed}. Den seneste brugbare plan er stadig aktiv.",
            f"WattPlan har brugt den samme plan i {elapsed}, fordi en ny plan ikke er klar.",
        )
    elif kind == "plan_unavailable":
        variants = (
            "Planlægning er ikke tilgængelig lige nu. Der er ingen plan, der kan bruges.",
            "WattPlan kan ikke lave en plan lige nu, så der er ingen tidsplan.",
            "Der er i øjeblikket ingen plan, som WattPlan kan bruge.",
        )
    elif kind == "plan_unusable":
        variants = (
            "Planlægningen er afbrudt. Den gemte plan kan ikke bruges nu.",
            "WattPlan er sat på pause, fordi den gemte plan ikke kan bruges.",
            "Den gemte plan kan ikke bruges lige nu, så planlægningen er sat på pause.",
        )
    elif kind == "plan_expired":
        variants = (
            "Planlægningen er afbrudt, fordi den tidligere plan er udløbet.",
            "Den tidligere plan er udløbet, så WattPlan venter på en ny.",
            "WattPlan kan ikke fortsætte med den gamle plan, fordi den er udløbet.",
        )
    elif kind == "restored_unvalidated":
        variants = (
            "En ny plan er ikke klar efter genstart.",
            "WattPlan er genstartet og venter på en ny plan, der kan bruges.",
            "Planråd er sat på pause, indtil en ny plan er klar efter genstart.",
        )
    elif kind == "recommendations_unavailable":
        variants = (
            "Der er ingen aktuelle anbefalinger til opladning eller planlagte enheder.",
            "Aktuelle forslag til opladning og enheder er ikke tilgængelige.",
            "WattPlan har ingen aktuelle anbefalinger til opladning eller enheder.",
        )
    elif kind == "stored_recommendations_unvalidated":
        variants = (
            "Gemte forslag til opladning og enheder er endnu ikke aktuelle.",
            "De gemte forslag venter på en ny plan, før de kan bruges.",
            "De gemte råd om opladning og enheder er endnu ikke bekræftet af en ny plan.",
        )
    elif kind == "quiet":
        variants = (
            "Der forventes ingen vigtige ændringer i planen resten af perioden.",
            "Planen forventes at være stort set uændret i resten af perioden.",
            "Der forventes ikke større ændringer i resten af planen.",
            "Den resterende plan forventes at være stabil.",
        )
    else:
        readable_kind = kind.replace("_", " ")
        variants = (
            readable_kind.capitalize() + ".",
            f"Planopdatering: {readable_kind}.",
            f"Planen viser {readable_kind}.",
        )

    return _select_variant(
        variants,
        fact=fact,
        model=model,
        now=now,
        variation_seed=variation_seed,
        language="da",
        forced_variant=forced_variant,
    )


def _semantic_fact_payload(fact: OutlookFact) -> dict[str, Any]:
    """Return only values that define the meaning of the rendered statement."""
    payload: dict[str, Any] = {
        "id": fact.fact_id,
        "kind": fact.kind,
        "topic": fact.topic,
        "information_value": fact.information_value,
        "subject": fact.subject,
        "values": dict(fact.values),
    }
    interval_kinds = {
        "negative_grid_price",
        "cheaper_grid_prices",
        "solar_surplus",
        "grid_charge",
        "battery_preserve",
        "comfort_timing",
        "grid_export",
    }
    point_kinds = {
        "solar_fading",
        "battery_full",
        "target_shortfall",
        "target_reached",
        "optional_start",
    }
    if fact.kind in interval_kinds:
        payload["start"] = fact.start.isoformat()
        payload["end"] = fact.end.isoformat()
    elif fact.kind in point_kinds:
        payload["start"] = fact.start.isoformat()
    return payload


def _semantic_id(selected: tuple[OutlookFact, ...], *, information_value: str) -> str:
    payload = [_semantic_fact_payload(fact) for fact in selected]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:10]
    return f"{selected[0].kind}_{information_value}_{digest}"


def render_plan_outlook(
    model: OutlookModel,
    *,
    now: datetime,
    language: str = "en",
    previous_outlook: dict[str, Any] | None = None,
    variation_seed: str | None = None,
) -> dict[str, Any]:
    """Render one selected model without changing its semantic content."""
    selected = model.selected_facts
    if not selected:
        return {}

    language = language if language in {"en", "da"} else "en"
    information_value = max(
        (fact.information_value for fact in selected), key=VALUE_RANK.__getitem__
    )
    semantic_id = _semantic_id(selected, information_value=information_value)
    previous_variants: dict[str, Any] = {}
    if (
        isinstance(previous_outlook, dict)
        and previous_outlook.get("semantic_id") == semantic_id
        and previous_outlook.get("language") == language
        and isinstance(previous_outlook.get("_render_variants"), dict)
    ):
        previous_variants = previous_outlook["_render_variants"]

    rendered: list[str] = []
    variants: dict[str, int] = {}
    effective_seed = variation_seed or semantic_id
    render_fact = render_fact_danish if language == "da" else render_fact_english
    for fact in selected:
        previous_variant = previous_variants.get(fact.fact_id)
        text, variant = render_fact(
            fact,
            model=model,
            now=now,
            variation_seed=effective_seed,
            forced_variant=(
                previous_variant if isinstance(previous_variant, int) else None
            ),
        )
        rendered.append(text)
        variants[fact.fact_id] = variant

    line_1 = rendered[0]
    line_2 = " ".join(rendered[1:])
    topic_fact = next(
        (fact for fact in selected if fact.topic == "reliability"),
        max(selected, key=lambda fact: VALUE_RANK[fact.information_value]),
    )
    report_payload = {
        "language": language,
        "facts": [
            {
                "id": fact.fact_id,
                "kind": fact.kind,
                "topic": fact.topic,
                "information_value": fact.information_value,
                "subject": fact.subject,
                "values": dict(fact.values),
            }
            for fact in selected
        ],
        "variants": variants,
        "rendered": rendered,
    }
    digest = hashlib.sha256(
        json.dumps(report_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:10]
    report_id = f"{selected[0].kind}_{information_value}_{digest}"
    return {
        "report_id": report_id,
        "semantic_id": semantic_id,
        "headline": line_1,
        "line_1": line_1,
        "line_2": line_2,
        "text": " ".join(rendered),
        "language": language,
        "information_value": information_value,
        "topic": topic_fact.topic,
        "selected_facts": [fact.fact_id for fact in selected],
        "fact_details": [
            {
                "id": fact.fact_id,
                "basis": fact.basis,
                "start": fact.start.isoformat(),
                "end": fact.end.isoformat(),
                "required_inputs": list(fact.required_inputs),
                "values": dict(fact.values),
            }
            for fact in selected
        ],
        "basis": sorted({fact.basis for fact in selected}),
        "covered_start": max(model.start, now).isoformat(),
        "covered_end": model.horizon_end.isoformat(),
        "valid_until": model.horizon_end.isoformat(),
        "plan_created_at": (
            model.plan_created_at.isoformat()
            if model.plan_created_at is not None
            else None
        ),
        "_render_variants": variants,
    }
