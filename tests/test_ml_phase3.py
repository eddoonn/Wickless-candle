from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from production_session import PRODUCTION_RELEASE_SHA
from wickless_ml.features import FEATURE_NAMES, FEATURE_SCHEMA_VERSION, features_from_setup
from wickless_ml.model import finalize_model, fit_isotonic_calibrator
from wickless_ml.model_v2 import FAMILY, score_model, train_boosted_stumps
from wickless_ml.runtime import score_signal


ROOT = Path(__file__).resolve().parents[1]


def setup_source(**overrides):
    values = {
        "instrument": "eurusd",
        "side": "BUY",
        "entry_model": "signal_close",
        "fill_time_utc": "2026-06-10T13:15:00+00:00",
        "body_ratio": 0.90,
        "wick_size_ticks": 0.0,
        "wickless_range_atr": 1.0,
        "close_location": 0.05,
        "quality_score": 96.0,
        "entry_displacement_atr": 0.0,
        "stop_distance_atr": 0.8,
        "cost_to_risk_ratio": 0.08,
        "spread_multiple": 8.0,
        "risk_pips": 8.0,
        "touch_bar_number": 0,
        "confirmation_bar_number": 0,
        "ema_distance_atr": 0.6,
        "ema_slope_atr": 0.3,
        "recent_volatility_atr": 1.4,
        "directional_persistence": 0.7,
        "volatility_expansion_ratio": 1.3,
        "correlated_signal_count": 2,
        "key": "phase3-signal",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def boosted_model(threshold: float = 0.5):
    rows = []
    labels = []
    for index in range(48):
        source = setup_source(
            body_ratio=0.72 + index * 0.005,
            recent_volatility_atr=0.6 + index * 0.025,
            directional_persistence=-0.8 + index * 0.035,
        )
        rows.append(features_from_setup(source))
        labels.append(int(index >= 24))
    base = train_boosted_stumps(
        rows,
        labels,
        feature_names=FEATURE_NAMES,
        estimators=24,
        learning_rate=0.12,
        maximum_thresholds=6,
        minimum_leaf_samples=4,
    )
    calibrator = fit_isotonic_calibrator(base.pop("training_raw_scores"), labels)
    return finalize_model(
        base,
        calibrator=calibrator,
        threshold=threshold,
        metadata={
            "production_release_sha": PRODUCTION_RELEASE_SHA,
            "feature_schema": FEATURE_SCHEMA_VERSION,
            "calibration_samples": len(labels),
        },
    )


class PhaseThreeMachineLearningTests(unittest.TestCase):
    def test_regime_features_are_decision_time_and_complete(self) -> None:
        features = features_from_setup(setup_source())
        self.assertEqual(tuple(features), FEATURE_NAMES)
        self.assertEqual(FEATURE_SCHEMA_VERSION, "wickless-meta-label-features-v2")
        self.assertEqual(features["volatility_high"], 1.0)
        self.assertEqual(features["trend_strong"], 1.0)
        self.assertEqual(features["spread_elevated"], 1.0)
        self.assertNotIn("exit_reason", features)
        self.assertNotIn("realized_r", features)

    def test_boosted_challenger_is_deterministic_and_bounded(self) -> None:
        first = boosted_model()
        second = boosted_model()
        self.assertEqual(first, second)
        self.assertEqual(first["model_family"], FAMILY)
        score = score_model(first, features_from_setup(setup_source()))
        self.assertGreaterEqual(score.probability, 0.0)
        self.assertLessEqual(score.probability, 1.0)
        self.assertLessEqual(score.lower_probability_bound, score.probability)

    def test_uncertain_v2_prediction_abstains_and_never_blocks(self) -> None:
        model = boosted_model(threshold=0.10)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            model_path = root / "models" / f"{model['model_id']}.json"
            model_path.write_text(json.dumps(model), encoding="utf-8")
            registry = {
                "schema_version": 1,
                "production_release_sha": PRODUCTION_RELEASE_SHA,
                "deployment": {"mode": "active", "status": "ACTIVE", "canary_percent": 20},
                "champion": {
                    "model_id": model["model_id"],
                    "model_path": f"models/{model['model_id']}.json",
                },
            }
            policy = json.loads((ROOT / "wickless_ml/policy.json").read_text())
            policy["training"]["maximum_prediction_uncertainty"] = 0.0
            (root / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
            (root / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
            prediction = score_signal(
                setup_source(),
                registry_path=root / "registry.json",
                policy_path=root / "policy.json",
            )
        self.assertEqual(prediction.decision, "ABSTAIN_UNCERTAIN")
        self.assertFalse(prediction.applied)
        self.assertFalse(prediction.should_block)

    def test_workflow_uses_phase3_trainer(self) -> None:
        workflow = (ROOT / ".github/workflows/ml-learning.yml").read_text()
        self.assertIn("python -m wickless_ml.phase3_training", workflow)
        self.assertIn("Train and validate Phase 3 challengers", workflow)


if __name__ == "__main__":
    unittest.main()
