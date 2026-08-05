"""Train, calibrate, validate, and register Wickless meta-label challengers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from autoresearch import evaluator
from autoresearch.phase1_validation import policy_profile_sha256
from no_wick_research import NoWickConfig, run_no_wick_backtest
from production_session import PRODUCTION_RELEASE_SHA
from wickless_ml.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, features_from_setup, parse_time
from wickless_ml.model import (
    calibrated_probability,
    classification_metrics,
    finalize_model,
    fit_isotonic_calibrator,
    raw_probability,
    score_model,
    trade_metrics,
    train_logistic_model,
)


UTC = timezone.utc
HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DatasetRow:
    timestamp_utc: str
    fold: str
    pair: str
    order_id: str
    label: int
    realized_r: float
    features: dict[str, float]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def _fold_name(timestamp: datetime, folds: Sequence[Any]) -> str | None:
    for fold in folds:
        if fold.start <= timestamp < fold.end:
            return fold.name
    return None


def build_dataset(data_root: Path, policy: dict[str, Any]) -> tuple[list[DatasetRow], list[dict[str, Any]]]:
    folds = evaluator._folds(policy)
    if len(folds) < 12:
        raise ValueError("ML training requires the twelve-fold Phase 1 policy")
    data, qa = evaluator._load_fold_data(data_root, folds[0])
    start = folds[0].start
    end = folds[-1].end
    rows: list[DatasetRow] = []
    for instrument in evaluator.FOREX_MAJORS:
        bids, asks = data[instrument]
        result = run_no_wick_backtest(
            bids,
            ask_bars=asks,
            config=NoWickConfig(instrument=instrument),
            start=start,
            end=end,
        )
        fills = {fill.order_id: fill for fill in result.fills}
        for trade in result.trades:
            fill = fills.get(trade.order_id)
            if fill is None:
                continue
            timestamp = parse_time(fill.fill_time_utc)
            fold = _fold_name(timestamp, folds)
            if fold is None:
                continue
            rows.append(
                DatasetRow(
                    timestamp_utc=timestamp.isoformat(),
                    fold=fold,
                    pair=instrument.upper(),
                    order_id=trade.order_id,
                    label=int(trade.net_r_after_costs > 0),
                    realized_r=float(trade.net_r_after_costs),
                    features=features_from_setup(fill, instrument=instrument),
                )
            )
    rows.sort(key=lambda row: (row.timestamp_utc, row.pair, row.order_id))
    return rows, qa


def chronological_split(
    rows: Sequence[DatasetRow], policy: dict[str, Any]
) -> tuple[list[DatasetRow], list[DatasetRow], list[DatasetRow], dict[str, list[str]]]:
    fold_names = [str(row["name"]) for row in policy["folds"]]
    if len(fold_names) != 12:
        raise ValueError("Expected exactly twelve chronological folds")
    train_names = set(fold_names[:8])
    calibration_names = set(fold_names[8:10])
    holdout_names = set(fold_names[10:12])
    train = [row for row in rows if row.fold in train_names]
    calibration = [row for row in rows if row.fold in calibration_names]
    holdout = [row for row in rows if row.fold in holdout_names]
    return train, calibration, holdout, {
        "training": fold_names[:8],
        "calibration": fold_names[8:10],
        "holdout": fold_names[10:12],
    }


def _probabilities(model: dict[str, Any], rows: Sequence[DatasetRow]) -> list[float]:
    return [score_model(model, row.features).probability for row in rows]


def _baseline_metrics(labels: Sequence[int], probability: float) -> dict[str, float | int]:
    return classification_metrics([probability] * len(labels), labels)


def _threshold_candidates() -> tuple[float, ...]:
    return (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75)


def select_threshold(
    probabilities: Sequence[float],
    rows: Sequence[DatasetRow],
    *,
    minimum_coverage: float,
    maximum_coverage: float,
) -> tuple[float, dict[str, Any]]:
    if len(probabilities) != len(rows) or not rows:
        raise ValueError("Threshold selection requires aligned calibration rows")
    candidates: list[tuple[tuple[float, float, float, float], float, dict[str, Any]]] = []
    for threshold in _threshold_candidates():
        selected = [
            row.realized_r
            for probability, row in zip(probabilities, rows)
            if probability >= threshold
        ]
        coverage = len(selected) / len(rows)
        if not minimum_coverage <= coverage <= maximum_coverage or len(selected) < 5:
            continue
        metrics = trade_metrics(selected)
        expectancy = float(metrics["expectancy_r"] or -1e9)
        profit_factor = float(metrics["profit_factor"] or 0.0)
        ranking = (
            expectancy,
            profit_factor,
            -float(metrics["maximum_drawdown_r"]),
            coverage,
        )
        candidates.append((ranking, threshold, {"coverage": coverage, **metrics}))
    if not candidates:
        selected = [
            row.realized_r
            for probability, row in zip(probabilities, rows)
            if probability >= 0.5
        ]
        return 0.5, {"coverage": len(selected) / len(rows), **trade_metrics(selected)}
    _, threshold, metrics = max(candidates, key=lambda row: (row[0], -row[1]))
    return threshold, metrics


def _filtered_metrics(
    probabilities: Sequence[float], rows: Sequence[DatasetRow], threshold: float
) -> dict[str, Any]:
    selected = [
        row.realized_r
        for probability, row in zip(probabilities, rows)
        if probability >= threshold
    ]
    return {"coverage": len(selected) / len(rows) if rows else 0.0, **trade_metrics(selected)}


def _dataset_digest(rows: Sequence[DatasetRow], qa: Sequence[dict[str, Any]]) -> str:
    payload = {
        "rows": [
            {
                "timestamp_utc": row.timestamp_utc,
                "pair": row.pair,
                "order_id": row.order_id,
                "label": row.label,
                "realized_r": row.realized_r,
                "features": row.features,
            }
            for row in rows
        ],
        "qa": list(qa),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _promotion_score(report: dict[str, Any]) -> list[float]:
    historical = report["historical_validation"]
    filtered = historical["holdout_filtered"]
    classification = historical["holdout_classification"]
    unfiltered = historical["holdout_unfiltered"]
    return [
        float(filtered["expectancy_r"] or -1e9) - float(unfiltered["expectancy_r"] or 0.0),
        -float(classification["brier_score"]),
        -float(classification["expected_calibration_error"]),
        float(filtered["profit_factor"] or 0.0),
        -float(filtered["maximum_drawdown_r"]),
    ]


def _initial_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "deployment": {
            "mode": "shadow",
            "status": "NO_CHAMPION",
            "canary_percent": 20,
            "updated_at_utc": None,
            "reason": "No historically validated champion exists yet.",
        },
        "champion": None,
        "challenger": None,
        "history": [],
    }


def train_and_register(
    *,
    data_root: Path,
    validation_policy_path: Path,
    learning_policy_path: Path,
    registry_path: Path,
    models_dir: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    generated = datetime.now(UTC).replace(microsecond=0)
    validation_policy = evaluator.load_policy(validation_policy_path)
    learning_policy = _read_json(learning_policy_path)
    if learning_policy.get("schema_version") != 1:
        raise ValueError("Unsupported ML learning policy")
    settings = learning_policy["training"]
    rows, qa = build_dataset(data_root, validation_policy)
    train, calibration, holdout, split_folds = chronological_split(rows, validation_policy)
    minimums = {
        "total": int(settings["minimum_total_samples"]),
        "training": int(settings["minimum_training_samples"]),
        "calibration": int(settings["minimum_calibration_samples"]),
        "holdout": int(settings["minimum_holdout_samples"]),
    }
    sample_counts = {
        "total": len(rows),
        "training": len(train),
        "calibration": len(calibration),
        "holdout": len(holdout),
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": generated.isoformat(),
        "production_release_sha": PRODUCTION_RELEASE_SHA,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "validation_profile_sha256": policy_profile_sha256(validation_policy),
        "learning_profile": learning_policy["profile"],
        "dataset_sha256": _dataset_digest(rows, qa),
        "sample_counts": sample_counts,
        "split_folds": split_folds,
        "data_qa": qa,
    }
    if any(sample_counts[name] < minimum for name, minimum in minimums.items()):
        report.update(
            {
                "status": "INSUFFICIENT_DATA",
                "minimum_samples": minimums,
                "reason": "The chronological dataset does not yet meet training minima.",
            }
        )
        _write_json(reports_dir / "latest.json", report)
        _write_json(reports_dir / f"training-{generated:%Y%m%dT%H%M%SZ}.json", report)
        return report

    base = train_logistic_model(
        [row.features for row in train],
        [row.label for row in train],
        feature_names=FEATURE_NAMES,
        l2_penalty=float(settings["l2_penalty"]),
        iterations=int(settings["iterations"]),
        learning_rate=float(settings["learning_rate"]),
    )
    calibration_raw = [raw_probability(base, row.features)[0] for row in calibration]
    calibrator = fit_isotonic_calibrator(calibration_raw, [row.label for row in calibration])
    provisional = finalize_model(
        base,
        calibrator=calibrator,
        threshold=0.5,
        metadata={
            "production_release_sha": PRODUCTION_RELEASE_SHA,
            "feature_schema": FEATURE_SCHEMA_VERSION,
            "validation_profile_sha256": policy_profile_sha256(validation_policy),
            "dataset_sha256": report["dataset_sha256"],
            "generated_at_utc": generated.isoformat(),
        },
    )
    calibration_probabilities = _probabilities(provisional, calibration)
    threshold, calibration_filtered = select_threshold(
        calibration_probabilities,
        calibration,
        minimum_coverage=float(settings["minimum_filter_coverage"]),
        maximum_coverage=float(settings["maximum_filter_coverage"]),
    )
    model = finalize_model(
        base,
        calibrator=calibrator,
        threshold=threshold,
        metadata=provisional["metadata"],
    )
    holdout_probabilities = _probabilities(model, holdout)
    holdout_labels = [row.label for row in holdout]
    training_positive_rate = statistics.mean(row.label for row in train)
    holdout_classification = classification_metrics(holdout_probabilities, holdout_labels)
    baseline_classification = _baseline_metrics(holdout_labels, training_positive_rate)
    holdout_unfiltered = trade_metrics(row.realized_r for row in holdout)
    holdout_filtered = _filtered_metrics(holdout_probabilities, holdout, threshold)
    filtered_expectancy = float(holdout_filtered["expectancy_r"] or -1e9)
    unfiltered_expectancy = float(holdout_unfiltered["expectancy_r"] or 0.0)
    filtered_pf = float(holdout_filtered["profit_factor"] or 0.0)
    unfiltered_pf = float(holdout_unfiltered["profit_factor"] or 0.0)
    gates = {
        "minimum_total_samples": len(rows) >= minimums["total"],
        "minimum_holdout_samples": len(holdout) >= minimums["holdout"],
        "brier_not_worse_than_constant": holdout_classification["brier_score"]
        <= baseline_classification["brier_score"],
        "log_loss_not_worse_than_constant": holdout_classification["log_loss"]
        <= baseline_classification["log_loss"],
        "maximum_expected_calibration_error": holdout_classification[
            "expected_calibration_error"
        ]
        <= float(settings["maximum_expected_calibration_error"]),
        "minimum_filtered_holdout_trades": int(holdout_filtered["trades"])
        >= int(settings["minimum_filtered_holdout_trades"]),
        "minimum_filter_coverage": float(holdout_filtered["coverage"])
        >= float(settings["minimum_filter_coverage"]),
        "maximum_filter_coverage": float(holdout_filtered["coverage"])
        <= float(settings["maximum_filter_coverage"]),
        "expectancy_improves": filtered_expectancy
        >= unfiltered_expectancy + float(settings["minimum_expectancy_improvement_r"]),
        "drawdown_not_worse": float(holdout_filtered["maximum_drawdown_r"])
        <= float(holdout_unfiltered["maximum_drawdown_r"]),
        "profit_factor_not_worse": filtered_pf >= unfiltered_pf,
    }
    passed = all(gates.values())
    report.update(
        {
            "status": "PASS" if passed else "REJECT",
            "model_id": model["model_id"],
            "threshold": threshold,
            "historical_validation": {
                "training_positive_rate": training_positive_rate,
                "calibration_classification": classification_metrics(
                    calibration_probabilities, [row.label for row in calibration]
                ),
                "calibration_filtered": calibration_filtered,
                "holdout_baseline_classification": baseline_classification,
                "holdout_classification": holdout_classification,
                "holdout_unfiltered": holdout_unfiltered,
                "holdout_filtered": holdout_filtered,
                "gates": gates,
                "passed": passed,
            },
        }
    )
    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / f"{model['model_id']}.json"
    _write_json(model_path, model)

    registry = _read_json(registry_path) if registry_path.exists() else _initial_registry()
    if registry.get("production_release_sha") != PRODUCTION_RELEASE_SHA:
        registry = _initial_registry()
    relative_model = str(model_path.resolve().relative_to(HERE.resolve()))
    record = {
        "model_id": model["model_id"],
        "model_path": relative_model,
        "trained_at_utc": generated.isoformat(),
        "threshold": threshold,
        "historical_status": report["status"],
        "historical": report["historical_validation"],
        "promotion_score": _promotion_score(report),
        "dataset_sha256": report["dataset_sha256"],
        "validation_profile_sha256": report["validation_profile_sha256"],
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
            "mode": "shadow",
            "status": "COLLECTING_LIVE_EVIDENCE",
            "canary_percent": int(learning_policy["deployment"]["canary_percent"]),
            "updated_at_utc": generated.isoformat(),
            "reason": "Historically validated champion reset to shadow for live evidence.",
        }
    history = list(registry.get("history", []))
    history.append(
        {
            "at_utc": generated.isoformat(),
            "event": "CHAMPION_PROMOTED" if promoted else "CHALLENGER_EVALUATED",
            "model_id": model["model_id"],
            "historical_status": report["status"],
        }
    )
    registry["history"] = history[-100:]
    _write_json(registry_path, registry)
    report["registry"] = {
        "promoted_to_shadow_champion": promoted,
        "champion_model_id": (
            registry["champion"]["model_id"] if isinstance(registry.get("champion"), dict) else None
        ),
        "deployment": registry["deployment"],
    }
    _write_json(reports_dir / "latest.json", report)
    _write_json(reports_dir / f"training-{generated:%Y%m%dT%H%M%SZ}.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument(
        "--validation-policy",
        type=Path,
        default=Path("autoresearch/walk_forward_policy.json"),
    )
    parser.add_argument("--learning-policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--registry", type=Path, default=HERE / "registry.json")
    parser.add_argument("--models", type=Path, default=HERE / "models")
    parser.add_argument("--reports", type=Path, default=HERE / "reports")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = train_and_register(
        data_root=args.data_root,
        validation_policy_path=args.validation_policy,
        learning_policy_path=args.learning_policy,
        registry_path=args.registry,
        models_dir=args.models,
        reports_dir=args.reports,
    )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
