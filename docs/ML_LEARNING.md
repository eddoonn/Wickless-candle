# Wickless machine learning and automated production learning

The machine-learning system is a meta-label layer around the existing deterministic Wickless strategy. It never creates a setup, changes a session, changes the 2R target, moves a stop, changes execution-cost assumptions, or bypasses strategy actionability checks.

## Deployment state

The source of truth is `wickless_ml/registry.json`.

A new installation starts with:

- mode: `shadow`;
- status: `NO_CHAMPION`;
- champion: none.

In this state the scanner behaves exactly like the deterministic production strategy. Missing, stale, incompatible, malformed, or out-of-distribution models fail open to the deterministic strategy and cannot suppress a signal.

## Phase 3: meta-label model

The model estimates the probability that an already valid Wickless setup reaches its target before its stop. It is a deterministic, L2-regularised logistic model with monotonic isotonic probability calibration.

Only information available when the setup is accepted for execution is used:

- candle body ratio, wick size, range divided by ATR, close location and quality score;
- entry displacement, stop distance divided by ATR, spread multiple, risk size and cost-to-risk ratio;
- touch and confirmation bar numbers;
- side, pair, entry model, session phase and cyclical UTC time.

Exit reason, realised R, future bars and other outcome information are excluded from features.

### Chronological model validation

The production-baseline trades from the Phase 1 twelve-fold BID/ASK dataset are divided chronologically:

- August 2025 through March 2026: model fitting;
- April and May 2026: probability calibration and threshold selection;
- June and July 2026: untouched historical holdout.

A challenger requires at least 60 total examples, 35 training examples, 10 calibration examples and 10 holdout examples. Historical promotion requires all configured gates, including probability quality no worse than a constant baseline, bounded calibration error, sufficient filtered coverage and trade count, improved expectancy, non-worse drawdown and non-worse profit factor on the untouched holdout.

A passing challenger becomes a **shadow champion** only. Historical validation can never activate filtering directly.

## Phase 4: automated production learning

`Wickless ML production learning` runs weekly at 01:30 London time, can be dispatched manually, and can be triggered by `wickless_ml/training.request`. It shares the autoresearch concurrency group so the fixed 15-month BID/ASK cache is not rebuilt concurrently with nightly research.

The workflow:

1. Restores or builds the Phase 1 seven-pair BID/ASK dataset.
2. Reconstructs production-baseline trades.
3. Fits and calibrates a challenger.
4. Evaluates the untouched June/July holdout.
5. Runs compilation, the complete test suite and system health.
6. Uploads durable reports before publication.
7. Publishes the model, registry and reports only after validation succeeds.
8. Posts the result and current deployment mode to Discord.

### Live evidence

The scanner records the champion model ID, calibrated probability, threshold, uncertainty, out-of-distribution score, deployment mode and whether the decision was applied.

Target and stop outcomes are recorded for:

- signals sent by the deterministic strategy;
- shadow predictions;
- canary-accepted signals;
- canary- or active-filtered signals, which continue to be tracked counterfactually.

This allows accepted and rejected model decisions to be evaluated without losing the rejected outcome labels.

`Wickless ML live evidence` runs daily at 19:30 London time. It restores the scanner state, calculates calibration, Brier score, coverage, expectancy, drawdown and feature-support drift, then updates the registry.

### Deployment transitions

The default policy is:

- **Shadow → Canary:** at least 50 resolved champion outcomes and every live-evidence gate passes.
- **Canary:** 20% of signals are deterministically assigned to the model cohort by model ID and signal ID. Signals outside the cohort continue through the deterministic strategy.
- **Canary → Active:** at least 30 resolved applied-cohort outcomes and every applied-evidence gate passes.
- **Active:** the model may reject an otherwise valid setup only when the feature vector is inside approved support and its calibrated probability is below the registered threshold.

The transition logic is automatic but evidence-gated. Changing a JSON mode manually is insufficient when the monitor detects an incompatible model, stale release, calibration breach or unsupported feature vector.

### Rollback

Canary or active mode automatically returns to shadow when the recent rolling window breaches either:

- expected calibration error; or
- out-of-distribution rate.

The rollback reason, previous mode, new mode, model ID and evidence metrics are appended to registry history and reported to Discord. A rollback disables model filtering but preserves the deterministic strategy.

## Audit files

- `wickless_ml/policy.json`: immutable learning and deployment thresholds.
- `wickless_ml/registry.json`: champion, challenger, deployment state and transition history.
- `wickless_ml/models/*.json`: reproducible standard-library model artifacts.
- `wickless_ml/reports/*.json`: historical training and holdout reports.
- `wickless_ml/live/*.json`: daily live-evidence and deployment reports.
- `tests/test_ml_learning.py`: leakage, calibration, shadow, canary, active and rollback contracts.

## Safety boundaries

The live scanner applies the deterministic strategy and actionability rules before requesting an ML score. The ML layer cannot modify the candidate surface, evaluator, Phase 1 gates, Phase 2 acquisition model, risk rules or session clocks.

The live scanner workflow retains read-only repository permission. Only the separate training and monitoring workflows may publish model audit state to `main`.
