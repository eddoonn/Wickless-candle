"""Deterministic standard-library meta-label model utilities."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


MODEL_FORMAT_VERSION = 1
_EPSILON = 1e-12


@dataclass(frozen=True)
class ModelScore:
    raw_probability: float
    probability: float
    uncertainty: float
    ood_score: float


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(max(value, -60.0))
    return exponent / (1.0 + exponent)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("Cannot calculate percentile of an empty sequence")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, probability))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _standardizer(
    rows: Sequence[dict[str, float]], feature_names: Sequence[str]
) -> tuple[list[float], list[float], list[list[float]]]:
    if not rows:
        raise ValueError("Training rows cannot be empty")
    matrix = [[float(row[name]) for name in feature_names] for row in rows]
    means = [statistics.mean(column) for column in zip(*matrix)]
    scales: list[float] = []
    for index, mean in enumerate(means):
        variance = statistics.mean((row[index] - mean) ** 2 for row in matrix)
        deviation = math.sqrt(max(0.0, variance))
        scales.append(deviation if deviation > 1e-9 else 1.0)
    standardized = [
        [(value - means[index]) / scales[index] for index, value in enumerate(row)]
        for row in matrix
    ]
    return means, scales, standardized


def train_logistic_model(
    rows: Sequence[dict[str, float]],
    labels: Sequence[int],
    *,
    feature_names: Sequence[str],
    l2_penalty: float,
    iterations: int,
    learning_rate: float,
) -> dict[str, Any]:
    """Fit a balanced L2 logistic model with deterministic batch descent."""

    if len(rows) != len(labels) or not rows:
        raise ValueError("Training rows and labels must be non-empty and aligned")
    if any(label not in {0, 1} for label in labels):
        raise ValueError("Labels must be binary")
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        raise ValueError("Training requires both winning and losing examples")
    if iterations < 1 or learning_rate <= 0 or l2_penalty < 0:
        raise ValueError("Invalid logistic-training settings")

    names = tuple(feature_names)
    means, scales, matrix = _standardizer(rows, names)
    coefficients = [0.0] * len(names)
    intercept = math.log((positives + 0.5) / (negatives + 0.5))
    positive_weight = len(labels) / (2.0 * positives)
    negative_weight = len(labels) / (2.0 * negatives)
    sample_weights = [positive_weight if label else negative_weight for label in labels]
    total_weight = sum(sample_weights)

    for iteration in range(iterations):
        gradient = [0.0] * len(names)
        intercept_gradient = 0.0
        for vector, label, sample_weight in zip(matrix, labels, sample_weights):
            linear = intercept + sum(
                coefficient * value
                for coefficient, value in zip(coefficients, vector)
            )
            error = (_sigmoid(linear) - label) * sample_weight
            intercept_gradient += error
            for index, value in enumerate(vector):
                gradient[index] += error * value
        step = learning_rate / math.sqrt(1.0 + iteration / 100.0)
        intercept -= step * intercept_gradient / total_weight
        for index in range(len(coefficients)):
            penalized = gradient[index] / total_weight + l2_penalty * coefficients[index]
            coefficients[index] -= step * penalized

    raw_scores = []
    ood_scores = []
    for vector in matrix:
        raw_scores.append(
            _sigmoid(
                intercept
                + sum(
                    coefficient * value
                    for coefficient, value in zip(coefficients, vector)
                )
            )
        )
        ood_scores.append(math.sqrt(sum(value * value for value in vector) / len(vector)))
    return {
        "format_version": MODEL_FORMAT_VERSION,
        "feature_names": list(names),
        "means": means,
        "scales": scales,
        "coefficients": coefficients,
        "intercept": intercept,
        "training_samples": len(labels),
        "training_positive_rate": positives / len(labels),
        "training_ood_p95": max(1.0, _percentile(ood_scores, 0.95)),
        "training_raw_scores": raw_scores,
    }


def fit_isotonic_calibrator(
    probabilities: Sequence[float], labels: Sequence[int]
) -> list[dict[str, float]]:
    """Fit an increasing calibration map with pair-adjacent violators."""

    if len(probabilities) != len(labels) or not probabilities:
        raise ValueError("Calibration probabilities and labels must align")
    grouped: list[dict[str, float]] = []
    for probability, label in sorted(zip(probabilities, labels), key=lambda row: row[0]):
        value = min(1.0, max(0.0, float(probability)))
        if grouped and abs(grouped[-1]["upper"] - value) <= _EPSILON:
            grouped[-1]["weight"] += 1.0
            grouped[-1]["positive"] += float(label)
            continue
        grouped.append(
            {
                "lower": value,
                "upper": value,
                "weight": 1.0,
                "positive": float(label),
            }
        )
    blocks: list[dict[str, float]] = []
    for group in grouped:
        blocks.append(group)
        while len(blocks) >= 2:
            left = blocks[-2]
            right = blocks[-1]
            left_value = left["positive"] / left["weight"]
            right_value = right["positive"] / right["weight"]
            if left_value <= right_value + _EPSILON:
                break
            blocks[-2:] = [
                {
                    "lower": left["lower"],
                    "upper": right["upper"],
                    "weight": left["weight"] + right["weight"],
                    "positive": left["positive"] + right["positive"],
                }
            ]
    return [
        {
            "upper": block["upper"],
            "value": (block["positive"] + 1.0) / (block["weight"] + 2.0),
        }
        for block in blocks
    ]


def calibrated_probability(
    probability: float, calibrator: Sequence[dict[str, float]]
) -> float:
    value = min(1.0, max(0.0, float(probability)))
    if not calibrator:
        return value
    for block in calibrator:
        if value <= float(block["upper"]) + _EPSILON:
            return min(1.0, max(0.0, float(block["value"])))
    return min(1.0, max(0.0, float(calibrator[-1]["value"])))


def raw_probability(model: dict[str, Any], features: dict[str, float]) -> tuple[float, float]:
    if model.get("format_version") != MODEL_FORMAT_VERSION:
        raise ValueError("Unsupported model format")
    names = model["feature_names"]
    means = model["means"]
    scales = model["scales"]
    coefficients = model["coefficients"]
    if not (len(names) == len(means) == len(scales) == len(coefficients)):
        raise ValueError("Model vector dimensions are inconsistent")
    vector: list[float] = []
    for name, mean, scale in zip(names, means, scales):
        if name not in features:
            raise ValueError(f"Missing model feature: {name}")
        value = float(features[name])
        if not math.isfinite(value):
            raise ValueError(f"Non-finite model feature: {name}")
        vector.append((value - float(mean)) / float(scale))
    linear = float(model["intercept"]) + sum(
        float(coefficient) * value
        for coefficient, value in zip(coefficients, vector)
    )
    ood = math.sqrt(sum(value * value for value in vector) / max(1, len(vector)))
    return _sigmoid(linear), ood


def score_model(model: dict[str, Any], features: dict[str, float]) -> ModelScore:
    raw, ood = raw_probability(model, features)
    probability = calibrated_probability(raw, model.get("calibrator", []))
    support = max(1.0, float(model.get("training_ood_p95", 1.0)))
    sample_count = max(1, int(model.get("training_samples", 1)))
    distribution_uncertainty = min(1.0, ood / (2.0 * support))
    sample_uncertainty = 1.0 / math.sqrt(1.0 + sample_count / 20.0)
    uncertainty = min(1.0, 0.75 * distribution_uncertainty + 0.25 * sample_uncertainty)
    return ModelScore(
        raw_probability=raw,
        probability=probability,
        uncertainty=uncertainty,
        ood_score=ood,
    )


def finalize_model(
    base_model: dict[str, Any],
    *,
    calibrator: Sequence[dict[str, float]],
    threshold: float,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in base_model.items()
        if key != "training_raw_scores"
    }
    payload.update(
        {
            "calibrator": list(calibrator),
            "threshold": float(threshold),
            "metadata": metadata,
        }
    )
    model_id = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]
    return {"model_id": model_id, **payload}


def classification_metrics(
    probabilities: Sequence[float], labels: Sequence[int], *, bins: int = 10
) -> dict[str, float | int]:
    if len(probabilities) != len(labels) or not probabilities:
        raise ValueError("Classification metrics require aligned non-empty samples")
    clipped = [min(1.0 - 1e-9, max(1e-9, float(value))) for value in probabilities]
    brier = statistics.mean((value - label) ** 2 for value, label in zip(clipped, labels))
    log_loss = -statistics.mean(
        label * math.log(value) + (1 - label) * math.log(1.0 - value)
        for value, label in zip(clipped, labels)
    )
    ece = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        members = [
            (probability, label)
            for probability, label in zip(clipped, labels)
            if lower <= probability < upper or (index == bins - 1 and probability == 1.0)
        ]
        if not members:
            continue
        average_probability = statistics.mean(row[0] for row in members)
        observed = statistics.mean(row[1] for row in members)
        ece += len(members) / len(labels) * abs(average_probability - observed)
    return {
        "samples": len(labels),
        "positive_rate": sum(labels) / len(labels),
        "brier_score": brier,
        "log_loss": log_loss,
        "expected_calibration_error": ece,
    }


def trade_metrics(realized_r: Iterable[float]) -> dict[str, float | int | None]:
    values = [float(value) for value in realized_r]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "wins": len(winners),
        "losses": len(losers),
        "net_r": sum(values),
        "expectancy_r": statistics.mean(values) if values else None,
        "profit_factor": (
            sum(winners) / -sum(losers)
            if losers
            else (None if not winners else 1e12)
        ),
        "maximum_drawdown_r": drawdown,
    }
