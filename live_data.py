#!/usr/bin/env python3
"""Download Dukascopy minute candles and aggregate them to 15m OHLC."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from wickless_bot import (
    DATA_TIMEFRAME,
    INSTRUMENTS,
    LIVE_INSTRUMENTS,
    TIMEFRAME_LABEL,
    TIMEFRAME_MINUTES,
    InstrumentProfile,
)


UTC = timezone.utc
JETTA_BASE_URL = "https://jetta.dukascopy.com"
# The strategy needs EMA warm-up, confirmed pivots, and older pending orders
# that remain valid until the EMA trend changes. Two weeks provides a stable
# reconstruction window on every stateless GitHub Actions run.
LIVE_LOOKBACK_MINUTES = 14 * 24 * 60


def _price_digits(multiplier: float) -> int:
    if multiplier <= 0:
        raise ValueError("Candle multiplier must be positive")
    return max(0, int(round(-math.log10(multiplier))))


def _apply_delta(previous: float, delta: float, multiplier: float) -> float:
    return round(previous + delta * multiplier, _price_digits(multiplier))


def decode_minute_candles(payload: dict) -> list[dict[str, float | int]]:
    """Decode Jetta's compact delta-encoded minute-candle response."""

    fields = ("times", "opens", "highs", "lows", "closes")
    arrays = {field: payload.get(field) or [] for field in fields}
    lengths = {len(values) for values in arrays.values()}
    if len(lengths) != 1:
        raise ValueError("Live candle arrays have inconsistent lengths")
    if not arrays["times"]:
        return []

    timestamp = int(payload["timestamp"])
    shift = int(payload.get("shift", 1))
    multiplier = float(payload.get("multiplier", 1))
    previous = {
        "open": float(payload["open"]),
        "high": float(payload["high"]),
        "low": float(payload["low"]),
        "close": float(payload["close"]),
    }
    candles: list[dict[str, float | int]] = []
    for index, time_delta in enumerate(arrays["times"]):
        timestamp += shift * int(time_delta)
        for singular, plural in (
            ("open", "opens"),
            ("high", "highs"),
            ("low", "lows"),
            ("close", "closes"),
        ):
            previous[singular] = _apply_delta(
                previous[singular],
                float(arrays[plural][index]),
                multiplier,
            )
        candles.append({"timestamp": timestamp, **previous})
    return candles


def aggregate_fifteen_minutes(
    minute_candles: Sequence[dict[str, float | int]],
) -> list[dict[str, float | int]]:
    bucket_ms = TIMEFRAME_MINUTES * 60_000
    buckets: dict[int, dict[str, float | int]] = {}
    for candle in sorted(minute_candles, key=lambda item: int(item["timestamp"])):
        timestamp = int(candle["timestamp"])
        bucket_timestamp = timestamp - timestamp % bucket_ms
        if bucket_timestamp not in buckets:
            buckets[bucket_timestamp] = {
                "timestamp": bucket_timestamp,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
            }
            continue
        bucket = buckets[bucket_timestamp]
        bucket["high"] = max(float(bucket["high"]), float(candle["high"]))
        bucket["low"] = min(float(bucket["low"]), float(candle["low"]))
        bucket["close"] = float(candle["close"])
    return [buckets[timestamp] for timestamp in sorted(buckets)]


def _request_json(url: str, retries: int = 3) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "wickless-candle-bot/1.0"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                if response.status != 200:
                    raise RuntimeError(f"Live feed returned HTTP {response.status}")
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("Live feed returned a non-object response")
            return payload
        except (OSError, ValueError, urllib.error.URLError) as error:
            if attempt + 1 == retries:
                raise RuntimeError(f"Live feed request failed: {error}") from error
            time.sleep(2**attempt)
    raise RuntimeError("Live feed request failed")


def fetch_current_day(
    instrument: InstrumentProfile,
    *,
    now: datetime | None = None,
    side: str = "BID",
) -> list[dict[str, float | int]]:
    """Fetch enough history to reconstruct indicators and pending limit orders."""

    now = (now or datetime.now(UTC)).astimezone(UTC)
    side = side.upper()
    if side not in {"BID", "ASK"}:
        raise ValueError("side must be BID or ASK")
    lookback_start = now - timedelta(minutes=LIVE_LOOKBACK_MINUTES)
    from_ms = int(lookback_start.timestamp() * 1000)
    code = urllib.parse.quote(instrument.jetta_code, safe="-")
    query = urllib.parse.urlencode({"from": from_ms})
    url = f"{JETTA_BASE_URL}/v1/candles/minute/{code}/{side}?{query}"
    candles = decode_minute_candles(_request_json(url))
    if not candles:
        raise RuntimeError(f"No live candles returned for {instrument.symbol}")
    return candles


def _write_csv(path: Path, rows: Sequence[dict[str, float | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("timestamp", "open", "high", "low", "close"),
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def refresh(
    data_dir: Path,
    *,
    instruments: Sequence[str] = LIVE_INSTRUMENTS,
    now: datetime | None = None,
    workers: int = 4,
) -> dict[str, Path]:
    """Fetch all instruments concurrently and write one atomic CSV per market."""

    now = (now or datetime.now(UTC)).astimezone(UTC)
    unknown = set(instruments) - set(INSTRUMENTS)
    if unknown:
        raise ValueError(f"Unsupported instruments: {sorted(unknown)}")
    outputs: dict[str, Path] = {}
    errors: dict[str, str] = {}

    side_count = 2 * len(instruments)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, side_count))) as pool:
        futures = {
            pool.submit(
                fetch_current_day,
                INSTRUMENTS[key],
                now=now,
                side=side,
            ): (key, side)
            for key in instruments
            for side in ("BID", "ASK")
        }
        market_rows: dict[str, dict[str, list[dict[str, float | int]]]] = {}
        observed_times: dict[str, dict[str, int]] = {}
        for future in as_completed(futures):
            key, side = futures[future]
            try:
                minute_rows = future.result()
                if not minute_rows:
                    raise RuntimeError("feed returned no minute candles")
                observed_times.setdefault(key, {})[side] = int(
                    minute_rows[-1]["timestamp"]
                )
                rows = aggregate_fifteen_minutes(minute_rows)
                if not rows:
                    raise RuntimeError(
                        f"aggregation returned no {TIMEFRAME_MINUTES}-minute candles"
                    )
                output = data_dir / (
                    f"{key}-{DATA_TIMEFRAME}-{side.lower()}-live.csv"
                )
                _write_csv(output, rows)
                market_rows.setdefault(key, {})[side] = rows
                if side == "BID":
                    outputs[key] = output
                newest = datetime.fromtimestamp(int(rows[-1]["timestamp"]) / 1000, UTC)
                print(
                    f"{INSTRUMENTS[key].symbol} {side}: {len(rows)} bars through "
                    f"{newest.isoformat()}"
                )
            except (OSError, ValueError, RuntimeError) as error:
                errors[f"{key}-{side.lower()}"] = str(error)
    if errors:
        detail = "; ".join(f"{key}: {value}" for key, value in sorted(errors.items()))
        raise RuntimeError(f"Live refresh failed for {len(errors)} market(s): {detail}")
    for key in instruments:
        sides = market_rows.get(key, {})
        if set(sides) != {"BID", "ASK"}:
            raise RuntimeError(f"Live refresh did not return BID and ASK for {key}")
        bid = sides["BID"][-1]
        ask = sides["ASK"][-1]
        if observed_times[key]["BID"] != observed_times[key]["ASK"]:
            raise RuntimeError(
                f"BID/ASK quote timestamps do not match for {INSTRUMENTS[key].symbol}"
            )
        observed_ms = observed_times[key]["BID"]
        bid_close = float(bid["close"])
        ask_close = float(ask["close"])
        if ask_close < bid_close:
            raise RuntimeError(f"Invalid negative spread for {INSTRUMENTS[key].symbol}")
        _write_json(
            data_dir / f"{key}-quote-live.json",
            {
                "instrument": key,
                "observed_time_utc": datetime.fromtimestamp(
                    observed_ms / 1000, UTC
                ).isoformat(),
                "bid": bid_close,
                "ask": ask_close,
                "spread": ask_close - bid_close,
                "source": "Dukascopy Jetta BID/ASK minute candles",
            },
        )
    return outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(".runtime-data"))
    parser.add_argument(
        "--instruments",
        nargs="+",
        choices=tuple(INSTRUMENTS),
        default=list(LIVE_INSTRUMENTS),
    )
    parser.add_argument("--workers", type=int, default=4)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        refresh(
            args.data_dir,
            instruments=args.instruments,
            workers=args.workers,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
