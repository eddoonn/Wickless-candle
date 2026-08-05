from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from production_session import PRODUCTION_RELEASE_SHA
from wickless_bot import OriginLimitSignal, _ml_outcome
from wickless_ml.features import FEATURE_NAMES, features_from_setup
from wickless_ml.model import (
    calibrated_probability,
    finalize_model,
    fit_isotonic_calibrator,
    train_logistic_model,
)
from wickless_ml.monitor import monitor_and_transition
from wickless_ml.runtime import score_signal
from wickless_ml.training import DatasetRow, chronological_split


ROOT = Path(__file__).resolve().parents[1]


def setup_source(**overrides):
    values = {
        "instrument": "eurusd",
        "side": "BUY",
        "entry_model": "signal_close",
        "fill_time_utc": "2026-06-10T08:15:00+00:00",
        "body_ratio": 0.9,
        "wick_size_ticks": 0.0,
        "wickless_range_atr": 1.0,
        "close_location": 0.05,
        "quality_score": 96.0,
        "entry_displacement_atr": 0.0,
        "stop_distance_atr": 0.8,
        "cost_to_risk_ratio": 0.05,
        "spread_multiple": 8.0,
        "risk_pips": 8.0,
        "touch_bar_number": 0,
        "confirmation_bar_number": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def signal() -> OriginLimitSignal:
    return OriginLimitSignal(
        key="abcdef1234567890",
        instrument="eurusd",
        symbol="EURUSD",
        timeframe="15m",
        pattern="BULLISH_WICKLESS",
        missing_wick="LOWER",
        side="BUY",
        signal_bar_open_time_utc="2026-06-10T08:00:00+00:00",
        signal_time_utc="2026-06-10T08:15:00+00:00",
        fill_bar_open_time_utc="2026-06-10T08:00:00+00:00",
        fill_time_utc="2026-06-10T08:15:00+00:00",
        fill_time_london="2026-06-10T09:15:00+01:00",
        entry_reference=1.1,
        stop=1.0992,
        target=1.1016,
        risk_points=0.0008,
        reward_risk=2.0,
        ema_length=50,
        ema_slope_lookback=5,
        pivot_left=3,
        pivot_right=3,
        session_label="fixed union",
        risk_pips=8.0,
        atr_15m=0.001,
        stop_distance_atr=0.8,
        spread_multiple=8.0,
        cost_to_risk_ratio=0.05,
        entry_model="signal_close",
        body_ratio=0.9,
        wick_size_ticks=0.0,
        wickless_range_atr=1.0,
        close_location=0.05,
        quality_score=96.0,
        entry_displacement_atr=0.0,
    )


def synthetic_model(threshold: float = 0.99):
    rows = []
    labels = []
    for index in range(40):
        source = setup_source(body_ratio=0.72 + index * 0.006)
        rows.append(features_from_setup(source))
        labels.append(int(index >= 20))
    base = train_logistic_model(
        rows,
        labels,
        feature_names=FEATURE_NAMES,
        l2_penalty=0.05,
        iterations=300,
        learning_rate=0.08,
    )
    raw = base.pop("training_raw_scores")
    calibrator = fit_isotonic_calibrator(raw, labels)
    return finalize_model(
        base,
        calibrator=calibrator,
        threshold=threshold,
        metadata={
            "production_release_sha": PRODUCTION_RELEASE_SHA,
            "feature_schema": "wickless-meta-label-features-v1",
        },
    )


class MachineLearningTests(unittest.TestCase):
    def test_feature_schema_contains_only_decision_time_fields(self) -> None:
        source = setup_source(exit_reason="TARGET", net_r_after_costs=2.0)
        features = features_from_setup(source)
        self.assertEqual(tuple(features), FEATURE_NAMES)
        self.assertNotIn("exit_reason", features)
        self.assertNotIn("net_r_after_costs", features)

    def test_logistic_and_calibration_are_deterministic(self) -> None:
        first = synthetic_model()
        second = synthetic_model()
        self.assertEqual(first, second)
        values = [calibrated_probability(value, first["calibrator"]) for value in (0.1, 0.5, 0.9)]
        self.assertEqual(values, sorted(values))

    def test_runtime_shadow_never_blocks_a_signal(self) -> None:
        model = synthetic_model()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "models" / f"{model['model_id']}.json").write_text(json.dumps(model))
            registry = {
                "schema_version": 1,
                "production_release_sha": PRODUCTION_RELEASE_SHA,
                "deployment": {"mode": "shadow", "status": "COLLECTING_LIVE_EVIDENCE", "canary_percent": 20},
                "champion": {"model_id": model["model_id"], "model_path": f"models/{model['model_id']}.json"},
            }
            policy = json.loads((ROOT / "wickless_ml/policy.json").read_text())
            (root / "registry.json").write_text(json.dumps(registry))
            (root / "policy.json").write_text(json.dumps(policy))
            prediction = score_signal(
                signal(), registry_path=root / "registry.json", policy_path=root / "policy.json"
            )
        self.assertEqual(prediction.mode, "shadow")
        self.assertFalse(prediction.applied)
        self.assertFalse(prediction.should_block)

    def test_runtime_active_rejection_can_block_only_inside_policy(self) -> None:
        model = synthetic_model(threshold=0.9999)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "models" / f"{model['model_id']}.json").write_text(json.dumps(model))
            registry = {
                "schema_version": 1,
                "production_release_sha": PRODUCTION_RELEASE_SHA,
                "deployment": {"mode": "active", "status": "ACTIVE", "canary_percent": 20},
                "champion": {"model_id": model["model_id"], "model_path": f"models/{model['model_id']}.json"},
            }
            policy = json.loads((ROOT / "wickless_ml/policy.json").read_text())
            (root / "registry.json").write_text(json.dumps(registry))
            (root / "policy.json").write_text(json.dumps(policy))
            prediction = score_signal(
                signal(), registry_path=root / "registry.json", policy_path=root / "policy.json"
            )
        self.assertTrue(prediction.applied)
        self.assertTrue(prediction.should_block)

    def test_outcome_mapping_is_cost_aware(self) -> None:
        annotated = signal()
        annotated = annotated.__class__(**{**annotated.__dict__, "ml_model_id": "model"})
        self.assertEqual(_ml_outcome("TARGET_ALREADY_REACHED", annotated), (1, 1.95))
        self.assertEqual(_ml_outcome("STOP_ALREADY_REACHED", annotated), (0, -1.05))

    def test_chronological_split_keeps_june_and_july_untouched(self) -> None:
        policy = json.loads((ROOT / "autoresearch/walk_forward_policy.json").read_text())
        rows = [
            DatasetRow("2026-01-01T00:00:00+00:00", fold["name"], "EURUSD", str(index), index % 2, 1.0, {})
            for index, fold in enumerate(policy["folds"])
        ]
        train, calibration, holdout, names = chronological_split(rows, policy)
        self.assertEqual(len(train), 8)
        self.assertEqual(len(calibration), 2)
        self.assertEqual([row.fold for row in holdout], ["june_2026", "july_2026"])
        self.assertEqual(names["holdout"], ["june_2026", "july_2026"])

    def test_live_monitor_promotes_shadow_to_canary_and_rolls_back(self) -> None:
        model_id = "model-1"
        handled = {}
        probabilities = [0.99, 0.98, 0.97, 0.96, 0.95, 0.03, 0.02, 0.01]
        outcomes = [1, 1, 1, 1, 1, 0, 0, 0]
        for index, (probability, outcome) in enumerate(zip(probabilities, outcomes)):
            handled[str(index)] = {
                "status": "SENT",
                "timestamp": f"2026-08-0{index + 1}T12:00:00+00:00",
                "closed_time_utc": f"2026-08-0{index + 1}T13:00:00+00:00",
                "ml_outcome": outcome,
                "ml_realized_r": 2.0 if outcome else -1.0,
                "signal": {
                    "ml_model_id": model_id,
                    "ml_probability": probability,
                    "ml_threshold": 0.5,
                    "ml_uncertainty": 0.1,
                    "ml_ood_score": 0.5,
                    "ml_applied": False,
                    "ml_decision": "SHADOW_ACCEPT" if probability >= 0.5 else "SHADOW_REJECT",
                },
            }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            registry = root / "registry.json"
            policy_path = root / "policy.json"
            state.write_text(json.dumps({"handled": handled}))
            registry.write_text(json.dumps({
                "schema_version": 1,
                "production_release_sha": PRODUCTION_RELEASE_SHA,
                "deployment": {"mode": "shadow", "status": "COLLECTING_LIVE_EVIDENCE", "canary_percent": 20},
                "champion": {"model_id": model_id},
                "history": [],
            }))
            policy = json.loads((ROOT / "wickless_ml/policy.json").read_text())
            policy["deployment"]["minimum_shadow_outcomes"] = 6
            policy_path.write_text(json.dumps(policy))
            report = monitor_and_transition(
                state_path=state,
                registry_path=registry,
                policy_path=policy_path,
                reports_dir=root / "reports",
            )
            self.assertEqual(report["deployment"]["mode"], "canary")
            bad = json.loads(state.read_text())
            for row in bad["handled"].values():
                row["signal"]["ml_probability"] = 0.95
                row["ml_outcome"] = 0
                row["ml_realized_r"] = -1.0
            state.write_text(json.dumps(bad))
            current = json.loads(registry.read_text())
            current["deployment"]["mode"] = "active"
            current["deployment"]["status"] = "ACTIVE"
            registry.write_text(json.dumps(current))
            rollback = monitor_and_transition(
                state_path=state,
                registry_path=registry,
                policy_path=policy_path,
                reports_dir=root / "reports",
            )
        self.assertEqual(rollback["deployment"]["mode"], "shadow")
        self.assertEqual(rollback["status"], "ROLLED_BACK")

    def test_workflows_and_scanner_preserve_safety_boundaries(self) -> None:
        live = (ROOT / ".github/workflows/live-signals.yml").read_text()
        learning = (ROOT / ".github/workflows/ml-learning.yml").read_text()
        monitor = (ROOT / ".github/workflows/ml-live-monitor.yml").read_text()
        scanner = (ROOT / "wickless_bot.py").read_text()
        self.assertIn("contents: read", live)
        self.assertNotIn("contents: write", live)
        self.assertIn("contents: write", learning)
        self.assertIn("contents: write", monitor)
        self.assertIn("annotate_signal", scanner)
        self.assertIn("ML_FILTER_REJECTED", scanner)
        self.assertIn("_update_filtered_outcomes", scanner)


if __name__ == "__main__":
    unittest.main()
