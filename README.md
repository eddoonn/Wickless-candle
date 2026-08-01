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
The pattern becomes a pending exact-price limit order only when it agrees with
the EMA trend during the New York signal window. Discord receives a signal
only after price trades back to the no-wick candle's opening price.

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
2. Require a BUY candle to close above a rising EMA(50), or a SELL candle to
   close below a falling EMA(50). EMA slope uses a five-bar lookback.
3. Accept new setups only from 09:30 through 13:30 America/New_York.
4. Place an exact limit at the no-wick candle's opening price.
5. Use the latest confirmed 3-left/3-right pivot plus one tick for the stop.
6. Target `2R`.
7. Allow multiple independent pending orders and open positions per pair.
8. Cancel a pending limit when its signal trend changes. There is no percentage
   band and no three-candle expiry.
9. If a historical bar has ambiguous fill/stop/target ordering, count the stop
   first.
10. Use a deterministic fill ID and durable state so retries do not repost.

The Discord message contains side, symbol, 15m timeframe, exact origin entry,
pivot stop, 2R target, EMA and pivot settings, signal session, London fill time,
and signal ID. It reports fills; it does not place or manage broker orders.

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
two-week reconstruction window for every market concurrently, rebuilds the
EMA, confirmed pivots and pending orders from finalized candles, and persists
signal IDs in a named volume. It shuts
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
5. Set the alert message to `{{strategy.order.alert_message}}`; the filled
   limit order supplies valid Discord JSON.

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

The live strategy is the same trend-filtered origin-limit model used by this
comparison: EMA(50) with a five-bar slope, confirmed 3/3 pivot stops plus one
tick, the 09:30–13:30 New York signal window, 2R targets, multiple pending
orders, and trend-change expiry.

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

## Accuracy and risk notes

- Dukascopy automation uses bid OHLC. Live BUY fills occur on ask, so the raw
  backtest does not model the full spread.
- Historical 15m OHLC cannot reveal intrabar ordering. Same-bar stop/target
  touches are treated conservatively as stops.
- A pattern match is not evidence of an edge. Backtest multiple regimes,
  account for costs, and validate out of sample.
- The live scanner reconstructs pending orders from a rolling two-week history.
  Extremely old orders from a trend lasting longer than that window are outside
  the reconstruction horizon.
- The half-tick tolerance handles representation noise. Raising it toward two
  ticks changes the pattern and should be treated as optimization.
- This is research software and signal delivery, not financial advice or an
  automatic trade-execution system.
