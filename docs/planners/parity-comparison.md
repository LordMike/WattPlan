# Planner parity benchmark

45/54 replays completed within the independent 120-second whole-run limit. The matrix contains all 17 published presets plus historical HiGHS-32, each run once on three unchanged datasets. Failures retain partial diagnostics and null economic totals; none were retried or given more time.

- All 29 completed HA replays with a scored historical match have unchanged cost and final battery energy. Dynamic-programming-strong remains cheapest on both HA scenarios: 64.801996 DKK with recorded forecasts and 67.733586 DKK with causal forecasts.
- Matched alternatives took 1.46–7.06 seconds longer per complete HA replay; HiGHS-8/16 took 2.51–8.60 seconds less. These are single-run measurements, with the new shared features included.
- Six HA replays reached the 120-second whole-run limit: HiGHS, HiGHS-24 and HiGHS-32 on each HA dataset. Three synthetic HiGHS replays failed with native solver 'no usable solution' RuntimeError (HiGHS-8, HiGHS-24 and HiGHS-32); these are separate from replay timeouts. Saved reports do not include native solver statuses, so their precise causes are unconfirmed.
- All 14 completed optimizing configurations met the synthetic battery deadline. The deliberately passive self-consumption baseline missed it. Charge-window-96 improved by 0.039199 TEST versus its matching prior run, with equal final battery energy; the other scored synthetic matches have unchanged costs.
- Both independent audits passed across all 54 reports. The final source passed 1,445 tests at 89.80% coverage, and the HACS archive was checked against those sources.

[Exact configuration](parity-benchmark.json). HiGHS suffixes are lookahead slots. Unsuffixed HiGHS retains the recorded settings: 48 slots on the two HA datasets and 32 on synthetic. Thus unsuffixed HiGHS and HiGHS-32 are the same effective configuration on the synthetic fixture. The former completed and the latter failed after 69 cycles despite identical starting battery energy through those completed cycles. This single run does not establish a preset regression or a native timeout cause.

The recorded scenario uses genuine archived forecasts and reconstructed measured actuals. The causal companion changes only usage/PV forecasts to causal persistence; its actuals and prices are identical. Both cover 192 planning cycles over 48 hours, with the original 81.812038-second initial capture delay retained. Neither has configured battery targets or comfort loads. The separate synthetic fixture covers 96 cycles, two heterogeneous batteries, signed tariffs and a battery deadline. Comfort and optional-load parity is verified by tests, not by these battery-only historical replays.

Historical columns use the latest matching final additional-planner report, or the earlier HA report where that configuration was not rerun. Equivalent factory aliases, omitted defaults and HiGHS settings/options are normalized. A dash in an old column means no matching scored prior run. Timeout results are never ranked by partial cost. Timing is one sample per configuration and includes newly added reporting; it is not a statistical speed guarantee.

Shared replay physics now suppresses grid charging while residual PV is being exported, preserving source-ingress restrictions and matching the original solver's grid-direction semantics. The independent audit counts when this correction applies to executed actions and checks historical simultaneous import/export intervals. New executed suppressions: 0; new simultaneous import/export intervals: 0; matching prior report simultaneous intervals: 0. Even when executed actions are unaffected, changed forecast replay can affect candidate ranking; this is a full parity-source comparison, not an isolated reporting-overhead experiment.

Published policy semantics also changed: a selected grid-charge gene with exactly zero accepted forecast grid charging is now published as preserve, matching HiGHS. Search scoring and accepted forecast energy are unchanged by that normalization, but actual forecast errors can make the previous grid-charge label execute differently. Therefore realized cost and cycle-start action counts can legitimately change even for otherwise unchanged healthy-battery searches.

## Recorded forecasts

Costs are DKK. Delta is new minus prior. Whole-run times are seconds; p95 covers completed planning calls.

| Configuration | Status | Prior cost | New cost | Cost delta | Prior time | New time | p95 call | Final kWh |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dynamic-programming-strong | Complete | 64.801996 | 64.801996 | 0.000000 | 28.808 | 35.864 | 0.2015 | 1.000 |
| beam-16 | Complete | 65.149669 | 65.149669 | 0.000000 | 20.730 | 25.374 | 0.1392 | 1.000 |
| beam-2 | Complete | 65.608359 | 65.608359 | 0.000000 | 4.908 | 7.178 | 0.0319 | 1.000 |
| dynamic-programming-fast | Complete | 65.877327 | 65.877327 | 0.000000 | 11.263 | 13.614 | 0.0726 | 1.000 |
| genetic-standard | Complete | 67.979279 | 67.979279 | 0.000000 | 13.963 | 17.175 | 0.1039 | 1.000 |
| highs-16 | Complete | 68.444444 | 68.444444 | 0.000000 | 89.313 | 80.983 | 1.0464 | 1.000 |
| genetic-fast | Complete | 68.981421 | 68.981421 | 0.000000 | 7.013 | 9.944 | 0.0555 | 1.000 |
| local-search-96 | Complete | 69.677497 | 69.677497 | 0.000000 | 22.138 | 26.510 | 0.1464 | 1.000 |
| charge-window-96 | Complete | 69.967282 | 69.967282 | 0.000000 | 17.728 | 22.129 | 0.1298 | 1.000 |
| highs-8 | Complete | 71.695776 | 71.695776 | 0.000000 | 61.403 | 52.806 | 0.6329 | 1.000 |
| cem-48 | Complete | 71.746226 | 71.746226 | 0.000000 | 11.882 | 15.574 | 0.0854 | 1.000 |
| greedy-8 | Complete | 72.314373 | 72.314373 | 0.000000 | 3.689 | 5.144 | 0.0196 | 1.000 |
| local-search-3 | Complete | 72.314373 | 72.314373 | 0.000000 | 2.861 | 4.400 | 0.0156 | 1.000 |
| self-consumption | Complete | 72.314373 | 72.314373 | 0.000000 | 2.451 | 3.967 | 0.0133 | 1.000 |
| greedy-256 | Complete | 72.702428 | 72.702428 | 0.000000 | 42.565 | 47.932 | 0.2758 | 1.000 |
| highs | RunTimeout, 30 cycles | — | — | — | 120.007 | 120.007 | 11.2576 | 2.336 |
| highs-24 | RunTimeout, 122 cycles | — | — | — | — | 120.014 | 2.9008 | 3.347 |
| highs-32 | RunTimeout, 47 cycles | — | — | — | 120.008 | 120.008 | 8.4426 | 2.757 |

Action counts are policy choices at cycle starts (self-consume / preserve / grid-charge), not execution-interval row counts. Failed rows describe only completed starts; their final kWh is partial.

| Configuration | Prior cycle-start actions | New cycle-start actions |
| --- | --- | --- |
| dynamic-programming-strong | Battery: 122/55/15 | Battery: 122/55/15 |
| beam-16 | Battery: 93/88/11 | Battery: 93/88/11 |
| beam-2 | Battery: 94/88/10 | Battery: 94/88/10 |
| dynamic-programming-fast | Battery: 125/56/11 | Battery: 125/56/11 |
| genetic-standard | Battery: 95/74/23 | Battery: 95/86/11 |
| highs-16 | Battery: 98/82/12 | Battery: 98/82/12 |
| genetic-fast | Battery: 160/14/18 | Battery: 160/14/18 |
| local-search-96 | Battery: 150/30/12 | Battery: 150/30/12 |
| charge-window-96 | Battery: 129/45/18 | Battery: 129/51/12 |
| highs-8 | Battery: 41/149/2 | Battery: 41/149/2 |
| cem-48 | Battery: 161/25/6 | Battery: 161/25/6 |
| greedy-8 | Battery: 192/0/0 | Battery: 192/0/0 |
| local-search-3 | Battery: 192/0/0 | Battery: 192/0/0 |
| self-consumption | Battery: 192/0/0 | Battery: 192/0/0 |
| greedy-256 | Battery: 190/0/2 | Battery: 190/0/2 |
| highs | Battery: 15/10/3 | Battery: 17/10/3 |
| highs-24 | — | Battery: 80/34/8 |
| highs-32 | Battery: 33/10/3 | Battery: 34/10/3 |

## Causal forecasts

Costs are DKK. Delta is new minus prior. Whole-run times are seconds; p95 covers completed planning calls.

| Configuration | Status | Prior cost | New cost | Cost delta | Prior time | New time | p95 call | Final kWh |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dynamic-programming-strong | Complete | 67.733586 | 67.733586 | 0.000000 | 28.039 | 34.206 | 0.2162 | 1.000 |
| beam-16 | Complete | 68.701992 | 68.701992 | 0.000000 | 19.715 | 24.397 | 0.1374 | 1.000 |
| dynamic-programming-fast | Complete | 68.821431 | 68.821431 | 0.000000 | 10.364 | 12.690 | 0.0662 | 1.000 |
| genetic-standard | Complete | 69.111255 | 69.111255 | 0.000000 | 13.507 | 16.929 | 0.1024 | 1.000 |
| beam-2 | Complete | 69.315129 | 69.315129 | 0.000000 | 4.867 | 7.025 | 0.0313 | 1.000 |
| highs-16 | Complete | 69.467963 | 69.467963 | 0.000000 | 95.913 | 90.247 | 1.1329 | 1.000 |
| genetic-fast | Complete | 69.476598 | 69.476598 | 0.000000 | 7.064 | 9.591 | 0.0554 | 1.000 |
| charge-window-96 | Complete | 70.655554 | 70.655554 | 0.000000 | 17.569 | 22.819 | 0.1356 | 1.000 |
| highs-8 | Complete | 71.525105 | 71.525105 | 0.000000 | 55.463 | 52.955 | 0.6524 | 1.000 |
| cem-48 | Complete | 71.751245 | 71.751245 | 0.000000 | 12.388 | 15.366 | 0.0812 | 1.000 |
| greedy-8 | Complete | 72.314373 | 72.314373 | 0.000000 | 3.770 | 5.397 | 0.0226 | 1.000 |
| local-search-3 | Complete | 72.314373 | 72.314373 | 0.000000 | 3.041 | 4.564 | 0.0161 | 1.000 |
| self-consumption | Complete | 72.314373 | 72.314373 | 0.000000 | 2.473 | 4.020 | 0.0136 | 1.000 |
| greedy-256 | Complete | — | 72.317820 | — | — | 49.230 | 0.2882 | 1.000 |
| local-search-96 | Complete | 73.444378 | 73.444378 | 0.000000 | 20.213 | 25.523 | 0.1403 | 1.000 |
| highs | RunTimeout, 60 cycles | — | — | — | — | 120.007 | 4.8072 | 3.466 |
| highs-24 | RunTimeout, 136 cycles | — | — | — | — | 120.010 | 3.3674 | 1.000 |
| highs-32 | RunTimeout, 88 cycles | — | — | — | 120.008 | 120.010 | 4.9703 | 1.809 |

Action counts are policy choices at cycle starts (self-consume / preserve / grid-charge), not execution-interval row counts. Failed rows describe only completed starts; their final kWh is partial.

| Configuration | Prior cycle-start actions | New cycle-start actions |
| --- | --- | --- |
| dynamic-programming-strong | Battery: 138/39/15 | Battery: 138/39/15 |
| beam-16 | Battery: 125/57/10 | Battery: 125/57/10 |
| dynamic-programming-fast | Battery: 152/30/10 | Battery: 152/30/10 |
| genetic-standard | Battery: 74/114/4 | Battery: 74/114/4 |
| beam-2 | Battery: 117/65/10 | Battery: 117/65/10 |
| highs-16 | Battery: 67/118/7 | Battery: 67/118/7 |
| genetic-fast | Battery: 146/33/13 | Battery: 146/33/13 |
| charge-window-96 | Battery: 139/49/4 | Battery: 139/49/4 |
| highs-8 | Battery: 48/141/3 | Battery: 48/141/3 |
| cem-48 | Battery: 169/21/2 | Battery: 169/21/2 |
| greedy-8 | Battery: 192/0/0 | Battery: 192/0/0 |
| local-search-3 | Battery: 192/0/0 | Battery: 192/0/0 |
| self-consumption | Battery: 192/0/0 | Battery: 192/0/0 |
| greedy-256 | — | Battery: 189/0/3 |
| local-search-96 | Battery: 176/11/5 | Battery: 176/11/5 |
| highs | — | Battery: 31/28/1 |
| highs-24 | — | Battery: 68/63/5 |
| highs-32 | Battery: 56/28/4 | Battery: 56/28/4 |

## Synthetic target fixture

Costs are synthetic TEST currency and cannot be compared with DKK. Prior results use matching immutable input hashes and normalized configurations from the selected per-planner reports, followed by the earlier verified synthetic comparison for original algorithms. No prior whole-run time is invented where an older report did not record one. A † marks a raw cost difference with unequal terminal energy: it is not a like-for-like economic improvement or regression. Dynamic-programming-strong ends with 4.652 kWh in the house battery, versus 0.700 kWh for every other completed configuration, so its cost is not directly comparable across configurations. All completed runs end with 5.000 kWh in the vehicle. Failed runs show partial energy and deadline checks only.

| Configuration | Status | Prior cost | New cost | Raw delta | Prior / new seconds | Target misses / checks | Prior → new battery kWh |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| beam-2 | Complete | — | 1.967313 | — | — / 4.390 | 0 / 1 | — → house: 0.700; vehicle: 5.000 |
| beam-16 | Complete | 1.967313 | 1.967313 | 0.000000 | — / 17.163 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| cem-48 | Complete | 1.967313 | 1.967313 | 0.000000 | 6.942 / 7.156 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| charge-window-96 | Complete | 2.006512 | 1.967313 | -0.039199 | 10.432 / 11.487 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| dynamic-programming-fast | Complete | 1.967313 | 1.967313 | 0.000000 | 10.948 / 9.765 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| greedy-256 | Complete | 1.967313 | 1.967313 | 0.000000 | — / 20.741 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| highs-16 | Complete | — | 1.967313 | — | — / 35.360 | 0 / 1 | — → house: 0.700; vehicle: 5.000 |
| local-search-96 | Complete | 1.967313 | 1.967313 | 0.000000 | — / 11.355 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| genetic-fast | Complete | 2.092129 | 2.092129 | 0.000000 | 4.054 / 4.179 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| highs | Complete | 2.116418 | 2.116418 | 0.000000 | — / 72.476 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| greedy-8 | Complete | — | 2.159019 | — | — / 2.833 | 0 / 1 | — → house: 0.700; vehicle: 5.000 |
| genetic-standard | Complete | 3.279315 | 3.279315 | 0.000000 | 6.982 / 7.449 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| local-search-3 | Complete | — | 4.338450 | — | — / 2.319 | 0 / 1 | — → house: 0.700; vehicle: 5.000 |
| self-consumption | Complete | 4.891220 | 4.891220 | 0.000000 | — / 2.176 | 1 / 1 | house: 0.700; vehicle: 5.000 → house: 0.700; vehicle: 5.000 |
| dynamic-programming-strong | Complete | 5.585262 | 5.585262 | 0.000000 | 24.271 / 26.780 | 0 / 1 | house: 4.652; vehicle: 5.000 → house: 4.652; vehicle: 5.000 |
| highs-24 | RuntimeError, 34 cycles | — | — | — | — / 24.629 | 0 / 1 | — → house: 2.474; vehicle: 4.601 |
| highs-32 | RuntimeError, 69 cycles | 2.116418 | — | — | — / 52.143 | 0 / 1 | house: 0.700; vehicle: 5.000 → house: 6.549; vehicle: 5.000 |
| highs-8 | RuntimeError, 32 cycles | — | — | — | — / 9.253 | 0 / 1 | — → house: 2.487; vehicle: 4.601 |

| Configuration | Prior cycle-start actions | New cycle-start actions |
| --- | --- | --- |
| beam-2 | — | house: 81/7/8; vehicle: 81/7/8 |
| beam-16 | house: 81/7/8; vehicle: 81/7/8 | house: 81/7/8; vehicle: 81/7/8 |
| cem-48 | house: 86/0/10; vehicle: 86/0/10 | house: 86/2/8; vehicle: 78/10/8 |
| charge-window-96 | house: 59/1/36; vehicle: 88/0/8 | house: 59/29/8; vehicle: 88/0/8 |
| dynamic-programming-fast | house: 81/7/8; vehicle: 81/7/8 | house: 81/7/8; vehicle: 81/7/8 |
| greedy-256 | house: 88/0/8; vehicle: 80/8/8 | house: 88/0/8; vehicle: 80/8/8 |
| highs-16 | — | house: 69/19/8; vehicle: 88/0/8 |
| local-search-96 | house: 88/0/8; vehicle: 88/0/8 | house: 88/0/8; vehicle: 88/0/8 |
| genetic-fast | house: 63/21/12; vehicle: 72/14/10 | house: 63/25/8; vehicle: 72/18/6 |
| highs | house: 69/20/7; vehicle: 89/0/7 | house: 69/20/7; vehicle: 89/0/7 |
| greedy-8 | — | house: 88/2/6; vehicle: 80/8/8 |
| genetic-standard | house: 10/78/8; vehicle: 16/72/8 | house: 10/80/6; vehicle: 16/72/8 |
| local-search-3 | — | house: 96/0/0; vehicle: 72/18/6 |
| self-consumption | house: 96/0/0; vehicle: 96/0/0 | house: 96/0/0; vehicle: 96/0/0 |
| dynamic-programming-strong | house: 55/27/14; vehicle: 56/27/13 | house: 55/27/14; vehicle: 56/32/8 |
| highs-24 | — | house: 26/0/8; vehicle: 27/0/7 |
| highs-32 | house: 69/20/7; vehicle: 89/0/7 | house: 60/2/7; vehicle: 62/0/7 |
| highs-8 | — | house: 24/0/8; vehicle: 25/0/7 |

## Reproduction and audit

Runtime base revision: `2d63ec9932d6bafb676fe2aa9197aa2591b03e45`, with the parity working-tree changes captured as immutable source copies and SHA-256 hashes before execution. Source, input and historical-evidence hashes are checked again after the matrix. Private evidence directory: `parity-20261009-final` under the local planner lab. It contains before/after manifests, source snapshots, exact config, validations, separate logs, all JSON/CSV/HTML reports, `audit.json` and `root-independent-audit.json`.

The audit independently reconstructs fractional battery charging/discharging, latched grid-charge eligibility, efficiencies, SoC and energy conservation from original actuals and saved actions. It checks tariff costs, aggregation, CSV/JSON equivalence, target deadlines, final energy, cycle-start action counts, time-in-mode, timing summaries and unscored failures. Raw household traces are not published.

The second, root-level audit independently verified all 54 reports, including manifest hashes, configuration coverage, cycle continuity, costs, timing summaries, energy conservation and null totals on failures. Its maximum site-energy balance residual was 2.22e-16 kWh.

Run the matrix only against frozen sources, with the compute lock held. Use a new private output directory and preserve the simulator's 120-second whole-run cap; do not extend timeouts or retry failed rows.
