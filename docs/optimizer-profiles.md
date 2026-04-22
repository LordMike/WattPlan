# WattPlan Optimizer Profiles

This page describes the user-facing optimizer profiles exposed by the Home Assistant integration.

These profiles are integration presets. Internally, the optimizer still operates on numeric controls such as throughput cost, action deadband, and mode-switch cost. The integration translates the selected profile into those numeric values before calling the optimizer.

## When to use each profile

### Aggressive

Use this when savings are the main goal and you are comfortable with the battery moving more often.

Typical behavior:
- Takes more charging and discharging opportunities when they look economically useful
- Is more willing to make smaller battery moves
- Can produce more active battery schedules

This is usually a good fit when:
- You want WattPlan to chase price differences more actively
- Battery wear is a lower concern than short-term economics
- You prefer the battery to work harder when there is value in doing so

### Balanced

This is the default and should fit most homes.

Typical behavior:
- Still pursues useful savings opportunities
- Avoids some of the smaller or twitchier battery moves
- Keeps behavior calmer without making the battery overly passive

This is usually a good fit when:
- You want a practical middle ground
- You care about savings and battery wear
- You want stable behavior without giving up the main value of planning

### Conservative

Use this when you want the battery to behave more steadily and avoid marginal moves.

Typical behavior:
- Ignores more small or borderline battery actions
- Produces calmer plans with less switching
- Gives up some savings in exchange for less battery activity

This is usually a good fit when:
- Battery wear matters more than small extra savings
- You dislike frequent small charge/discharge changes
- You want simpler, quieter battery behavior

## What profiles do not do

Profiles do not raise the configured battery minimum.

If you want more reserve left in a battery, set that battery's minimum energy directly in the battery configuration. Profiles only control how willing WattPlan is to move battery energy around.

Profiles also do not replace battery targets. If you need a battery, such as an EV, to reach a specific level by a specific time, use a target. A common Home Assistant setup is an automation that sets a weekday morning target and adjusts it for holidays or other patterns.
