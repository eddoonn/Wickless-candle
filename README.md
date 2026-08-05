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
The pattern becomes actionable only when it passes the impulse-quality gates,
agrees with the EMA trend, and closes during the liquid London-plus-New-York
window. Discord receives the executable signal-close entry after every spread,
stop-distance, execution-cost, freshness, and position safeguard passes.

## What is included

- `Wickless_Candles_v1_0.pine` — Pine Script v6 overlay with opening-side
  detection, bar markers/colors, bullish/bearish counts and percentages, alert
  conditions, and a strict 15m guard.
- `Wickless_Reversal_Strategy_v1_0.pine` — backtestable Pine strategy and
  dynamic Discord JSON alerts.
- `wickless_bot.py` — standard-library Python detector, scanner, Discord
  renderer, dedupe state, statistics, and conservative backtester.
- `live_scan.py` and `time_display.py` — the live entrypoint and shared timestamp
  renderer that pair every event time in UTC and `Europe/London`, including
  automatic GMT/BST daylight-saving conversion.
- `no_wick_research.py` — the shared, no-lookahead engine used by both live
  Discord scanning and historical backtests.
- `live_data.py` — account-free live Dukascopy BID and ASK candles, aggregated
  from 1m to true 15m OHLC, plus a timestamped executable quote snapshot.
- `run_daemon.py`, `Dockerfile`, and `docker-compose.yml` — an always-on,
  candle-aligned deployment.
- GitHub Actions for CI, five-minute live scanning, monthly trailing 30-day
  backtests across XAUUSD and all seven USD majors, and a reproducible
  seven-pair production-reference backtest.
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
3. Accept new setups from 05:00 through 13:30 America/New_York, covering the
   liquid London morning and New York session.
4. Require at least an 80% body, no more than a two-tick opening wick, a
   `0.50–2.00 × ATR(14)` range, and a close in the final 10% of the impulse.
5. Enter at the finalized signal candle's ASK close for BUY or BID close for
   SELL; no intrabar price path is credited before that close.
6. Place the stop one tick beyond the signal candle's opposite edge.
7. Reject risk below the pair minimum (5 pips for FX majors; $0.50 for XAUUSD),
   below `0.40 × ATR(14)`, or below `3 ×` the observed spread.
8. Reject risk above `1.50 × ATR(14)` or when estimated spread plus slippage
   exceeds 10% of `1R`.
9. Target `2R`.
10. Allow only one active position per pair.
11. If a historical bar has ambiguous stop/target ordering, count the stop
    first.
12. Validate the current BID/ASK quote and reject an entry if its stop or target
    already traded, its quote is stale, or price moved more than `0.25R`.
13. Publish only signals no more than 120 seconds old.
14. Pre-claim a deterministic signal ID and persist the active position so a
    retry or service restart cannot repost or overlap it.

The Discord message contains side, symbol, 15m timeframe, quality score,
signal-close entry, signal-range stop, 2R target, current BID/ASK and spread,
entry displacement in R,
stop distance in pips and ATR, estimated execution cost as a percentage of 1R,
signal age, actionability status, and signal ID. Signal close, entry, detection,
and publication timestamps are each shown in both UTC and Europe/London, with
GMT/BST handled automatically. It reports validated entries; it does not place
or manage broker orders.

## Automatic Discord signals with GitHub Actions

The workflow is scheduled every five minutes and reads the webhook only from
the repository secret `DISCORD_WEBHOOK_URL`. The URL is never stored in source,
Pine, workflow YAML, logs, Docker layers, or test fixtures.

1. Revoke the webhook URL that was shared in chat and create a replacement.
   A Discord webhook is a channel-writing credential.
2. Open the repository's **Settings → Secrets and variables → Actions**.
3. Choose **New repository secret**.
4. Name it exactly `DISCORD_WEBHOOK_URL` and paste the rotated URL.
5. Open **Actions → Live Wickless 15m Discord signals → Run workflow** once.
6. Confirm the run is green. Scheduled runs then continue automatically.

GitHub scheduled workflows can start late during platform load, and a private
repository running every five minutes can exceed the included Actions minutes
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
the EMA and risk state from finalized candles, and
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
5. Set the alert message to `{{strategy.order.alert_message}}`; the finalized
   signal-close entry supplies valid Discord JSON.

TradingView saves a snapshot of the script, chart, symbol, timeframe, and
inputs. Delete and recreate the alert after changing any of them.

## Local tests

No Python packages are required:

```bash
python -m compileall -q \
  wickless_bot.py live_scan.py time_display.py no_wick_research.py \
  live_data.py run_daemon.py autoresearch scripts tests
python -m unittest discover -s tests -v
```

Optional, clearly labelled Discord connectivity test:

```bash
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
python tools/test_discord_webhook.py
unset DISCORD_WEBHOOK_URL
```

## Constrained autoresearch

The repository includes an isolated research loop under `autoresearch/`, adapted
from `eddoonn/autoresearch`. Experiments run only on `autoresearch/...` branches;
they cannot update `main`, and the editable candidate surface cannot change the
strategy's reward/risk, stop bounds, spread/cost limits, slippage, or
one-position safeguards.

Start with `autoresearch/README.md` and `autoresearch/program.md`. The fixed
evaluator uses reusable Dukascopy M1 BID/ASK archives for June and July 2026,
requires at least ten July trades, and prioritizes worst-fold profitability over
raw frequency. Every experiment also appends one line to `attempts.log`; after
every 20 cumulative attempts, a deterministic coach updates only the bounded
`playbook.md` search guidance and never changes scoring or safety rules.

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
  --stop-buffer-ticks 1 \
  --output reports/latest/eurusd
```

The result includes `summary.json` and a trade-by-trade `trades.csv`. The
monthly workflow produces the same files for all eight default markets as a
downloadable Actions artifact.

Re-run the reviewed seven-pair June/July production reference, including paired
UTC and London windows and trade timestamps:

```bash
python scripts/rerun_production_backtest.py \
  --data-root .runtime-data/autoresearch-datasets \
  --policy autoresearch/policy.json \
  --output reports/production-backtest/latest
```

The **Production Wickless backtest** Actions workflow restores or builds the
fixed BID/ASK data, runs all tests, publishes `report.json`, `summary.json`, and
`trades.csv` to `reports/production-backtest/latest`, appends an immutable
history summary, and sends the outcome to Discord. Every generated timestamp,
fold boundary, data boundary, signal time, entry time, and exit time is available
in both UTC and Europe/London.

## Reproduce the seven-pair comparison

The live strategy is the same trend-filtered signal-close model used by this
comparison: EMA(50) with a five-bar slope, the signal candle's opposite edge
plus one tick for the stop, the 05:00–13:30 New York London-plus-New-York signal
window, unchanged impulse-quality gates, 2R targets, one active position per
pair, and pair/ATR stop bounds.
Python is the execution source of truth for BID/ASK spread and cost-to-risk
validation because Pine historical bars do not provide equivalent market-side
data.

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
Comparisons include the current signal-close strategy and legacy origin-limit
and origin-reclaim research variants. Only one position can be active per pair,
and one tick of slippage per side is deducted.

## Accuracy and risk notes

- Live validation uses Dukascopy BID/ASK: BUY entries use ASK and exit on BID;
  SELL entries use BID and exit on ASK. Bid-only historical files remain
  supported for legacy research and are explicitly reported as a limitation.
- Historical 15m OHLC cannot reveal intrabar ordering. Same-bar stop/target
  touches are treated conservatively as stops.
- A pattern match is not evidence of an edge. Backtest multiple regimes,
  account for costs, and validate out of sample.
- The live scanner reconstructs EMA and ATR state from a rolling two-week
  history and acts only on finalized candles.
- The two-tick wick ceiling is a signal-quality parameter and must be evaluated
  out of sample before it is changed.
- This is research software and signal delivery, not financial advice or an
  automatic trade-execution system.
