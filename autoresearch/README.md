# Wickless autoresearch

This directory runs controlled Wickless strategy experiments without allowing the
research loop to modify production code or weaken execution safeguards.

## Fixed boundary

- Production is pinned to commit `12250ed9e7698e9b7e57341f09d23372cb7ba1cc`.
- The evaluator consumes observed Dukascopy M1 BID/ASK archives and builds only
  complete 15-minute bars.
- `candidate.py` is parsed as a literal and is never imported or executed.
- Reward/risk, stop mode and bounds, spread and cost limits, slippage, quality
  enforcement, and one-position-per-pair remain immutable.
- `policy.json`, the evaluator, objective order, and acceptance gates are never
  changed by either the worker or the coach.
- Research writes stay on `autoresearch/...` branches; the loop never updates
  `main` or the live scanner.

## Scoring

The policy evaluates June and July 2026. A candidate must pass every hard gate.
Passing is not enough: candidates are compared lexicographically by worst-fold
net R, total net R, profit factor, lower drawdown, then total trades.

The coach does not create or alter that score. It changes only the order in
which untried candidate ideas are selected.

## Worker attempt log

Every non-dry experiment appends exactly one CSV record to `attempts.log`:

```text
timestamp,one-sentence description,category,score,KEPT or DISCARDED
```

The score field records the existing lexicographic objective. The file is opened
only in append mode, flushed, and synced after every attempt. Full reports and
the tamper-evident hash chain remain in `runs/*.json` and `results.jsonl`.

The high-level categories are candle quality, trend filter, session window,
entry geometry, and wick detection. Two-category experiments are logged with a
`+` separator so the coach can see repeated search patterns.

## Coach

`coach.py` reads `attempts.log` and `playbook.md` after every 20 cumulative
worker attempts. It counts attempted categories, identifies categories that
produced kept improvements, reports untried categories, and checks whether the
last ten attempts improved the incumbent.

When recent attempts are still improving, it advances its checkpoint and leaves
`playbook.md` unchanged. When the last ten attempts are flat, categories with at
least five attempts and zero kept improvements move into `Do not try`, and two
or three materially different underexplored categories move into `Explore next`.
The generated playbook is always under 40 lines.

`coach_state.json` stores only the last reviewed attempt count. It also detects
an attempt log that appears to have been truncated.

Run the coach manually with:

```bash
python -m autoresearch.coach --force
```

## Run one experiment

The data root must contain the two directories named in `policy.json`, then run:

```bash
python -m autoresearch.run_experiment --data-root /path/to/datasets
```

Exit code `0` means keep and exit code `3` means discard. Every non-dry run
writes its full report, updates the incumbent only on a keep, appends the
hash-chained result ledger, and appends one worker-attempt line.

## Nightly worker and coach loop

The GitHub workflow starts at **23:00 Europe/London** and normally runs 12
experiments. The 20-attempt coach cadence is cumulative, so the coach can run
mid-batch on a later night. After a coach pass, the very next proposal selection
reads the rewritten playbook.

The persistent `autoresearch/nightly` branch receives the candidate, result
ledger, attempt log, coach checkpoint, playbook, and full reports. Discord gets
an immediate message for each new keep, a nightly worker/coach summary, and a
failure alert if the workflow crashes.

See `program.md` for the worker procedure and `coach_prompt.md` for the coach
contract.
