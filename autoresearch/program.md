# Wickless autoresearch worker program

You are the worker in a controlled strategy-research loop. Profitability, stability, and reproducibility matter more than raw trade count.

## Setup

1. Run only inside the nightly Actions worktree checked out from `main`.
2. Record the starting commit before changing candidate or audit files.
3. Read the walk-forward policy, playbook, incumbent, and recent attempts before selecting an idea.
4. Never change production code, evaluator code, validation policy, scoring, gates, tests, or workflows during a research run.

## Phase 1 validation

Every candidate is tested on 12 chronological monthly out-of-sample folds from August 2025 through July 2026 using one immutable BID/ASK dataset. Each fold records a 90-day historical context window, a two-day purge gap, the test month, and a one-day post-test embargo. The deterministic Wickless engine performs no model fitting inside those context windows.

The original June and July minimums remain fixed at 10 trades each. Candidates must also pass annual trade, profit-factor, drawdown, concentration, fold-consistency, moving-block bootstrap, and parameter-neighbourhood robustness gates. Multiple-testing risk is recorded as a diagnostic and never used to weaken a gate.

## Attempt loop

Temporarily edit only the literal `candidate.py`, evaluate it with the Phase 1 walk-forward policy, append the report and ledgers, and keep only candidates that pass every gate and beat the incumbent. Neighbourhood backtests run only after the main candidate passes its base gates. After the batch, restore the neutral production candidate. The workflow verifies the changed-file allowlist before committing one audit update to `main`.

After every 20 cumulative attempts, run the bounded coach. Never weaken success criteria or protected execution rules.
