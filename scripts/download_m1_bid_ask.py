#!/usr/bin/env python3
"""Download Dukascopy ticks and aggregate two-sided one-minute OHLC archives."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import http.client
import json
import lzma
import os
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


UTC = timezone.utc
RECORD = struct.Struct("!IIIff")
URL = (
    "https://www.dukascopy.com/datafeed/"
    "{symbol}/{year}/{month:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
)
USER_AGENT = "wickless-autoresearch/1 reusable-m1-cache"


@dataclass(frozen=True)
class Job:
    symbol: str
    timestamp: datetime

    @property
    def url(self) -> str:
        return URL.format(
            symbol=self.symbol,
            year=self.timestamp.year,
            month=self.timestamp.month - 1,
            day=self.timestamp.day,
            hour=self.timestamp.hour,
        )


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def market_hours(start: date, end: date) -> list[datetime]:
    hours: list[datetime] = []
    current = start
    while current <= end:
        weekday = current.weekday()
        if weekday == 6:
            selected = range(21, 24)
        elif weekday < 4:
            selected = range(24)
        elif weekday == 4:
            selected = range(21)
        else:
            selected = ()
        hours.extend(
            datetime(current.year, current.month, current.day, hour, tzinfo=UTC)
            for hour in selected
        )
        current += timedelta(days=1)
    return hours


def price_scale(symbol: str) -> int:
    return 1_000 if symbol.endswith("JPY") else 100_000


def fetch(job: Job, cache_dir: Path, retries: int, timeout: int) -> tuple[Job, Path, str]:
    target = cache_dir / job.symbol / f"{job.timestamp:%Y%m%d%H}.bi5"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return job, target, "cached"
    request = urllib.request.Request(job.url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            temporary = target.with_suffix(".part")
            temporary.write_bytes(payload)
            os.replace(temporary, target)
            return job, target, "downloaded"
        except urllib.error.HTTPError as error:
            if error.code == 404 or error.code in {301, 302, 303, 307, 308}:
                target.write_bytes(b"")
                return job, target, "empty"
            last_error = error
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            last_error = error
        time.sleep(min(8, 0.5 * (2**attempt)))
    raise RuntimeError(f"Failed {job.url}: {last_error}")


def decode(path: Path, job: Job):
    payload = path.read_bytes()
    if not payload:
        return
    raw = lzma.decompress(payload)
    if len(raw) % RECORD.size:
        raise ValueError(f"Truncated BI5 records: {path}")
    scale = price_scale(job.symbol)
    for offset in range(0, len(raw), RECORD.size):
        milliseconds, ask, bid, ask_volume, bid_volume = RECORD.unpack_from(raw, offset)
        yield (
            job.timestamp + timedelta(milliseconds=milliseconds),
            bid / scale,
            ask / scale,
            bid_volume,
            ask_volume,
        )


def aggregate(paths: list[tuple[Job, Path]], output: Path) -> dict[str, object]:
    bars: dict[datetime, dict[str, float]] = {}
    ticks = 0
    for job, path in sorted(paths, key=lambda item: item[0].timestamp):
        for timestamp, bid, ask, bid_volume, ask_volume in decode(path, job):
            minute = timestamp.replace(second=0, microsecond=0)
            bar = bars.get(minute)
            if bar is None:
                bars[minute] = {
                    "bid_open": bid,
                    "bid_high": bid,
                    "bid_low": bid,
                    "bid_close": bid,
                    "ask_open": ask,
                    "ask_high": ask,
                    "ask_low": ask,
                    "ask_close": ask,
                    "bid_volume": float(bid_volume),
                    "ask_volume": float(ask_volume),
                    "ticks": 1,
                }
            else:
                bar["bid_high"] = max(bar["bid_high"], bid)
                bar["bid_low"] = min(bar["bid_low"], bid)
                bar["bid_close"] = bid
                bar["ask_high"] = max(bar["ask_high"], ask)
                bar["ask_low"] = min(bar["ask_low"], ask)
                bar["ask_close"] = ask
                bar["bid_volume"] += float(bid_volume)
                bar["ask_volume"] += float(ask_volume)
                bar["ticks"] += 1
            ticks += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = (
        "timestamp_utc", "bid_open", "bid_high", "bid_low", "bid_close",
        "ask_open", "ask_high", "ask_low", "ask_close", "spread_open",
        "spread_close", "bid_volume", "ask_volume", "tick_count",
    )
    with gzip.open(output, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for minute in sorted(bars):
            bar = bars[minute]
            writer.writerow(
                {
                    "timestamp_utc": minute.isoformat().replace("+00:00", "Z"),
                    **{key: f"{bar[key]:.5f}" for key in columns[1:9]},
                    "spread_open": f"{bar['ask_open'] - bar['bid_open']:.5f}",
                    "spread_close": f"{bar['ask_close'] - bar['bid_close']:.5f}",
                    "bid_volume": f"{bar['bid_volume']:.2f}",
                    "ask_volume": f"{bar['ask_volume']:.2f}",
                    "tick_count": int(bar["ticks"]),
                }
            )
    timestamps = sorted(bars)
    return {
        "file": output.name,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "compressed_bytes": output.stat().st_size,
        "minutes": len(timestamps),
        "ticks": ticks,
        "first_timestamp_utc": timestamps[0].isoformat() if timestamps else None,
        "last_timestamp_utc": timestamps[-1].isoformat() if timestamps else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--start", required=True, type=parse_day)
    parser.add_argument("--end", required=True, type=parse_day)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    symbols = [symbol.upper() for symbol in args.symbols]
    jobs = [
        Job(symbol, timestamp)
        for symbol in symbols
        for timestamp in market_hours(args.start, args.end)
    ]
    completed: dict[str, list[tuple[Job, Path]]] = {symbol: [] for symbol in symbols}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(fetch, job, args.cache, args.retries, args.timeout): job
            for job in jobs
        }
        for number, future in enumerate(as_completed(futures), 1):
            try:
                job, path, _ = future.result()
            except Exception as error:
                failures.append(str(error))
                continue
            completed[job.symbol].append((job, path))
            if number % 250 == 0 or number == len(jobs):
                print(f"hours {number}/{len(jobs)}", flush=True)
    if failures:
        print(f"FAILED_HOURS {len(failures)}", flush=True)
        for line in failures[:10]:
            print(line, flush=True)
    manifests = []
    for symbol in symbols:
        filename = f"{symbol}_M1_BIDASK_{args.start}_{args.end}.csv.gz"
        manifests.append(
            {"symbol": symbol, **aggregate(completed[symbol], args.output / filename)}
        )
    manifest = {
        "provider": "Dukascopy",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "requested_start": args.start.isoformat(),
        "requested_end": args.end.isoformat(),
        "files": manifests,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
