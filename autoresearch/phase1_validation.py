"""Phase 1 walk-forward, uncertainty, and robustness validation.

This module decorates the immutable evaluator. It never changes strategy execution,
risk, costs, sessions, or candidate permissions.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator


HERE = Path(__file__).resolve().parent
NEIGHBOUR_STEPS: dict[str, float] = {
    "minimum_body_ratio": 0.02,
    "minimum_range_atr": 0.04,
    "maximum_range_atr": 0.10,
    "close_location_fraction": 0.01,
    "ema_length": 5,
    "ema_slope_lookback": 1,
    "tolerance_ticks": 0.50,
    "maximum_wick_ticks": 0.25,
    "expiry_bars": 1,
    "origin_zone_atr_fraction": 0.02,
    "origin_zone_minimum_ticks": 1,
    "reclaim_buffer_ticks": 1,
    "maximum_entry_displacement_atr": 0.05,
}


def policy_profile_sha256(policy: dict[str, Any]) -> str:
    payload = json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _phase1(policy: dict[str, Any]) -> dict[str, Any] | None:
    value = policy.get("phase1_validation")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("phase1_validation must be an object")
    required = {
        "profile",
        "training_window_days",
        "purge_days",
        "embargo_days",
        "bootstrap_samples",
        "bootstrap_block_size",
        "confidence_level",
        "maximum_neighbour_variants",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("phase1_validation is missing: " + ", ".join(missing))
    if len(policy.get("folds", [])) < 12:
        raise ValueError("Phase 1 requires at least 12 chronological folds")
    return value


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a percentile of an empty sequence")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _moving_block_means(
    values: list[float], *, samples: int, block_size: int
) -> list[float]:
    if not values:
        return []
    encoded = json.dumps(values, separators=(",", ":"), allow_nan=False)
    seed = int(hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    width = max(1, min(block_size, len(values)))
    means: list[float] = []
    for _ in range(samples):
        sample: list[float] = []
        while len(sample) < len(values):
            start = generator.randrange(len(values))
            sample.extend(values[(start + offset) % len(values)] for offset in range(width))
        means.append(statistics.mean(sample[: len(values)]))
    return means


def validation_diagnostics(
    fold_net_r: list[float], *, phase1: dict[str, Any]
) -> dict[str, Any]:
    if not fold_net_r:
        raise ValueError("Phase 1 diagnostics require at least one fold")
    positive = [value for value in fold_net_r if value > 0]
    nonnegative = [value for value in fold_net_r if value >= 0]
    positive_total = sum(positive)
    largest_share = max(positive) / positive_total if positive_total > 0 else 1.0
    rolling_width = min(3, len(fold_net_r))
    rolling = [
        sum(fold_net_r[index : index + rolling_width])
        for index in range(len(fold_net_r) - rolling_width + 1)
    ]
    bootstrap = _moving_block_means(
        fold_net_r,
        samples=int(phase1["bootstrap_samples"]),
        block_size=int(phase1["bootstrap_block_size"]),
    )
    confidence = float(phase1["confidence_level"])
    tail = (1.0 - confidence) / 2.0
    probability_positive = (
        sum(value > 0 for value in bootstrap) / len(bootstrap) if bootstrap else 0.0
    )
    profitable_count = len(positive)
    sign_test_p = sum(
        math.comb(len(fold_net_r), successes)
        for successes in range(profitable_count, len(fold_net_r) + 1)
    ) / (2 ** len(fold_net_r))
    deviation = statistics.stdev(fold_net_r) if len(fold_net_r) > 1 else 0.0
    mean = statistics.mean(fold_net_r)
    annualized_sharpe = mean / deviation * math.sqrt(12) if deviation > 0 else None
    return {
        "fold_count": len(fold_net_r),
        "fold_net_r": fold_net_r,
        "profitable_fold_count": profitable_count,
        "profitable_fold_ratio": profitable_count / len(fold_net_r),
        "nonnegative_fold_ratio": len(nonnegative) / len(fold_net_r),
        "mean_fold_net_r": mean,
        "median_fold_net_r": statistics.median(fold_net_r),
        "fold_net_r_stdev": deviation,
        "annualized_fold_sharpe": annualized_sharpe,
        "largest_profitable_fold_share": largest_share,
        "worst_rolling_three_fold_net_r": min(rolling),
        "one_sided_sign_test_p_value": sign_test_p,
        "bootstrap": {
            "method": "deterministic-circular-moving-block",
            "samples": len(bootstrap),
            "block_size": int(phase1["bootstrap_block_size"]),
            "confidence_level": confidence,
            "mean_fold_net_r_lower": _percentile(bootstrap, tail),
            "mean_fold_net_r_upper": _percentile(bootstrap, 1.0 - tail),
            "probability_mean_positive": probability_positive,
        },
    }


def multiple_testing_diagnostic(
    probability_mean_positive: float, *, prior_trials: int
) -> dict[str, Any]:
    trials = max(1, prior_trials + 1)
    raw_p = max(0.0, min(1.0, 1.0 - probability_mean_positive))
    bonferroni_p = min(1.0, raw_p * trials)
    adjusted_confidence = 1.0 - bonferroni_p
    if adjusted_confidence >= 0.80:
        risk = "low"
    elif adjusted_confidence >= 0.50:
        risk = "moderate"
    else:
        risk = "high"
    return {
        "prior_candidate_trials": prior_trials,
        "trials_including_current": trials,
        "raw_probability_mean_positive": probability_mean_positive,
        "bonferroni_adjusted_confidence": adjusted_confidence,
        "selection_bias_risk": risk,
        "promotion_gate": False,
    }


def _ledger_trial_count(path: Path = HERE / "results.jsonl") -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("category") != "baseline":
            count += 1
    return count


def _walk_forward_metadata(
    fold_row: dict[str, Any], *, evaluator: Any, phase1: dict[str, Any]
) -> dict[str, Any]:
    test_start = evaluator._parse_utc(fold_row["start_utc"])
    test_end = evaluator._parse_utc(fold_row["end_utc_exclusive"])
    purge = int(phase1["purge_days"])
    embargo = int(phase1["embargo_days"])
    training_end = test_start - timedelta(days=purge)
    training_start = training_end - timedelta(days=int(phase1["training_window_days"]))
    return {
        "training_start_utc": training_start.isoformat(),
        "training_end_utc_exclusive": training_end.isoformat(),
        "purge_days": purge,
        "test_start_utc": test_start.isoformat(),
        "test_end_utc_exclusive": test_end.isoformat(),
        "embargo_end_utc_exclusive": (test_end + timedelta(days=embargo)).isoformat(),
        "embargo_days": embargo,
        "fitting_performed": False,
        "role": "chronological-out-of-sample-validation",
    }


def _bounded_value(value: float, lower: float, upper: float, expected: type) -> Any:
    bounded = min(upper, max(lower, value))
    if expected is int:
        return int(round(bounded))
    return round(float(bounded), 10)


def neighbour_parameter_sets(evaluator: Any, candidate: Any, maximum: int) -> list[dict[str, Any]]:
    defaults = evaluator.NoWickConfig()
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in sorted(candidate.parameters):
        value = candidate.parameters[key]
        expected, lower_or_choices, upper = evaluator.ALLOWED_PARAMETERS[key]
        alternatives: list[Any] = []
        if key in NEIGHBOUR_STEPS and expected in {int, float}:
            step = NEIGHBOUR_STEPS[key]
            alternatives = [
                _bounded_value(float(value) - step, lower_or_choices, upper, expected),
                _bounded_value(float(value) + step, lower_or_choices, upper, expected),
            ]
        elif expected is bool:
            alternatives = [not bool(value)]
        elif expected is str and isinstance(lower_or_choices, set):
            default_value = getattr(defaults, key)
            alternatives = [default_value] + sorted(lower_or_choices - {value, default_value})
        for alternative in alternatives:
            if alternative == value:
                continue
            parameters = dict(candidate.parameters)
            parameters[key] = alternative
            try:
                normalized = evaluator.validate_parameters(parameters)
            except evaluator.CandidateError:
                continue
            rendered = {
                name: item.isoformat(timespec="minutes") if hasattr(item, "isoformat") else item
                for name, item in normalized.items()
            }
            signature = json.dumps(rendered, sort_keys=True, separators=(",", ":"))
            if signature in seen:
                continue
            seen.add(signature)
            variants.append(parameters)
            if len(variants) >= maximum:
                return variants
    return variants


@contextmanager
def _cached_fold_loader(evaluator: Any) -> Iterator[None]:
    original = evaluator._load_fold_data
    cache: dict[tuple[str, str], tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    def cached(data_root: Path, fold: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        key = (str(data_root.resolve()), fold.directory)
        if key not in cache:
            cache[key] = original(data_root, fold)
        return cache[key]

    evaluator._load_fold_data = cached
    try:
        yield
    finally:
        evaluator._load_fold_data = original


def _enhance_report(
    report: dict[str, Any],
    *,
    policy: dict[str, Any],
    evaluator: Any,
    phase1: dict[str, Any],
    prior_trials: int,
) -> dict[str, Any]:
    fold_rows = {row["name"]: row for row in policy["folds"]}
    fold_net_r: list[float] = []
    for row in policy["folds"]:
        name = row["name"]
        report["folds"][name]["walk_forward"] = _walk_forward_metadata(
            fold_rows[name], evaluator=evaluator, phase1=phase1
        )
        fold_net_r.append(float(report["folds"][name]["metrics"]["net_r"]))
    diagnostics = validation_diagnostics(fold_net_r, phase1=phase1)
    diagnostics["multiple_testing"] = multiple_testing_diagnostic(
        diagnostics["bootstrap"]["probability_mean_positive"],
        prior_trials=prior_trials,
    )
    report["schema_version"] = 2
    report["validation"] = {
        "profile": phase1["profile"],
        "profile_sha256": policy_profile_sha256(policy),
        "dataset_directory": policy["folds"][0]["directory"],
        "diagnostics": diagnostics,
    }
    acceptance = policy["acceptance"]
    checks = report["acceptance_gates"]["checks"]
    checks.update(
        {
            "minimum_profitable_fold_ratio": diagnostics["profitable_fold_ratio"]
            >= float(acceptance["minimum_profitable_fold_ratio"]),
            "maximum_single_fold_profit_share": diagnostics[
                "largest_profitable_fold_share"
            ]
            <= float(acceptance["maximum_single_fold_profit_share"]),
            "minimum_worst_rolling_three_fold_net_r": diagnostics[
                "worst_rolling_three_fold_net_r"
            ]
            >= float(acceptance["minimum_worst_rolling_three_fold_net_r"]),
            "minimum_bootstrap_probability_mean_positive": diagnostics["bootstrap"][
                "probability_mean_positive"
            ]
            >= float(acceptance["minimum_bootstrap_probability_mean_positive"]),
            "minimum_bootstrap_mean_fold_net_r_lower": diagnostics["bootstrap"][
                "mean_fold_net_r_lower"
            ]
            >= float(acceptance["minimum_bootstrap_mean_fold_net_r_lower"]),
        }
    )
    report["acceptance_gates"]["passed"] = all(checks.values())
    return report


def _evaluate_neighbourhood(
    candidate: Any,
    *,
    data_root: Path,
    policy: dict[str, Any],
    evaluator: Any,
    original_evaluate: Any,
    phase1: dict[str, Any],
    prior_trials: int,
) -> dict[str, Any]:
    variants = neighbour_parameter_sets(
        evaluator, candidate, int(phase1["maximum_neighbour_variants"])
    )
    reports: list[dict[str, Any]] = []
    for index, parameters in enumerate(variants, 1):
        encoded = json.dumps(parameters, sort_keys=True, default=str, separators=(",", ":"))
        neighbour = evaluator.Candidate(
            name=f"robust-{index:02d}",
            description=f"One-step robustness neighbour {index} for {candidate.name}.",
            parameters=evaluator.validate_parameters(parameters),
            source_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        )
        report = original_evaluate(neighbour, data_root=data_root, policy=policy)
        reports.append(
            _enhance_report(
                report,
                policy=policy,
                evaluator=evaluator,
                phase1=phase1,
                prior_trials=prior_trials,
            )
        )
    pass_count = sum(row["acceptance_gates"]["passed"] for row in reports)
    pass_rate = pass_count / len(reports) if reports else 0.0
    net_values = [float(row["overall"]["net_r"]) for row in reports]
    median_net = statistics.median(net_values) if net_values else float("-inf")
    minimum_pass_rate = float(policy["acceptance"]["minimum_neighbourhood_pass_rate"])
    minimum_median = float(policy["acceptance"]["minimum_neighbourhood_median_net_r"])
    return {
        "status": "evaluated" if reports else "no-valid-neighbours",
        "variant_count": len(reports),
        "passing_variants": pass_count,
        "pass_rate": pass_rate,
        "median_overall_net_r": median_net if net_values else None,
        "worst_overall_net_r": min(net_values) if net_values else None,
        "required_pass_rate": minimum_pass_rate,
        "required_median_overall_net_r": minimum_median,
        "passed": bool(reports)
        and pass_rate >= minimum_pass_rate
        and median_net >= minimum_median,
        "variants": [
            {
                "parameters": row["candidate"]["parameters"],
                "passed": row["acceptance_gates"]["passed"],
                "overall_net_r": row["overall"]["net_r"],
                "profit_factor": row["overall"]["profit_factor"],
                "maximum_drawdown_r": row["overall"]["maximum_drawdown_r"],
            }
            for row in reports
        ],
    }


def install_phase1_validation() -> None:
    """Patch the evaluator once while preserving legacy production-policy behaviour."""

    from autoresearch import evaluator

    if getattr(evaluator, "_PHASE1_VALIDATION_INSTALLED", False):
        return
    original_evaluate = evaluator.evaluate
    original_load_policy = evaluator.load_policy

    def load_policy(path: Path = evaluator.DEFAULT_POLICY) -> dict[str, Any]:
        policy = original_load_policy(path)
        _phase1(policy)
        return policy

    def evaluate(
        candidate: Any,
        *,
        data_root: Path,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = policy or load_policy()
        phase1 = _phase1(resolved)
        if phase1 is None:
            return original_evaluate(candidate, data_root=data_root, policy=resolved)
        prior_trials = _ledger_trial_count()
        with _cached_fold_loader(evaluator):
            report = original_evaluate(candidate, data_root=data_root, policy=resolved)
            report = _enhance_report(
                report,
                policy=resolved,
                evaluator=evaluator,
                phase1=phase1,
                prior_trials=prior_trials,
            )
            base_passed = report["acceptance_gates"]["passed"]
            if not candidate.parameters:
                robustness = {"status": "not-applicable-production-reference", "passed": True}
            elif not base_passed:
                robustness = {"status": "not-run-base-gates-failed", "passed": False}
            else:
                robustness = _evaluate_neighbourhood(
                    candidate,
                    data_root=data_root,
                    policy=resolved,
                    evaluator=evaluator,
                    original_evaluate=original_evaluate,
                    phase1=phase1,
                    prior_trials=prior_trials,
                )
            report["validation"]["neighbourhood_robustness"] = robustness
            report["acceptance_gates"]["checks"]["neighbourhood_robustness"] = bool(
                robustness["passed"]
            )
            report["acceptance_gates"]["passed"] = all(
                report["acceptance_gates"]["checks"].values()
            )
            return report

    evaluator.load_policy = load_policy
    evaluator.evaluate = evaluate
    evaluator._PHASE1_VALIDATION_INSTALLED = True
