outlook-grid-price-rise = { $variant ->
    [0] Grid prices rise later { $period_text }.
    [1] Grid prices run higher later { $period_text }.
    [2] Higher grid prices apply later { $period_text }.
   *[other] Grid prices will increase later.
}
outlook-grid-price-fall = { $variant ->
    [0] Grid prices fall later { $period_text }.
    [1] Grid prices run lower later { $period_text }.
    [2] Lower grid prices apply later { $period_text }.
   *[other] Grid prices will fall later.
}
outlook-limited-grid-use = { $variant ->
    [0] Little grid use is expected { $period_text }.
    [1] Grid use should stay low { $period_text }.
    [2] Only a small amount of grid electricity is expected { $period_text }.
   *[other] Low grid use is expected { $period_text }.
}
outlook-flat-grid-prices =
    { $variant ->
       [0] Grid prices stay broadly steady { $period_text }.
       [1] Grid prices show little movement { $period_text }.
       [2] Grid-price changes stay small { $period_text }.
      *[3] Grid prices remain fairly level { $period_text }.
    }
outlook-negative-grid-price = { $variant ->
    [0] Grid prices are below zero { $range }.
    [1] Grid prices are negative { $range }.
    [2] Grid prices fall below zero { $range }.
   *[other] Grid prices stay below zero { $range }.
}
outlook-cheaper-grid-prices = { $variant ->
    [0] Grid prices are lower { $range }.
    [1] Grid prices dip { $range }.
    [2] Grid prices sit lower { $range }.
   *[other] Cheaper grid prices run { $range }.
}
outlook-solar-surplus = { $variant ->
    [0] Solar generation is forecast to exceed expected use { $range }, peaking around { $peak_at }.
    [1] More solar power than expected use is forecast { $range }, with a peak near { $peak_at }.
    [2] Solar output should be higher than expected use { $range } and peak around { $peak_at }.
   *[other] Solar is expected to cover more than the load { $range }, peaking near { $peak_at }.
}
outlook-solar-modest =
    { $variant ->
       [0] Solar output rises slowly toward { $peak_at } and stays below expected use.
       [1] A small solar peak is expected around { $peak_at }, below expected use.
       [2] Solar generation builds modestly toward { $peak_at } but does not cover expected use.
      *[other] Solar is expected to remain below the load despite a small peak near { $peak_at }.
    }
outlook-solar-fading =
    { $variant ->
       [0] Solar output is expected to fall after about { $start }.
       [1] Solar generation starts decreasing around { $start }.
       [2] Less solar power is expected after roughly { $start }.
      *[other] Solar generation eases after { $start }.
    }
outlook-low-reserve =
    { $variant ->
       [0] The current charge in { $subject } is close to its minimum.
       [1] { $subject } currently has little charge above its minimum level.
       [2] { $subject } is currently near its minimum battery level.
       *[other] { $subject } has limited reserves above its minimum.
    }
outlook-low-reserve-solo =
    { $variant ->
       [0] Battery charge is close to its minimum.
       [1] Little charge remains above the minimum level.
       [2] Reserves are near the minimum battery level.
      *[other] Limited reserves remain above minimum.
    }
outlook-grid-charge =
    { $variant ->
       [0] { $subject } is scheduled to charge from the grid { $range }.
       [1] { $subject } charges from the grid { $range }.
       [2] Grid charging for { $subject } is planned { $range }.
      *[other] { $subject } is expected to use grid power for charging { $range }.
    }
outlook-grid-charge-solo =
    { $variant ->
       [0] Grid charging runs { $range }.
       [1] Charging from the grid is scheduled { $range }.
       [2] The battery charges from the grid { $range }.
      *[other] Grid power is used for charging { $range }.
    }
outlook-battery-preserve =
    { $variant ->
       [0] { $subject } is scheduled to hold its charge { $range }.
       [1] The stored energy in { $subject } is kept { $range }.
       [2] { $subject } is planned not to discharge { $range }.
      *[other] { $subject } is preserved { $range }.
    }
outlook-battery-preserve-solo =
    { $variant ->
       [0] Stored energy is kept { $range }.
       [1] Charge is held { $range }.
       [2] Discharging is paused { $range }.
      *[other] Stored charge is preserved { $range }.
    }
outlook-battery-self-consume =
    { $variant ->
       [0] { $subject } has no scheduled grid-charging or charge-holding.
       [1] No grid charging or charge-holding for { $subject }.
       [2] { $subject } is in normal use.
      *[other] { $subject } is planned for normal self-consumption.
    }
outlook-battery-self-consume-solo =
    { $variant ->
       [0] No grid charging or charge-holding is scheduled.
       [1] Normal self-consumption continues.
       [2] The battery stays in use.
      *[other] Self-consumption is planned.
    }
outlook-battery-full =
    { $variant ->
       [0] { $subject } is expected to be fully charged by { $start }.
       [1] { $subject } should reach full charge by { $start }.
       [2] The forecast shows { $subject } fully charged by { $start }.
      *[other] { $subject } is forecast to fill by { $start }.
    }
outlook-battery-full-solo =
    { $variant ->
       [0] The battery is expected to be fully charged by { $start }.
       [1] Full charge should be reached by { $start }.
       [2] The forecast shows a full battery by { $start }.
      *[other] A full battery is forecast by { $start }.
    }
outlook-target-shortfall-known-solo =
    { $variant ->
       [0] Only { $expected }% is expected by { $start }, below the { $requested }% target.
       [1] By { $start }, the forecast is { $expected }% instead of the { $requested }% target.
       [2] The battery may reach only { $expected }% by { $start }; the target is { $requested }%.
      *[other] The forecast is { $expected }% at { $start }, below the { $requested }% target.
    }
outlook-target-shortfall-missing-solo =
    { $variant ->
       [0] The target is not expected to be met by { $start }.
       [1] The forecast leaves the battery below its target at { $start }.
       [2] The requested charge level may be missed by { $start }.
      *[other] The battery may miss its target by { $start }.
    }
outlook-target-reached-known-solo =
    { $variant ->
       [0] The { $requested }% target is expected by { $start }.
       [1] Charging to { $requested }% should complete by { $start }.
       [2] The forecast has the battery at { $requested }% by { $start }.
      *[other] The battery should reach { $requested }% by { $start }.
    }
outlook-target-reached-missing-solo =
    { $variant ->
       [0] The target is expected to be met by { $start }.
       [1] The requested charge level should be reached by { $start }.
       [2] The battery should be at its target by { $start }.
      *[other] The target should be met by { $start }.
    }
outlook-comfort-timing =
    { $variant ->
       [0] { $subject } is scheduled { $range }, while solar power is expected.
       [1] { $subject } runs { $range } during expected solar generation.
       [2] { $subject } is planned { $range } to overlap with forecast solar power.
      *[other] Expected solar generation overlaps { $subject } { $range }.
    }
outlook-grid-export =
    { $variant ->
       [0] Extra electricity is expected to be sent to the grid { $range }.
       [1] The forecast shows electricity being exported to the grid { $range }.
       [2] Surplus electricity is expected to flow to the grid { $range }.
      *[other] Grid export is expected { $range }.
    }
outlook-heavy-grid-use =
    { $variant ->
       [0] Heavy grid use is expected { $period_text }.
       [1] Electricity use from the grid is forecast to be high { $period_text }.
       [2] A high amount of electricity is expected to come from the grid { $period_text }.
      *[other] The home is expected to draw heavily from the grid { $period_text }.
    }
outlook-charging-dominates-imports =
    { $variant ->
       [0] Most grid use is expected to charge batteries.
       [1] Battery charging is expected to account for most grid electricity use.
       [2] Most imported electricity is forecast to go into battery charging.
      *[other] Grid use is concentrated in battery charging.
    }
outlook-grid-use-increase =
    { $variant ->
       [0] Grid use is expected to increase later.
       [1] The home is forecast to draw more electricity from the grid later.
       [2] Grid electricity use should be higher later { $period_text }.
      *[other] More grid use is expected later.
    }
outlook-grid-use-decrease =
    { $variant ->
       [0] Grid use is expected to decrease later.
       [1] The home is forecast to draw less electricity from the grid later.
       [2] Grid electricity use should be lower later { $period_text }.
      *[other] Less grid use is expected later.
    }
outlook-plan-refresh-failure =
    { $variant ->
    [0] No new plan has been available for { $duration }. The previous plan is still in use.
    [1] The plan has not refreshed for { $duration }. WattPlan continues with the previous plan.
    [2] An updated plan has been missing for { $duration }. The last usable plan remains active.
   *[3] WattPlan has used the same plan for { $duration } because a new one is not ready.
    }
outlook-plan-unavailable =
    { $variant ->
       [0] Planning is unavailable right now. There is no usable plan.
       [1] WattPlan cannot create a plan right now, so no schedule is available.
       [2] There is currently no plan that WattPlan can use.
      *[other] No current plan is available.
    }
outlook-plan-unusable =
    { $variant ->
       [0] Planning is interrupted. The saved plan cannot be used now.
       [1] WattPlan is paused because the saved plan is not usable.
       [2] The saved plan cannot currently be used, so planning is paused.
   *[other] The saved plan cannot be used currently.
    }
outlook-plan-expired =
    { $variant ->
       [0] Planning is interrupted because the previous plan has expired.
       [1] The previous plan has expired, so WattPlan is waiting for a new one.
       [2] WattPlan cannot continue with the old plan because it has expired.
      *[other] The plan has expired and is awaiting replacement.
    }
outlook-restored-unvalidated =
    { $variant ->
       [0] A new plan is not ready after restart.
       [1] WattPlan restarted and is waiting for a new usable plan.
       [2] Schedule advice remains paused until a new plan is ready after restart.
      *[other] Recommendations await a fresh plan after restart.
    }
outlook-recommendations-unavailable =
    { $variant ->
       [0] There are no current recommendations for charging or scheduled devices.
       [1] Current charging and device suggestions are unavailable.
       [2] WattPlan has no current charging or device recommendations to show.
      *[other] Current recommendations are unavailable.
    }
outlook-stored-recommendations-unvalidated =
    { $variant ->
       [0] Saved charging and device suggestions are not current yet.
       [1] Stored suggestions are waiting for a new plan before they can be used.
       [2] The saved charging and device advice has not yet been confirmed by a new plan.
      *[other] Stored recommendations await validation.
    }
outlook-quiet =
    { $variant ->
       [0] No important plan changes are expected { $period_text }.
       [1] The plan is expected to stay much the same { $period_text }.
       [2] Nothing significant is expected to change in the rest of the plan.
      *[other] The remaining plan is expected to stay stable.
    }

outlook-duration = { $unit ->
    [hour] { $value ->
        [1] an hour
       *[other] { $value } hours
    }
   *[minute] { $value ->
        [1] one minute
       *[other] { $value } minutes
    }
}

outlook-period = { $period ->
    [rest-of-today] for the rest of today
    [whole-forecast] across the whole forecast
    [today] today
    [tomorrow] tomorrow
   *[weekday] on { $weekday ->
        [0] Monday
        [1] Tuesday
        [2] Wednesday
        [3] Thursday
        [4] Friday
        [5] Saturday
       *[6] Sunday
    }
}

outlook-time = { $period ->
    [today] { $time } today
    [tomorrow] { $time } tomorrow
   *[weekday] { $time } on { $weekday ->
        [0] Monday
        [1] Tuesday
        [2] Wednesday
        [3] Thursday
        [4] Friday
        [5] Saturday
       *[6] Sunday
    }
}

outlook-time-range = from { $start } to { $end }

outlook-time-range-same-day = { $period ->
    [today] from { $start } to { $end } today
    [tomorrow] from { $start } to { $end } tomorrow
   *[weekday] from { $start } to { $end } on { $weekday ->
        [0] Monday
        [1] Tuesday
        [2] Wednesday
        [3] Thursday
        [4] Friday
        [5] Saturday
       *[6] Sunday
    }
}

outlook-grid-price-swing-ease-then-rise = { $variant ->
    [0] Grid prices ease before rising again around { $turn_at }.
    [1] Grid prices fall, then turn upward near { $turn_at }.
   *[2] Lower grid prices come before a rise around { $turn_at }.
}
outlook-grid-price-swing-rise-then-ease = { $variant ->
    [0] Grid prices rise before easing around { $turn_at }.
    [1] Grid prices climb, then turn downward near { $turn_at }.
   *[2] Higher grid prices come before a fall around { $turn_at }.
}
outlook-optional-start-single = { $variant ->
    [0] The best time to start { $subject } is { $start }.
    [1] Start { $subject } at { $start }.
   *[2] { $start } is recommended for { $subject }.
}
outlook-optional-start-alternative = { $variant ->
    [0] Start { $subject } at { $start }; { $alternative_at } is the alternative.
    [1] { $start } is preferred for { $subject }, with { $alternative_at } also available.
    [2] { $subject } is best started at { $start }; an alternative is { $alternative_at }.
   *[3] For { $subject }, use { $start } as the first choice and { $alternative_at } as the second.
}
outlook-target-shortfall-known = { $variant ->
    [0] { $subject } is expected to reach { $expected }% by { $start }, below the { $requested }% target.
    [1] By { $start }, { $subject } is forecast at { $expected }% instead of the { $requested }% target.
    [2] { $subject } may reach only { $expected }% by { $start }; the target is { $requested }%.
   *[3] The forecast for { $subject } is { $expected }% at { $start }, below the { $requested }% target.
}
outlook-target-shortfall-missing = { $variant ->
    [0] { $subject } is not expected to meet its target by { $start }.
    [1] The forecast leaves { $subject } below its target at { $start }.
   *[2] { $subject } may miss the requested charge level by { $start }.
}
outlook-target-reached-known = { $variant ->
    [0] { $subject } is expected to reach the { $requested }% target by { $start }.
    [1] { $subject } should charge to { $requested }% by { $start }.
   *[2] { $subject } is forecast to be at { $requested }% by { $start }.
}
outlook-target-reached-missing = { $variant ->
    [0] { $subject } is expected to meet its target by { $start }.
    [1] { $subject } is expected to reach its requested charge level by { $start }.
   *[2] { $subject } should be at its target by { $start }.
}
outlook-source-problem-import-price-stale = { $variant ->
    [0] Import price data is stale after { $duration }; the last values are still being used.
    [1] Import price updates have been delayed for { $duration }, so WattPlan retains earlier prices.
   *[2] Older import price data is in use after { $duration }.
}
outlook-source-problem-import-price-unavailable = { $variant ->
    [0] Import price data has been unavailable for { $duration }; a current price forecast cannot be trusted.
    [1] No usable import price update has arrived for { $duration }.
   *[2] Import price data is unavailable after { $duration }, limiting price-based advice.
}
outlook-source-problem-export-price-stale = { $variant ->
    [0] Export price data is stale after { $duration }; earlier values remain in use.
    [1] Export price updates have been delayed for { $duration }.
   *[2] Older export price data is in use after { $duration }.
}
outlook-source-problem-export-price-unavailable = { $variant ->
    [0] Export price data has been unavailable for { $duration }; export value is uncertain.
    [1] No usable export price update has arrived for { $duration }.
   *[2] Export price data is unavailable after { $duration }.
}
outlook-source-problem-usage-stale = { $variant ->
    [0] Usage data is stale after { $duration }; the last forecast remains in use.
    [1] Usage updates have been delayed for { $duration }.
   *[2] Older usage data is in use after { $duration }.
}
outlook-source-problem-usage-unavailable = { $variant ->
    [0] Usage data has been unavailable for { $duration }; demand-aware advice is limited.
    [1] No usable usage update has arrived for { $duration }.
   *[2] Usage data is unavailable after { $duration }.
}
outlook-source-problem-pv-stale = { $variant ->
    [0] Solar data is stale after { $duration }; the last forecast remains in use.
    [1] Solar updates have been delayed for { $duration }.
    [2] An earlier solar forecast is in use after { $duration }.
   *[3] No new solar data has arrived for { $duration }; the previous forecast remains in use.
}
outlook-source-problem-pv-unavailable = { $variant ->
    [0] Solar data has been unavailable for { $duration }; solar-based advice is limited.
    [1] No usable solar update has arrived for { $duration }.
   *[2] Solar data is unavailable after { $duration }.
}
