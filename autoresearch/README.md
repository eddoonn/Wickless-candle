# Wickless autoresearch

This directory adapts the controlled experiment loop from `eddoonn/autoresearch`
to the Wickless strategy without allowing an experiment to modify production or
weaken execution safeguards.

## Boundary

- Production is pinned to commit `12250ed9e7698e9b7e57341f09d23372cb7ba1cc`.
- The fixed evaluator consumes observed Dukascopy M1 BID/ASK archives and builds
  complete 15-minute bars.
- `candidate.py` is the only strategy surface an experiment agent may edit. It
  is parsed as a literal and never imported or executed.
- Reward/risk, stop mode and buffer, ATR stop floors/ceiling, spread multiple,
  slippage, execution-cost cap, quality enforcement, and one-position-per-pair
  remain immutable.
- `main` is never updated by the loop. Work stays on branches named
  `autoresearch/<run-tag>`.

## Evaluation

The default policy uses June 2026 and July 2026. July remains the required
consistency benchmark with at least 10 closed trades. A candidate must also be
net profitable in each fold, retain pair breadth, stay under the drawdown and
concentration caps, and have no ambiguous exits.

Passing the hard gates is not enough. Candidates are compared lexicographically
in this order:

1. worst-fold net R;
2. total net R;
3. overall profit factor;
4. lower overall drawdown;
5. total trades.

This makes profitability and regime consistency more important than raw signal
count.

## Run one experiment

The data root must contain these directories:

```text
dukascopy_m1_bidask_2026-06/
dukascopy_m1_bidask_2026-07/
```

Then run:

```bash
python -m autoresearch.run_experiment --data-root /path/to/datasets
```

Exit code `0` means keep. Exit code `3` means discard. Each non-dry run writes a
full trade-by-trade JSON report, updates the incumbent only on a keep, and
appends a hash-chained line to `results.jsonl`.

See `program.md` for the autonomous agent procedure.

