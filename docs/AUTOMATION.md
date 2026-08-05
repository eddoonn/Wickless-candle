# Wickless automation

This repository is designed to operate without routine manual intervention while preserving strict separation between production code and research audit history.

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

A separate weekday `Wickless scanner health heartbeat` runs at 18:45 London time. It reads the actual live-workflow history and reports whether the most recent scan completed successfully within 20 minutes. The heartbeat is operational only; it never implies that a trade signal existed.

## Nightly autoresearch

`Nightly Wickless autoresearch` runs at 23:00 London time and writes audit history only to `autoresearch/nightly`.

Branch roles:

- `main`: production code and user-facing workflows.
- `autoresearch/framework-v1`: fixed evaluator, planner, tests, policy, and automation framework.
- `autoresearch/nightly`: append-only experiment results and generated audit artifacts.

The nightly workflow:

1. Checks out the fixed framework.
2. Resumes the isolated nightly branch and merges the framework forward.
3. Restores or builds the fixed June/July seven-pair BID/ASK datasets.
4. Automatically refreshes the production reference when its release stamp is absent or stale.
5. Plans a diversified batch across candle quality, trend, entry model, and wick detection.
6. Evaluates every candidate with unchanged risk, cost, execution, and acceptance gates.
7. Records a behavior fingerprint, distinguishing changed experiments from exact no-effect reproductions.
8. Runs the coach at its fixed interval.
9. Restores the neutral candidate file.
10. Runs compilation, the complete test suite, health auditing, and protected-scope verification.
11. Uploads a durable artifact before attempting branch publication or Discord delivery.
12. Pushes only `autoresearch/nightly`, then posts a benchmark-versus-best-changed-test Discord summary.

Exact ties are not described as a best test. They are counted as `No effect`, and the nightly summary selects the strongest experiment only from candidates whose behavior changed.

## Acceptance gates

Candidate gates remain unchanged:

- At least 10 June trades.
- At least 10 July trades.
- At least 11 overall trades.
- Overall profit factor at least 1.5.
- Overall maximum drawdown no greater than 4R.
- Maximum pair share no greater than 0.60.
- At least three distinct pairs.
- No ambiguous exits.
- Objective improvement when promotion is considered.

A production reference is a comparison point, not a promoted candidate. It may fail current gates; every new candidate must still pass them.

## Production backtests

`Production Wickless backtest` runs manually, from `ops/backtest.request`, and automatically after changes to production sessions, strategy/evaluator logic, policy, the Pine strategy, or the backtest runner.

The workflow:

- retries dataset creation;
- runs compilation, all tests, and the system-health audit;
- evaluates the fixed June/July BID/ASK data;
- uploads the full report before publication;
- records the production release and session label;
- retries report publication to `main`;
- sends paired UTC/London Discord results with bounded retries.

Report-only commits do not retrigger the workflow.

## Health auditing

`Wickless system health` runs after relevant pushes and every morning at 07:15 London time.

It verifies:

- production and reference release identities agree;
- both production sessions are present;
- no locked session parameter can enter regular or bootstrap search;
- the editable candidate surface is neutral;
- nightly research is active rather than paused;
- live scans are single-flight;
- checked-in reports and incumbents are release-current, reported as warnings when refresh is pending.

Critical invariant failures fail the workflow and send a best-effort Discord alert. JSON health records are retained as Actions artifacts for 30 days.

## Discord delivery

GitHub branches and uploaded artifacts are the source of truth. Discord is a delivery channel.

- Research results are committed and artifacts uploaded before notifications.
- Notifications use bounded retries and disable mentions.
- A missing or rejected webhook does not erase research or backtest output.
- Scanner and system failures include their Actions run URL.

A Discord `403 Forbidden` means the repository secret `DISCORD_WEBHOOK_URL` is invalid or revoked and must be replaced.

## Manual controls

Actions can still be dispatched manually for exceptional cases:

- Nightly batch size: 1–20.
- Force production-reference refresh.
- Run a complete or limited bootstrap search.
- Run production backtest.
- Run scanner heartbeat.
- Run system-health validation.

Routine operation does not require these controls.
