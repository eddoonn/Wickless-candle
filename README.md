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

`≈` defaults to half of one minimum tick. A doji is not classified. Signals use
only finalized fifteen-minute candles, so the live detector does not repaint.
The pattern is now a pending setup, not an immediate trade signal: one of the
next three contiguous 15m candles must trade within ±0.5% of the no-wick candle's
opening price before Discord receives a signal.

## What is included

- `Wickless_Candles_v1_0.pine` — Pine Script v6 overlay with opening-side
  detection, bar markers/colors, bullish/bearish counts and percentages, alert
  conditions, and a strict 15m guard.
- `Wickless_Reversal_Strategy_v1_0.pine` — backtestable Pine strategy and
  dynamic Discord JSON alerts.
- `wickless_bot.py` — standard-library Python detector, scanner, Discord
  renderer, dedupe state, statistics, and conservative backtester.
- `no_wick_research.py` — separate, no-lookahead comparison engine for
  trend-filtered origin-limit entries, confirmed-pivot stops, New York session
  filtering, and pending-order expiry. It does not alter live Discord signals.
- `live_data.py` — account-free live Dukascopy bid candles, aggregated from 1m
  to true 15m OHLC.
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
2. Store the no-wick candle's open as its origin price.
3. Inspect only the next three contiguous finalized 15m bars.
4. Confirm the setup when a bar's high/low range intersects the origin-price
   band, which defaults to `origin ± 0.5%`. The earliest qualifying bar wins.
5. Enter at the confirming bar's close in the candle's direction:
   bullish open-low candle → `BUY`; bearish open-high candle → `SELL`.
6. If none of bars 1–3 qualifies, expire the setup without an alert. A missing
   15m bar/session gap also expires it.
7. Use the no-wick bar's opposite extreme plus a 20-tick buffer as the stop.
8. Target `2R` by default.
9. Permit one open position per instrument in the backtester.
10. If a historical bar touches stop and target, count the stop first.
11. Use a deterministic signal ID and durable state so retries do not repost.

The Discord message contains side, symbol, 15m timeframe, entry reference, stop,
2R target, opening-price trigger, retrace candle number/margin, London time, and
signal ID. It does not place or manage broker orders.

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
on some plans. The scanner looks back 45 minutes and deduplicates, so normal
delays do not miss or duplicate a signal. For timing closest to each bar close,
use the Docker deployment below.

## Always-on Docker deployment

On any continuously running Linux host with Docker:

```bash
cp .env.example .env
# Edit .env and insert the rotated webhook.
docker compose up -d --build
docker compose logs -f wickless-signals
```

The daemon wakes 20 seconds after each fifteen-minute boundary, refreshes a
two-hour lookback for every market concurrently, reconstructs pending setups
from finalized candles, and persists signal IDs in a named volume. It shuts
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
3. Create an alert with **Any alert() function call**.
4. Paste the rotated Discord webhook into TradingView's **Webhook URL** field.
5. Leave the alert message unchanged; the script supplies valid Discord JSON.

TradingView saves a snapshot of the script, chart, symbol, timeframe, and
inputs. Delete and recreate the alert after changing any of them.

## Local tests

No Python packages are required:

```bash
python -m compileall -q wickless_bot.py live_data.py run_daemon.py tests
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
  --retrace-bars 3 \
  --retrace-margin-percent 0.5 \
  --output reports/latest/eurusd
```

The result includes `summary.json` and a trade-by-trade `trades.csv`. The
monthly workflow produces the same files for all eight default markets as a
downloadable Actions artifact.

## Trend-filtered retracement research

The separately described "No Wick Strategy" changes the execution model: it
filters wickless candles with price versus EMA(50) plus a five-bar EMA slope,
places a limit order at the signal candle's open, uses the most recently
confirmed 3-left/3-right pivot plus one tick for the stop, and accepts new
signals only from 09:30 through 13:30 America/New_York. Pending orders are
cancelled on a trend change and targets remain 2R for a like-for-like
comparison.

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
are allowed, and one tick of slippage per side is deducted.

The research strategy is deliberately not wired into live alerts. A profitable
historical window is evidence to continue out-of-sample testing, not permission
to replace the live strategy.

## Accuracy and risk notes

- Dukascopy automation uses bid OHLC. Live BUY fills occur on ask, so the raw
  backtest does not model the full spread.
- Historical 15m OHLC cannot reveal intrabar ordering. Same-bar stop/target
  touches are treated conservatively as stops.
- A pattern match is not evidence of an edge. Backtest multiple regimes,
  account for costs, and validate out of sample.
- A 0.5% price band is still wide for major FX pairs (roughly 50–75 pips
  around typical prices). Treat it as a configurable condition, not as proof
  of a meaningful pullback filter.
- The half-tick tolerance handles representation noise. Raising it toward two
  ticks changes the pattern and should be treated as optimization.
- This is research software and signal delivery, not financial advice or an
  automatic trade-execution system.
