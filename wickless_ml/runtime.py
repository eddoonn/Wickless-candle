"""Guarded champion scoring for live Wickless signals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from production_session import PRODUCTION_RELEASE_SHA
from wickless_ml.features import (
    SUPPORTED_FEATURE_SCHEMA_VERSIONS,
    features_from_setup,
    supported_instrument,
)
from wickless_ml.model_v2 import score_model


HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Prediction:
    model_id: str = ""
    model_family: str = ""
    mode: str = "unavailable"
    raw_probability: float | None = None
    probability: float | None = None
    lower_probability_bound: float | None = None
    threshold: float = 0.0
    uncertainty: float = 1.0
    ood_score: float = 0.0
    decision: str = "UNAVAILABLE"
    applied: bool = False
    should_block: bool = False
    reason: str = "No champion model is available."


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def load_champion(
    registry_path: Path = HERE / "registry.json",
    policy_path: Path = HERE / "policy.json",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    registry = _read_json(registry_path)
    policy = _read_json(policy_path)
    if registry.get("schema_version") != 1 or policy.get("schema_version") != 1:
        raise ValueError("Unsupported ML registry or policy schema")
    if registry.get("production_release_sha") != PRODUCTION_RELEASE_SHA:
        raise ValueError("ML registry is stale for the production release")
    champion = registry.get("champion")
    if not isinstance(champion, dict):
        raise ValueError("No ML champion is registered")
    model_id = champion.get("model_id")
    model_path = champion.get("model_path")
    if not isinstance(model_id, str) or not isinstance(model_path, str):
        raise ValueError("Champion registry record is incomplete")
    model = _read_json(registry_path.resolve().parent / model_path)
    if model.get("model_id") != model_id:
        raise ValueError("Champion model identity does not match the registry")
    metadata = model.get("metadata", {})
    if metadata.get("production_release_sha") != PRODUCTION_RELEASE_SHA:
        raise ValueError("Champion model is stale for the production release")
    if metadata.get("feature_schema") not in SUPPORTED_FEATURE_SCHEMA_VERSIONS:
        raise ValueError("Champion model uses an incompatible feature schema")
    return registry, policy, model


def _canary_selected(signal_key: str, model_id: str, percent: int) -> bool:
    digest = hashlib.sha256(f"{model_id}|{signal_key}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < percent


def score_signal(
    signal: Any,
    *,
    registry_path: Path = HERE / "registry.json",
    policy_path: Path = HERE / "policy.json",
) -> Prediction:
    instrument = str(getattr(signal, "instrument", ""))
    if not supported_instrument(instrument):
        return Prediction(reason=f"Instrument {instrument or 'unknown'} is outside the trained universe.")
    registry, policy, model = load_champion(registry_path, policy_path)
    features = features_from_setup(signal, instrument=instrument)
    scored = score_model(model, features)
    threshold = float(model["threshold"])
    training_policy = policy["training"]
    maximum_ood = float(training_policy["maximum_ood_score"])
    maximum_uncertainty = float(training_policy.get("maximum_prediction_uncertainty", 1.0))
    lower_bound_slack = float(training_policy.get("lower_bound_slack", 1.0))
    deployment = registry.get("deployment", {})
    requested_mode = str(deployment.get("mode", "shadow"))
    status = str(deployment.get("status", "UNKNOWN"))
    mode = requested_mode if requested_mode in {"shadow", "canary", "active"} else "shadow"
    if status in {"NO_CHAMPION", "DRIFT", "ROLLED_BACK", "MODEL_ERROR"}:
        mode = "shadow"

    uncertainty_enabled = (
        model.get("metadata", {}).get("feature_schema")
        == "wickless-meta-label-features-v2"
    )
    common = {
        "model_id": model["model_id"],
        "model_family": str(model.get("model_family", "logistic_regression")),
        "mode": mode,
        "raw_probability": scored.raw_probability,
        "probability": scored.probability,
        "lower_probability_bound": scored.lower_probability_bound,
        "threshold": threshold,
        "uncertainty": scored.uncertainty,
        "ood_score": scored.ood_score,
    }
    if scored.ood_score > maximum_ood:
        return Prediction(
            **common,
            decision="ABSTAIN_OOD",
            applied=False,
            should_block=False,
            reason="Feature vector is outside the approved model support; deterministic strategy wins.",
        )
    if uncertainty_enabled and scored.uncertainty > maximum_uncertainty:
        return Prediction(
            **common,
            decision="ABSTAIN_UNCERTAIN",
            applied=False,
            should_block=False,
            reason="Prediction uncertainty exceeds policy; deterministic strategy wins.",
        )
    if (
        uncertainty_enabled
        and scored.probability >= threshold
        and scored.lower_probability_bound < max(0.0, threshold - lower_bound_slack)
    ):
        return Prediction(
            **common,
            decision="ABSTAIN_LOWER_BOUND",
            applied=False,
            should_block=False,
            reason="The calibrated probability passes, but its lower confidence bound does not.",
        )

    accepted = scored.probability >= threshold
    canary_percent = int(deployment.get("canary_percent", 20))
    applied = mode == "active" or (
        mode == "canary"
        and _canary_selected(str(getattr(signal, "key", "")), model["model_id"], canary_percent)
    )
    if mode == "shadow":
        decision = "SHADOW_ACCEPT" if accepted else "SHADOW_REJECT"
    elif mode == "canary" and not applied:
        decision = "CANARY_BYPASS"
    elif mode == "canary":
        decision = "CANARY_ACCEPT" if accepted else "CANARY_REJECT"
    else:
        decision = "ACTIVE_ACCEPT" if accepted else "ACTIVE_REJECT"
    return Prediction(
        **common,
        decision=decision,
        applied=applied,
        should_block=applied and not accepted,
        reason=(
            "Model decision is observational only."
            if not applied
            else "Model decision is inside the approved deployment cohort."
        ),
    )


def safe_score_signal(signal: Any) -> Prediction:
    try:
        return score_signal(signal)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return Prediction(decision="UNAVAILABLE", reason=f"ML fallback: {error}")


def annotate_signal(signal: Any) -> tuple[Any, Prediction]:
    prediction = safe_score_signal(signal)
    annotated = replace(
        signal,
        ml_model_id=prediction.model_id,
        ml_mode=prediction.mode,
        ml_probability=prediction.probability,
        ml_threshold=prediction.threshold,
        ml_uncertainty=prediction.uncertainty,
        ml_ood_score=prediction.ood_score,
        ml_decision=prediction.decision,
        ml_applied=prediction.applied,
    )
    return annotated, prediction
