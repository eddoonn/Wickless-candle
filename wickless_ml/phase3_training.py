"""Phase 3 model-family selection, calibration, abstention, and registration."""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from autoresearch import evaluator
from autoresearch.phase1_validation import policy_profile_sha256
from production_session import PRODUCTION_RELEASE_SHA
from wickless_ml.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION
from wickless_ml.model import (
    classification_metrics,
    finalize_model,
    fit_isotonic_calibrator,
    raw_probability as logistic_raw_probability,
    trade_metrics,
    train_logistic_model,
)
from wickless_ml.model_v2 import (
    FAMILY as BOOSTED_FAMILY,
    raw_probability as boosted_raw_probability,
    score_model,
    train_boosted_stumps,
)
from wickless_ml.training import (
    DatasetRow,
    _dataset_digest,
    _initial_registry,
    _promotion_score,
    _read_json,
    _write_json,
    build_dataset,
    chronological_split,
)

UTC = timezone.utc
HERE = Path(__file__).resolve().parent
LOGISTIC_FAMILY = "logistic_regression"


def calibration_subsplit(rows: Sequence[DatasetRow], fold_names: Sequence[str]):
    if len(fold_names) != 2:
        raise ValueError("Phase 3 requires exactly two chronological calibration folds")
    return (
        [row for row in rows if row.fold == fold_names[0]],
        [row for row in rows if row.fold == fold_names[1]],
    )


def _raw(model: dict[str, Any], features: dict[str, float]) -> float:
    if model.get("model_family") == BOOSTED_FAMILY:
        return boosted_raw_probability(model, features)[0]
    return logistic_raw_probability(model, features)[0]


def _decision(score, threshold: float, settings: dict[str, Any]) -> str:
    if score.uncertainty > float(settings["maximum_prediction_uncertainty"]):
        return "ABSTAIN"
    if score.probability >= threshold and score.lower_probability_bound < max(
        0.0, threshold - float(settings["lower_bound_slack"])
    ):
        return "ABSTAIN"
    return "ACCEPT" if score.probability >= threshold else "REJECT"


def _filtered(scores, rows, threshold: float, settings: dict[str, Any]):
    decisions = [_decision(score, threshold, settings) for score in scores]
    selected = [
        row.realized_r
        for row, decision in zip(rows, decisions)
        if decision == "ACCEPT"
    ]
    abstained = sum(decision == "ABSTAIN" for decision in decisions)
    return {
        "coverage": len(selected) / len(rows) if rows else 0.0,
        "abstained": abstained,
        "abstention_rate": abstained / len(rows) if rows else 0.0,
        **trade_metrics(selected),
    }


def _select_threshold(scores, rows, settings):
    candidates = []
    for threshold in (0.30, 0.35, 0.40, 0.43, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75):
        metrics = _filtered(scores, rows, threshold, settings)
        coverage = float(metrics["coverage"])
        if not (
            float(settings["minimum_filter_coverage"])
            <= coverage
            <= float(settings["maximum_filter_coverage"])
        ) or int(metrics["trades"]) < 5:
            continue
        rank = (
            float(metrics["expectancy_r"] or -1e9),
            float(metrics["profit_factor"] or 0.0),
            -float(metrics["maximum_drawdown_r"]),
            -float(metrics["abstention_rate"]),
            coverage,
        )
        candidates.append((rank, threshold, metrics))
    if not candidates:
        return 0.5, _filtered(scores, rows, 0.5, settings)
    _, threshold, metrics = max(candidates, key=lambda value: (value[0], -value[1]))
    return threshold, metrics


def _train_base(family: str, rows: Sequence[DatasetRow], settings: dict[str, Any]):
    features = [row.features for row in rows]
    labels = [row.label for row in rows]
    if family == LOGISTIC_FAMILY:
        model = train_logistic_model(
            features,
            labels,
            feature_names=FEATURE_NAMES,
            l2_penalty=float(settings["l2_penalty"]),
            iterations=int(settings["iterations"]),
            learning_rate=float(settings["learning_rate"]),
        )
        model["model_family"] = LOGISTIC_FAMILY
        return model
    if family == BOOSTED_FAMILY:
        return train_boosted_stumps(
            features,
            labels,
            feature_names=FEATURE_NAMES,
            estimators=int(settings["boosting_estimators"]),
            learning_rate=float(settings["boosting_learning_rate"]),
            maximum_thresholds=int(settings["boosting_maximum_thresholds"]),
            minimum_leaf_samples=int(settings["boosting_minimum_leaf_samples"]),
        )
    raise ValueError(f"Unsupported model family: {family}")


def _metadata(family, validation_policy, report, generated, calibration_samples, settings):
    return {
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "validation_profile_sha256": policy_profile_sha256(validation_policy),
        "dataset_sha256": report["dataset_sha256"],
        "generated_at_utc": generated.isoformat(),
        "model_family": family,
        "calibration_samples": calibration_samples,
        "label_definition": "target_before_stop_after_costs",
        "library_versions": {"python": "standard-library-only"},
        "uncertainty_policy": {
            "maximum_prediction_uncertainty": float(settings["maximum_prediction_uncertainty"]),
            "lower_bound_slack": float(settings["lower_bound_slack"]),
        },
    }


def _candidate(family, train, calibration_fit, selection, settings, metadata):
    base = _train_base(family, train, settings)
    calibrator = fit_isotonic_calibrator(
        [_raw(base, row.features) for row in calibration_fit],
        [row.label for row in calibration_fit],
    )
    provisional = finalize_model(
        base, calibrator=calibrator, threshold=0.5, metadata=metadata
    )
    scores = [score_model(provisional, row.features) for row in selection]
    threshold, filtered = _select_threshold(scores, selection, settings)
    probabilities = [score.probability for score in scores]
    classification = classification_metrics(probabilities, [row.label for row in selection])
    unfiltered = trade_metrics(row.realized_r for row in selection)
    ranking = (
        float(filtered["expectancy_r"] or -1e9)
        - float(unfiltered["expectancy_r"] or 0.0),
        float(filtered["profit_factor"] or 0.0),
        -float(filtered["maximum_drawdown_r"]),
        -float(classification["brier_score"]),
        -float(classification["expected_calibration_error"]),
    )
    return {
        "family": family,
        "base": base,
        "threshold": threshold,
        "filtered": filtered,
        "classification": classification,
        "unfiltered": unfiltered,
        "ranking": ranking,
    }


def train_and_register(*, data_root, validation_policy_path, learning_policy_path,
                       registry_path, models_dir, reports_dir):
    generated = datetime.now(UTC).replace(microsecond=0)
    validation_policy = evaluator.load_policy(validation_policy_path)
    learning_policy = _read_json(learning_policy_path)
    settings = learning_policy["training"]
    rows, qa = build_dataset(data_root, validation_policy)
    train, calibration, holdout, split_folds = chronological_split(rows, validation_policy)
    calibration_fit, selection = calibration_subsplit(
        calibration, split_folds["calibration"]
    )
    sample_counts = {
        "total": len(rows), "training": len(train), "calibration": len(calibration),
        "calibration_fit": len(calibration_fit), "model_selection": len(selection),
        "holdout": len(holdout),
    }
    # The model-selection fold is one fixed calendar month (May 2026 in the
    # twelve-fold profile) that yields only two fills under the production
    # strategy, so the policy floor for selection is deliberately 2 rather than
    # 5. Threshold tuning already refuses to engage below five trades and falls
    # back to the neutral 0.5 threshold, so a higher floor would only block
    # training without changing the model. Promotion still requires the full
    # holdout gate chain on the June/July folds.
    minimums = {
        "total": int(settings["minimum_total_samples"]),
        "training": int(settings["minimum_training_samples"]),
        "calibration": int(settings["minimum_calibration_samples"]),
        "calibration_fit": int(settings["minimum_calibration_fit_samples"]),
        "model_selection": int(settings["minimum_model_selection_samples"]),
        "holdout": int(settings["minimum_holdout_samples"]),
    }
    report = {
        "schema_version": 2,
        "generated_at_utc": generated.isoformat(),
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "validation_profile_sha256": policy_profile_sha256(validation_policy),
        "learning_profile": learning_policy["profile"],
        "dataset_sha256": _dataset_digest(rows, qa),
        "sample_counts": sample_counts,
        "split_folds": {
            **split_folds,
            "calibration_fit": split_folds["calibration"][:1],
            "model_selection": split_folds["calibration"][1:],
        },
        "data_qa": qa,
    }
    if any(sample_counts[name] < minimum for name, minimum in minimums.items()):
        report.update({
            "status": "INSUFFICIENT_DATA", "minimum_samples": minimums,
            "reason": "The chronological dataset does not yet meet Phase 3 minima.",
        })
        _write_json(reports_dir / "latest.json", report)
        return report

    families = tuple(settings.get("model_families", [LOGISTIC_FAMILY, BOOSTED_FAMILY]))
    candidates = []
    for family in families:
        candidates.append(_candidate(
            family, train, calibration_fit, selection, settings,
            _metadata(family, validation_policy, report, generated, len(calibration_fit), settings),
        ))
    chosen = max(candidates, key=lambda row: (row["ranking"], row["family"] == LOGISTIC_FAMILY))
    base = chosen["base"]
    calibrator = fit_isotonic_calibrator(
        [_raw(base, row.features) for row in calibration],
        [row.label for row in calibration],
    )
    model = finalize_model(
        base,
        calibrator=calibrator,
        threshold=float(chosen["threshold"]),
        metadata=_metadata(
            chosen["family"], validation_policy, report, generated, len(calibration), settings
        ),
    )
    holdout_scores = [score_model(model, row.features) for row in holdout]
    probabilities = [score.probability for score in holdout_scores]
    labels = [row.label for row in holdout]
    classification = classification_metrics(probabilities, labels)
    training_rate = statistics.mean(row.label for row in train)
    baseline = classification_metrics([training_rate] * len(labels), labels)
    unfiltered = trade_metrics(row.realized_r for row in holdout)
    filtered = _filtered(holdout_scores, holdout, float(model["threshold"]), settings)
    gates = {
        "brier_not_worse_than_constant": classification["brier_score"] <= baseline["brier_score"],
        "log_loss_not_worse_than_constant": classification["log_loss"] <= baseline["log_loss"],
        "maximum_expected_calibration_error": classification["expected_calibration_error"] <= float(settings["maximum_expected_calibration_error"]),
        "minimum_filtered_holdout_trades": int(filtered["trades"]) >= int(settings["minimum_filtered_holdout_trades"]),
        "minimum_filter_coverage": float(filtered["coverage"]) >= float(settings["minimum_filter_coverage"]),
        "maximum_filter_coverage": float(filtered["coverage"]) <= float(settings["maximum_filter_coverage"]),
        "maximum_abstention_rate": float(filtered["abstention_rate"]) <= float(settings["maximum_abstention_rate"]),
        "expectancy_improves": float(filtered["expectancy_r"] or -1e9) >= float(unfiltered["expectancy_r"] or 0.0) + float(settings["minimum_expectancy_improvement_r"]),
        "drawdown_not_worse": float(filtered["maximum_drawdown_r"]) <= float(unfiltered["maximum_drawdown_r"]),
        "profit_factor_not_worse": float(filtered["profit_factor"] or 0.0) >= float(unfiltered["profit_factor"] or 0.0),
    }
    passed = all(gates.values())
    report.update({
        "status": "PASS" if passed else "REJECT",
        "model_id": model["model_id"], "model_family": chosen["family"],
        "threshold": model["threshold"],
        "model_comparison": {
            row["family"]: {
                "selected": row is chosen,
                "threshold": row["threshold"],
                "selection_classification": row["classification"],
                "selection_unfiltered": row["unfiltered"],
                "selection_filtered": row["filtered"],
            } for row in candidates
        },
        "historical_validation": {
            "training_positive_rate": training_rate,
            "holdout_baseline_classification": baseline,
            "holdout_classification": classification,
            "holdout_unfiltered": unfiltered,
            "holdout_filtered": filtered,
            "gates": gates, "passed": passed,
        },
    })
    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / f"{model['model_id']}.json"
    _write_json(model_path, model)
    registry = _read_json(registry_path) if registry_path.exists() else _initial_registry()
    if registry.get("production_release_sha") != PRODUCTION_RELEASE_SHA:
        registry = _initial_registry()
    record = {
        "model_id": model["model_id"], "model_family": chosen["family"],
        "model_path": str(model_path.resolve().relative_to(HERE.resolve())),
        "trained_at_utc": generated.isoformat(), "threshold": model["threshold"],
        "historical_status": report["status"], "historical": report["historical_validation"],
        "promotion_score": _promotion_score(report), "dataset_sha256": report["dataset_sha256"],
        "validation_profile_sha256": report["validation_profile_sha256"],
        "feature_schema": FEATURE_SCHEMA_VERSION,
    }
    registry["challenger"] = record
    current = registry.get("champion")
    promoted = passed and (
        not isinstance(current, dict)
        or record["promotion_score"] > list(current.get("promotion_score", []))
    )
    if promoted:
        registry["champion"] = record
        registry["deployment"] = {
            "mode": "shadow", "status": "COLLECTING_LIVE_EVIDENCE",
            "canary_percent": int(learning_policy["deployment"]["canary_percent"]),
            "updated_at_utc": generated.isoformat(),
            "reason": "Historically validated Phase 3 champion reset to shadow.",
        }
    history = list(registry.get("history", []))
    history.append({
        "at_utc": generated.isoformat(),
        "event": "CHAMPION_PROMOTED" if promoted else "CHALLENGER_EVALUATED",
        "model_id": model["model_id"], "model_family": chosen["family"],
        "historical_status": report["status"],
    })
    registry["history"] = history[-100:]
    _write_json(registry_path, registry)
    report["registry"] = {
        "promoted_to_shadow_champion": promoted,
        "champion_model_id": (registry.get("champion") or {}).get("model_id"),
        "deployment": registry["deployment"],
    }
    _write_json(reports_dir / "latest.json", report)
    _write_json(reports_dir / f"training-{generated:%Y%m%dT%H%M%SZ}.json", report)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--validation-policy", type=Path, default=Path("autoresearch/walk_forward_policy.json"))
    parser.add_argument("--learning-policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--registry", type=Path, default=HERE / "registry.json")
    parser.add_argument("--models", type=Path, default=HERE / "models")
    parser.add_argument("--reports", type=Path, default=HERE / "reports")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = train_and_register(
        data_root=args.data_root, validation_policy_path=args.validation_policy,
        learning_policy_path=args.learning_policy, registry_path=args.registry,
        models_dir=args.models, reports_dir=args.reports,
    )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
