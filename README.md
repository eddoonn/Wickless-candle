# Wickless Candle — 15-minute indicator, strategy, and Discord bot

A behavior-equivalent, independently written implementation of the public
description for
[xGhozt Wickless Candles](https://www.tradingview.com/script/1ltr9jnV-xGhozt-Wickless-Candles/),
plus a transparent execution layer, reproducible backtester, and automated
Discord delivery.

The TradingView source is protected. This project does not access, extract, or
copy it. The publisher's public July 2022 update says the indicator shows only
candles with no wick **from the opening price**, which makes the core rule:

| Closed 15m candle | Missing wick | Pending direction |
|---|---|---|
| Bullish and `open ≈ low` | Lower wick | `BUY` |
| Bearish and `open ≈ high` | Upper wick | `SELL` |

`≈` allows at most two minimum ticks. A doji is not classified. Signals use
only finalized fifteen-minute candles, so the live detector does not repaint.
The pattern becomes a three-bar setup only when it passes the impulse-quality
gates and agrees with the EMA trend during the New York signal window. Discord
receives a signal only after the executable market side touches the narrow
origin zone and a finalized candle reclaims that zone in the setup direction.

## What is included

- `Wickless_Candles_v1_0.pine` — Pine Script v6 overlay with opening-side
  detection, bar markers/colors, bullish/bearish counts and percentages, alert
  conditions, and a strict 15m guard.
- `Wickless_Reversal_Strategy_v1_0.pine` — backtestable Pine strategy and
  dynamic Discord JSON alerts.
- `wickless_bot.py` — standard-library Python detector, scanner, Discord
  renderer, dedupe state, statistics, and conservative backtester.
- `no_wick_research.py` — the shared, no-lookahead engine used by both live
  Discord scanning and historical backtests.
- `live_data.py` — account-free live Dukascopy BID and ASK candles, aggregated
  from 1m to true 15m OHLC, plus a timestamped executable quote snapshot.
- `run_daemon.py`, `Dockerfile`, and `docker-compose.yml` — an always-on,
  candle-aligned deployment.
- GitHub Actions for CI, fifteen-minute live scanning, and monthly trailing
  30-day backtests across XAUUSD and all seven USD majors.
- Unit, integration, timing, Pine-contract, workflow, and secret-leak tests.

The default markets are:

`XAUUSD`, `EURUSD`, `GBPUSD`, `USDJPY`, `USDCHF`, `USDCAD`, `AUDUSD`, and
`NZDUSD`.

## Execution rules

The public indicator does not define position sizing, stop loss, take profit,
or a post-close fill model. Those are deliberately explicit here:

1. Detect the pattern at the close of a finalized 15m bar.
2. Require a BUY candle to close above a rising EMA(50), or a SELL candle to
   close below a falling EMA(50). EMA slope uses a five-bar lookback.
3. Accept new setups only from 09:30 through 13:30 America/New_York.
4. Require at least an 80% body, no more than a two-tick opening wick, a
   `0.50–2.00 × ATR(14)` range, and a close in the final 10% of the impulse.
5. Create a symmetric origin zone with half-width
   `max(2 ticks, 0.10 × ATR(14))` around the wickless candle's open.
6. Require an actual ASK-side zone touch for BUY or BID-side touch for SELL,
   followed within three bars by a one-tick directional reclaim.
7. Enter at the reclaim candle's ASK close for BUY or BID close for SELL. Reject
   an entry more than `0.30 × ATR(14)` from the origin.
8. Use the latest confirmed 3-left/3-right pivot plus one tick for the stop.
9. Reject risk below the pair minimum (5 pips for FX majors; $0.50 for XAUUSD),
   below `0.40 × ATR(14)`, or below `3 ×` the observed spread.
10. Reject risk above `1.50 × ATR(14)` or when estimated spread plus slippage
   exceeds 10% of `1R`.
11. Target `2R`.
12. Allow multiple setup candidates but only one active position per pair.
13. Invalidate a setup on trend change, missing candle, structural stop breach,
    or after three confirmation bars.
14. If a historical bar has ambiguous stop/target ordering, count the stop
   first.
15. Validate the current BID/ASK quote and reject an entry if its stop or target
    already traded, its quote is stale, or price moved more than `0.25R`.
16. Publish only signals no more than 120 seconds old.
17. Pre-claim a deterministic signal ID and persist the active position so a
    retry or service restart cannot repost or overlap it.

The Discord message contains side, symbol, 15m timeframe, origin zone, quality
score, touch/reclaim bars, reclaim entry, pivot stop, 2R target, current BID/ASK
and spread, entry displacement in ATR and R,
stop distance in pips and ATR, estimated execution cost as a percentage of 1R,
signal age, publication time, actionability status, London fill time, and signal
ID. It reports validated entries; it does not place or manage broker orders.

## Automatic Discord signals with GitHub Actions

The workflow runs every fifteen minutes and reads the webhook only from the
repository secret `DISCORD_WEBHOOK_URL`. The URL is never stored in source,
Pine, workflow YAML, logs, Docker layers, or test fixtures.

1. Revoke the webhook URL that was shared in chat and create a replacement.
   A Discord webhook is a channel-writing credential.
2. Open the repository's **Settings → Secrets and variables → Actions**.
3. Choose **New repository secret**.
4. Name it exactly `DISCORD_WEBHOOK_URL` and paste the rotated URL.
5. Open **Actions → Live Wickless 15m Discord signals → Run workflow** once.
6. Confirm the run is green. Scheduled runs then continue automatically.

GitHub scheduled workflows can start late during platform load, and a private
repository running every fifteen minutes can exceed the included Actions minutes
on some plans. The scanner audits fills from a 45-minute research window but
fails closed after 120 seconds: a delayed run records the signal as expired
instead of presenting an old entry as actionable.
Use the Docker deployment below for reliable candle-close delivery.

## Always-on Docker deployment

On any continuously running Linux host with Docker:

```bash
cp .env.example .env
# Edit .env and insert the rotated webhook.
docker compose up -d --build
docker compose logs -f wickless-signals
```

The daemon wakes 20 seconds after each fifteen-minute boundary, refreshes a
two-week BID/ASK reconstruction window for every market concurrently, rebuilds
the EMA, confirmed pivots and pending orders from finalized candles, and
persists handled IDs plus active position state in a named volume. It shuts
down cleanly on `SIGTERM`, runs as a non-root user, drops Linux capabilities,
and has no broker credentials.

One immediate dry run, without Discord:

```bash
python run_daemon.py --once --dry-run --instruments eurusd gbpusd
```

## TradingView setup

Indicator:

1. Open `Wickless_Candles_v1_0.pine` in Pine Editor.
2. Save, add to chart, and use a 15-minute chart.
3. To alert on patterns, create an alert from either exposed alert condition.

Strategy and direct TradingView-to-Discord alerts:

1. Open `Wickless_Reversal_Strategy_v1_0.pine`, save, and add it to a 15m chart.
2. Review the Strategy Tester and set costs/size to match the intended market.
3. Create an alert for **Order fills only**.
4. Paste the rotated Discord webhook into TradingView's **Webhook URL** field.
5. Set the alert message to `{{strategy.order.alert_message}}`; the confirmed
   reclaim entry supplies valid Discord JSON.

TradingView saves a snapshot of the script, chart, symbol, timeframe, and
inputs. Delete and recreate the alert after changing any of them.

## Local tests

No Python packages are required:

```bash
python -m compileall -q \
  wickless_bot.py no_wick_research.py live_data.py run_daemon.py tests
python -m unittest discover -s tests -v
```

Optional, clearly labelled Discord connectivity test:

```bash
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
python tools/test_discord_webhook.py
unset DISCORD_WEBHOOK_URL
```

## Historical statistics and backtest

Install the pinned downloader:

```bash
npm ci --ignore-scripts
mkdir -p .runtime-data
./node_modules/.bin/dukascopy-node \
  -i eurusd -from 2026-06-29 -to 2026-07-30 \
  -t m15 -f csv -dir .runtime-data
```

Reproduce indicator-style counts:

```bash
python wickless_bot.py stats \
  --instrument eurusd \
  --csv .runtime-data/eurusd-m15-bid-2026-06-29-2026-07-30.csv
```

Run the strategy:

```bash
python wickless_bot.py backtest \
  --instrument eurusd \
  --csv .runtime-data/eurusd-m15-bid-2026-06-29-2026-07-30.csv \
  --start 2026-06-30T00:00:00Z \
  --end 2026-07-30T00:00:00Z \
  --ema-length 50 \
  --ema-slope-lookback 5 \
  --pivot-left 3 \
  --pivot-right 3 \
  --stop-buffer-ticks 1 \
  --output reports/latest/eurusd
```

The result includes `summary.json` and a trade-by-trade `trades.csv`. The
monthly workflow produces the same files for all eight default markets as a
downloadable Actions artifact.

## Reproduce the seven-pair comparison

The live strategy is the same trend-filtered origin-reclaim model used by this
comparison: EMA(50) with a five-bar slope, confirmed 3/3 pivot stops plus one
tick, the 09:30–13:30 New York signal window, quality-gated 0.10 ATR origin
zones, touch plus reclaim, three-bar expiry, 2R targets, one active position
per pair, and pair/ATR stop bounds. Python is the execution source of truth for BID/ASK spread and
cost-to-risk validation because Pine historical bars do not provide equivalent
market-side data.

Run the complete comparison across the seven FX majors:

```bash
python no_wick_research.py \
  --data-dir .runtime-data/no-wick-warmup-20260626-0730 \
  --start 2026-06-30T00:00:00Z \
  --end 2026-07-30T00:00:00Z \
  --output reports/no-wick-comparison-30d
```

The output contains aggregate and per-pair CSVs, the full JSON configuration
and limitations, and an auditable trade log for the recommended variant.
Comparisons include the current close-entry baseline, an unfiltered origin
limit, EMA/range-stop variants with and without the New York window, and
EMA/pivot-stop variants with and without the window. Multiple pending orders
are allowed, only one can be active per pair, and one tick of slippage per side
is deducted.

## Accuracy and risk notes

- Live validation uses Dukascopy BID/ASK: BUY zones touch and enter on ASK and
  exit on BID; SELL zones touch and enter on BID and exit on ASK. Bid-only historical files remain
  supported for legacy research and are explicitly reported as a limitation.
- Historical 15m OHLC cannot reveal intrabar ordering. Same-bar stop/target
  touches are treated conservatively as stops.
- A pattern match is not evidence of an edge. Backtest multiple regimes,
  account for costs, and validate out of sample.
- The live scanner reconstructs three-bar setup candidates from a rolling
  two-week history; expired candidates cannot return after a restart.
- The two-tick wick ceiling is a signal-quality parameter and must be evaluated
  out of sample before it is changed.
- This is research software and signal delivery, not financial advice or an
  automatic trade-execution system.
