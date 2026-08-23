# Wickless Research Summary — June/July/August 2026

Generated locally 2026-08-23. Data: Dukascopy M1 BID/ASK resampled to strict M15,
12 instruments (7 USD majors + XAUUSD + EURJPY/GBPJPY/AUDJPY/EURGBP).
Baseline rules = production defaults (union session, signal-close entry, 2R target).

## Step 1 — Breadth expansion (more markets)

| Window | Core-7 baseline | Universe-12 |
|---|---|---|
| June | −3.09R (3 tr) | **+0.88R** (5 tr) |
| July | +10.58R (16 tr) | +9.50R (20 tr) |
| Aug 3–21 | −6.13R (6 tr) | −8.22R (11 tr) |
| **Pooled** | **+1.37R**, PF 1.08, DD 6.13 | **+2.15R**, PF 1.09, DD 8.22 |

Verdict: +44% sample size at flat expectancy. Diversification helped June,
hurt August. Necessary but not sufficient — keep XAUUSD and JPY crosses under
observation; per-pair R table in breadth_and_controls.json.

## Step 2 — Portfolio risk controls (chronological replay)

Rules: max 2 concurrent positions; stop trading a UTC day at −2R; half risk
while below equity HWM.

| Window | Universe-12 raw | With controls |
|---|---|---|
| June | +0.88R | −0.59R (half-sizing during recovery) |
| July | +9.50R | +5.68R |
| Aug | −8.22R | −4.62R |
| **Pooled** | +2.15R, DD 8.22 | +0.47R, **DD 4.62** |

Verdict: drawdown cut 44%; profit give-back in recovery periods is the cost.
Circuit breakers almost never bound (low overlap at this frequency);
half-sizing did most of the work.

## Step 3 — Exit asymmetry (bar-replay simulator)

Simulator validated against engine exits (< 0.05R tolerance; residual diffs are
engine cost deductions). Variants: time-stop after N bars; bank half at +1R,
stop to breakeven.

| Variant | June | July | Aug | Pooled | Worst DD |
|---|---|---|---|---|---|
| Engine baseline | +0.88 | +9.50 | −8.22 | +2.16 | 8.22 |
| Time-stop 8 bars | −1.22 | +3.97 | **−2.74** | +0.01 | **3.13** |
| Time-stop 16 bars | +1.00 | +6.20 | −5.73 | +1.47 | 5.73 |
| Partial + breakeven | +2.00 | +1.00 | −5.50 | −2.50 | 5.50 |

Verdict: time-stops reshape risk (Aug DD halved) but cap trend-month profits.
Partial+BE destroys the edge here (winners get BE'd before running). Nothing
improves absolute return — consistent with every other test this session.

## Engine defect found (fix recommended)

`PendingOrder.target` keeps float drift (e.g. 1.1559400000000004). A bar whose
high equals the exact decimal target fails the `>=` test by one ULP, silently
converting a TARGET into a STOP (EURUSD 2026-08-05T11:15 case). Fix: quantize
entry/stop/target to `profile.price_decimals` when creating PendingOrder, or
compare with a tolerance. Direction is conservative, but it distorts backtests
and live behavior asymmetrically.

## Round 2 — further hypotheses tested

### ULP precision quirk: correctness fix, NOT a profit source
Quantized-target replay across all 36 trades found exactly 2 affected:
one engine-pessimistic (EURUSD Aug 5, STOP should be TARGET), one
engine-optimistic (USDJPY Jul 23). Net impact +0.02R — a wash. Still worth
fixing for backtest/live fidelity, but it moves no money.

### Reward:risk multiple: production RR=2.0 is already optimal
| RR | Pooled net R | PF |
|---|---|---|
| 1.5 | −4.22 | 0.82 |
| **2.0 (prod)** | **+2.15** | **1.09** |
| 2.5 | +1.78 | 1.07 |
| 3.0 | −3.70 | 0.87 |

Lower targets get eaten by costs; higher ones never get reached often enough.

### H1 trend confluence gate: initial promise was LOOKAHEAD BIAS
First implementation returned +7.64R/16 trades — but its index fallback
read EMA values from the END of the series (future leak). Reimplemented
causally and stress-tested across gate definitions:

| Gate variant | Pooled net R | Expectancy |
|---|---|---|
| none (baseline) | +2.15 / 36 | +0.06 |
| H1 EMA50 slope-3h | +3.27 / 32 | +0.10 |
| H1 EMA50 slope-6h | +4.38 / 28 | +0.16 |
| H1 EMA20 slope-3h | −1.81 / 34 | −0.05 |
| H1 EMA100 slope-3h | +4.31 / 31 | +0.14 |
| H1 EMA50 position | +3.27 / 32 | +0.10 |

Verdict: mild positive tilt, not a plateau — no variant avoids August's
losses once causal. Marginal, unproven, keep out of production.

### Entry-cost timing: dead end
Cost-to-risk by hour is flat (0.05–0.09 across all filled-signal hours);
no timing edge available.

## Bottom line

Across four independent interventions (parameters, entry model, regime gate,
breadth, risk overlay, exit shapes), pooled expectancy remains ~+0.05R/trade.
The strategy is approximately breakeven-plus-costs at current frequency; its
future rests on sample accumulation (nightly autoresearch), breadth, and the
risk layer — not signal tuning. Promotion gates correctly reject everything
tested; do not promote.

## Artifacts

- `.runtime-data/breadth_and_controls.json` — steps 1–2 detail
- `.runtime-data/exit_variants.json` — step 3 detail
- `.runtime-data/validation_*.json`, `sweep_results_aug3wk.json` — earlier tests
- `reports/local-aug3wk/` — production reference rerun
