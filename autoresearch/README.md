# Wickless autoresearch

This directory runs controlled strategy experiments while protecting production code and execution safeguards.

## Single-branch boundary

- The repository has one durable branch: `main`.
- The nightly workflow records its starting commit and rejects worktree changes outside the audit allowlist.
- Durable research state is limited to `candidate.py`, `incumbent.json`, `results.jsonl`, `attempts.log`, `playbook.md`, `coach_state.json`, and JSON reports under `runs/`.
- `candidate.py` is restored to the neutral production baseline before publication.
- Production code, evaluator code, policy, scoring, tests, risk limits, and execution safeguards remain immutable during experiments.

## Scoring and operation

June and July 2026 are evaluated independently. Candidates must pass every hard gate and beat the incumbent objective. Every attempt appends one tamper-evident ledger record and one append-only attempt row. After every 20 cumulative attempts, the deterministic coach may update only bounded search guidance.

The scheduled workflow runs at 23:00 Europe/London, normally evaluates 12 diversified candidates, uploads a durable artifact, commits one validated audit-state update to `main`, and then sends Discord. The `main` branch and Actions artifacts are the source of truth.
