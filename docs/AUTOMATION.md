# Wickless automation

This repository is designed to operate without routine manual intervention while using one durable branch, `main`, and strict file-scope separation between production code and research audit history.

## Production sessions

Every live scan, production backtest, reference benchmark, bootstrap candidate, and nightly experiment uses the fixed DST-aware union:

- 08:00–17:00 `Europe/London`
- OR 05:00–13:30 `America/New_York`

The two clocks use independent IANA time zones. Candidate parameters cannot shorten, disable, or replace either production window.

## Live scanning

`Live Wickless 15m Discord signals` runs every five minutes.

- Only one scan may run at a time. A newer scheduled run cancels overlapping work.
- Market-data downloads retry automatically.
- Only finalized 15-minute candles and unseen validated entries are posted.
- Signal state is restored and saved across runs.
- Every run uploads a seven-day diagnostic artifact containing the scanner log and paired UTC/London metadata.
- A failed run sends a best-effort Discord failure alert with the Actions URL.

A separate weekday `Wickless scanner health heartbeat` targets 18:45 London time. It evaluates the intended checkpoint rather than the possibly delayed Actions start time, reports `HEALTHY`, `DEGRADED`, or `UNHEALTHY`, and can dispatch one recovery scan when the live scanner is genuinely stale. The heartbeat is operational only; it never implies that a trade signal existed.

## Nightly autoresearch

`Nightly Wickless autoresearch` runs at 23:00 London time and writes validated audit history to protected paths on `main`.

Repository model:

- `main` is the only branch. Production code and audit state are separated by a strict changed-file allowlist.
- Every nightly run records its starting commit, restores the neutral candidate, and rejects production-code changes.
- Production reporting keeps its reviewed June/July policy. Autoresearch uses the separate `autoresearch/walk_forward_policy.json` Phase 1 policy.

### Phase 1 validation

Every candidate is evaluated on 12 chronological monthly folds from August 2025 through July 2026. One cached seven-pair BID/ASK dataset covers May 2025 through July 2026, allowing each fold to record a 90-day historical context window without repeatedly downloading or parsing separate monthly sources.

The deterministic Wickless engine does not fit a statistical model in the context window. The walk-forward metadata records:

- a 90-day historical context window;
- a two-day purge gap before each test month;
- the monthly out-of-sample test window;
- a one-day post-test embargo.

The original June and July minimums remain 10 trades each. Phase 1 also requires:

- at least 60 trades across all 12 folds;
- overall profit factor of at least 1.5;
- overall maximum drawdown no greater than 4R;
- maximum pair share no greater than 0.60;
- at least three distinct pairs;
- no ambiguous exits;
- profitable performance in at least 58% of monthly folds;
- no single profitable fold contributing more than 35% of positive fold profit;
- worst rolling three-fold result no lower than -2R;
- at least 80% deterministic moving-block-bootstrap probability that mean monthly R is positive;
- a 90% bootstrap lower bound for mean monthly R of at least -0.25R;
- at least half of the tested one-step parameter neighbours passing the base gates, with non-negative median total R.

The bootstrap uses deterministic circular moving blocks of three monthly folds. Every report also records a sign-test diagnostic, monthly dispersion, an annualized fold Sharpe diagnostic, and a Bonferroni-style multiple-testing warning based on the append-only experiment history. The multiple-testing value is diagnostic only and can never weaken a promotion gate.

Parameter-neighbourhood checks run only after a candidate passes all base gates, which avoids multiplying computation for clearly invalid candidates. Production-reference candidates remain comparison points and do not require artificial parameter neighbours.

The nightly workflow:

1. Checks out `main` and records the immutable starting commit.
2. Runs experiments in the Actions worktree while enforcing the audit-file allowlist.
3. Restores or builds the fixed 15-month seven-pair BID/ASK dataset.
4. Automatically refreshes the production reference when its release or Phase 1 validation-profile stamp is absent or stale.
5. Plans a diversified batch across candle quality, trend, entry model, and wick detection.
6. Evaluates every candidate with unchanged risk, cost, execution, session, and acceptance rules.
7. Records separate behavior and realized-outcome fingerprints.
8. Classifies each test as `No effect`, `Funnel only`, or `Trade changed`.
9. Runs the bounded coach at its fixed interval.
10. Restores the neutral candidate file.
11. Runs compilation, the complete test suite, health auditing, and changed-file scope verification.
12. Uploads a durable artifact before attempting publication or Discord delivery.
13. Commits one validated audit-state update to `main`, then posts the benchmark-versus-best-trade-changing-test summary.

Effect meanings are strict:

- `No effect`: candidate identity changed, but signal-funnel counters and realized trades were identical.
- `Funnel only`: at least one rejection or eligibility counter changed, but the realized trades and metrics stayed identical.
- `Trade changed`: the realized trade set or resulting metrics changed.

Only `Trade changed` candidates can be presented as the best test. Exact benchmark reproductions and funnel-only changes remain visible in the audit but cannot masquerade as improvements.

A production reference is a comparison point, not a promoted candidate. It may fail current gates; every new candidate must pass all Phase 1 gates and improve the unchanged lexicographic objective.

## Production backtests

`Production Wickless backtest` runs manually, from `ops/backtest.request`, and automatically after changes to production sessions, strategy/evaluator logic, the production policy, the Pine strategy, or the backtest runner.

The workflow:

- retries dataset creation;
- runs compilation, all tests, and the system-health audit;
- evaluates the reviewed June/July BID/ASK data;
- uploads the full report before publication;
- records the production release and session label;
- retries report publication to `main`;
- sends paired UTC/London Discord results with bounded retries.

Report-only commits do not retrigger the workflow.

## Health auditing

`Wickless system health` runs after relevant pushes, on production pull requests, and every morning at 07:15 London time.

It verifies:

- production, production-policy, Phase 1 policy, and reference release identities agree;
- both production sessions are present;
- no locked session parameter can enter regular or bootstrap search;
- the editable candidate surface is neutral;
- nightly research is active rather than paused;
- Phase 1 contains 12 chronological folds, unchanged June/July gates, purge and embargo metadata, consistency gates, and the shared dataset contract;
- the incumbent carries the current Phase 1 profile stamp, reported as a warning while an automatic refresh is pending;
- live scans are single-flight;
- scanner heartbeat recovery remains checkpoint-aware and cannot write repository contents;
- checked-in production reports remain release-current.

Critical invariant failures fail the workflow and send a best-effort Discord alert outside pull requests. JSON health records are retained as Actions artifacts for 30 days.

## Discord delivery

The `main` branch and uploaded artifacts are the source of truth. Discord is a delivery channel.

- Research results are committed and artifacts uploaded before notifications.
- Notifications use bounded retries and disable mentions.
- A missing or rejected webhook does not erase research or backtest output.
- Scanner and system failures include their Actions run URL.

A Discord `403 Forbidden` means the repository secret `DISCORD_WEBHOOK_URL` is invalid or revoked and must be replaced.

## Manual controls

Actions can still be dispatched manually for exceptional cases:

- Nightly batch size: 1–20.
- Force the Phase 1 production-reference refresh.
- Run a complete or limited Phase 1 bootstrap search.
- Run the reviewed production backtest.
- Run scanner heartbeat and recovery validation.
- Run system-health validation.

Routine operation does not require these controls.
