"""Phase 3 nonlinear challenger utilities with backward-compatible scoring."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from wickless_ml.model import score_model as score_legacy

FAMILY = "gradient_boosted_stumps"
FORMAT_VERSION = 2


@dataclass(frozen=True)
class ModelScore:
    raw_probability: float
    probability: float
    uncertainty: float
    lower_probability_bound: float
    ood_score: float


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, value))))


def _matrix(rows, names):
    return [[float(row[name]) for name in names] for row in rows]


def _thresholds(values: Sequence[float], maximum: int) -> list[float]:
    ordered = sorted(set(values))
    mids = [(left + right) / 2 for left, right in zip(ordered, ordered[1:])]
    if len(mids) <= maximum:
        return mids
    return [
        mids[round(index * (len(mids) - 1) / (maximum - 1))]
        for index in range(maximum)
    ]


def train_boosted_stumps(
    rows,
    labels,
    *,
    feature_names,
    estimators=48,
    learning_rate=0.12,
    maximum_thresholds=8,
    minimum_leaf_samples=4,
) -> dict[str, Any]:
    """Fit a deterministic gradient-boosted decision-stump challenger."""

    if len(rows) != len(labels) or not rows or set(labels) != {0, 1}:
        raise ValueError("Boosting requires aligned binary examples from both classes")
    names = tuple(feature_names)
    matrix = _matrix(rows, names)
    means = [statistics.mean(column) for column in zip(*matrix)]
    scales = []
    for index, mean in enumerate(means):
        deviation = math.sqrt(
            statistics.mean((row[index] - mean) ** 2 for row in matrix)
        )
        scales.append(deviation or 1.0)
    positives = sum(labels)
    negatives = len(labels) - positives
    weights = [
        len(labels) / (2 * positives) if label else len(labels) / (2 * negatives)
        for label in labels
    ]
    intercept = math.log((positives + 0.5) / (negatives + 0.5))
    logits = [intercept] * len(labels)
    candidates = [
        _thresholds(column, maximum_thresholds) for column in zip(*matrix)
    ]
    stumps = []
    for _ in range(estimators):
        residual = [label - _sigmoid(logit) for label, logit in zip(labels, logits)]
        best = None
        for feature_index, cuts in enumerate(candidates):
            for cut in cuts:
                left = [
                    index for index, row in enumerate(matrix) if row[feature_index] <= cut
                ]
                right = [
                    index for index, row in enumerate(matrix) if row[feature_index] > cut
                ]
                if len(left) < minimum_leaf_samples or len(right) < minimum_leaf_samples:
                    continue

                def weighted_mean(indexes):
                    total = sum(weights[index] for index in indexes)
                    return sum(
                        weights[index] * residual[index] for index in indexes
                    ) / total

                left_value = weighted_mean(left)
                right_value = weighted_mean(right)
                left_set = set(left)
                loss = sum(
                    weights[index]
                    * (
                        residual[index]
                        - (left_value if index in left_set else right_value)
                    )
                    ** 2
                    for index in range(len(labels))
                )
                candidate = (loss, feature_index, cut, left_value, right_value)
                if best is None or candidate[:3] < best[:3]:
                    best = candidate
        if best is None:
            break
        _, feature_index, cut, left_value, right_value = best
        stumps.append([feature_index, cut, left_value, right_value])
        for index, row in enumerate(matrix):
            logits[index] += learning_rate * (
                left_value if row[feature_index] <= cut else right_value
            )
    standardized = [
        [(value - means[index]) / scales[index] for index, value in enumerate(row)]
        for row in matrix
    ]
    ood_scores = [
        math.sqrt(sum(value * value for value in row) / len(row))
        for row in standardized
    ]
    return {
        "format_version": FORMAT_VERSION,
        "model_family": FAMILY,
        "feature_names": list(names),
        "means": means,
        "scales": scales,
        "intercept": intercept,
        "learning_rate": learning_rate,
        "stumps": stumps,
        "training_samples": len(labels),
        "training_positive_rate": positives / len(labels),
        "training_ood_p95": sorted(ood_scores)[round(0.95 * (len(ood_scores) - 1))],
        "training_raw_scores": [_sigmoid(logit) for logit in logits],
    }


def raw_probability(model, features):
    names = model["feature_names"]
    raw = [float(features[name]) for name in names]
    standardized = [
        (value - float(mean)) / float(scale)
        for value, mean, scale in zip(raw, model["means"], model["scales"])
    ]
    logit = float(model["intercept"])
    rate = float(model["learning_rate"])
    for feature_index, cut, left_value, right_value in model["stumps"]:
        logit += rate * (
            float(left_value)
            if raw[int(feature_index)] <= float(cut)
            else float(right_value)
        )
    ood = math.sqrt(
        sum(value * value for value in standardized) / len(standardized)
    )
    return _sigmoid(logit), ood


def score_model(model, features):
    """Score either a legacy logistic model or the Phase 3 nonlinear model."""

    if model.get("model_family") != FAMILY:
        legacy = score_legacy(model, features)
        return ModelScore(
            raw_probability=legacy.raw_probability,
            probability=legacy.probability,
            uncertainty=legacy.uncertainty,
            lower_probability_bound=max(
                0.0, legacy.probability - legacy.uncertainty
            ),
            ood_score=legacy.ood_score,
        )
    from wickless_ml.model import calibrated_probability

    raw, ood = raw_probability(model, features)
    probability = calibrated_probability(raw, model.get("calibrator", []))
    samples = max(1, int(model.get("metadata", {}).get("calibration_samples", 1)))
    support = max(1.0, float(model.get("training_ood_p95", 1.0)))
    uncertainty = min(
        0.5,
        1.64
        * math.sqrt(max(1e-12, probability * (1.0 - probability)) / (samples + 2))
        + 0.12 * min(1.0, ood / (2.0 * support)),
    )
    return ModelScore(
        raw_probability=raw,
        probability=probability,
        uncertainty=uncertainty,
        lower_probability_bound=max(0.0, probability - uncertainty),
        ood_score=ood,
    )
