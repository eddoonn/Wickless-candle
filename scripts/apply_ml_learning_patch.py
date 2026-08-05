from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one exact match, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def regex_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: regex match count {count}: {pattern[:100]}")
    path.write_text(updated, encoding="utf-8")


def patch_paths() -> None:
    runtime = ROOT / "wickless_ml" / "runtime.py"
    replace_once(
        runtime,
        "    model = _read_json(HERE / model_path)\n",
        "    model = _read_json(registry_path.resolve().parent / model_path)\n",
    )
    training = ROOT / "wickless_ml" / "training.py"
    replace_once(
        training,
        "    relative_model = str(model_path.relative_to(HERE))\n",
        "    relative_model = str(model_path.resolve().relative_to(HERE.resolve()))\n",
    )


def patch_scanner() -> None:
    path = ROOT / "wickless_bot.py"
    replace_once(
        path,
        'DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"\nSTOP_TOO_TIGHT = "STOP_TOO_TIGHT"\n',
        'DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"\nML_FILTER_REJECTED = "ML_FILTER_REJECTED"\nSTOP_TOO_TIGHT = "STOP_TOO_TIGHT"\n',
    )
    replace_once(
        path,
        '''    quality_score: float = 0.0
    entry_displacement_atr: float = 0.0


@dataclass(frozen=True)
class CurrentQuote:
''',
        '''    quality_score: float = 0.0
    entry_displacement_atr: float = 0.0
    ml_model_id: str = ""
    ml_mode: str = "unavailable"
    ml_probability: float | None = None
    ml_threshold: float = 0.0
    ml_uncertainty: float = 1.0
    ml_ood_score: float = 0.0
    ml_decision: str = "UNAVAILABLE"
    ml_applied: bool = False


@dataclass(frozen=True)
class CurrentQuote:
''',
    )
    replace_once(
        path,
        '''                    {
                        "name": "Published (UTC)",
                        "value": f"`{signal.published_time_utc}`",
                        "inline": False,
                    },
''',
        '''                    {
                        "name": "ML meta-label",
                        "value": (
                            "`UNAVAILABLE • deterministic strategy used`"
                            if signal.ml_probability is None
                            else (
                                f"`p={100 * signal.ml_probability:.1f}% • "
                                f"threshold={100 * signal.ml_threshold:.1f}% • "
                                f"{signal.ml_decision} • "
                                f"uncertainty={100 * signal.ml_uncertainty:.0f}%`"
                            )
                        ),
                        "inline": False,
                    },
                    {
                        "name": "Published (UTC)",
                        "value": f"`{signal.published_time_utc}`",
                        "inline": False,
                    },
''',
    )
    replace_once(
        path,
        '''        "entry_displacement_atr": signal.entry_displacement_atr,
    }
    state.rejections.append(record)
''',
        '''        "entry_displacement_atr": signal.entry_displacement_atr,
        "ml_model_id": signal.ml_model_id,
        "ml_mode": signal.ml_mode,
        "ml_probability": signal.ml_probability,
        "ml_threshold": signal.ml_threshold,
        "ml_uncertainty": signal.ml_uncertainty,
        "ml_ood_score": signal.ml_ood_score,
        "ml_decision": signal.ml_decision,
        "ml_applied": signal.ml_applied,
    }
    state.rejections.append(record)
''',
    )
    regex_once(
        path,
        r"def _update_active_position\(\n.*?\n\ndef scan_markets\(",
        '''def _ml_outcome(
    status: str,
    signal: OriginLimitSignal,
) -> tuple[int, float] | None:
    if not signal.ml_model_id:
        return None
    cost = max(0.0, signal.cost_to_risk_ratio)
    if status == TARGET_ALREADY_REACHED:
        return 1, round(signal.reward_risk - cost, 6)
    if status in {STOP_ALREADY_REACHED, AMBIGUOUS_PRICE_PATH}:
        return 0, round(-1.0 - cost, 6)
    return None


def _record_ml_outcome(
    handled: dict[str, object],
    *,
    status: str,
    signal: OriginLimitSignal,
    at: datetime,
) -> None:
    outcome = _ml_outcome(status, signal)
    if outcome is None:
        return
    label, realized_r = outcome
    handled["ml_outcome"] = label
    handled["ml_realized_r"] = realized_r
    handled["ml_outcome_status"] = status
    handled["closed_time_utc"] = at.astimezone(UTC).isoformat()


def _update_active_position(
    state: ScannerState,
    *,
    instrument: str,
    bid_bars: Sequence[Bar],
    ask_bars: Sequence[Bar],
    at: datetime,
) -> None:
    record = state.positions.get(instrument)
    if not isinstance(record, dict) or not isinstance(record.get("signal"), dict):
        return
    try:
        signal = OriginLimitSignal(**record["signal"])
    except TypeError as error:
        raise ValueError(f"Invalid persisted position for {instrument}: {error}") from error
    status = _price_path_status(
        signal,
        bid_bars=bid_bars,
        ask_bars=ask_bars,
        require_ask_fill=False,
    )
    if status is None:
        record["last_evaluated_time_utc"] = at.astimezone(UTC).isoformat()
        return
    state.positions.pop(instrument, None)
    handled = state.handled.setdefault(signal.key, {})
    handled["position_status"] = status
    handled["closed_time_utc"] = at.astimezone(UTC).isoformat()
    _record_ml_outcome(handled, status=status, signal=signal, at=at)


def _update_filtered_outcomes(
    state: ScannerState,
    *,
    instrument: str,
    bid_bars: Sequence[Bar],
    ask_bars: Sequence[Bar],
    at: datetime,
) -> None:
    for record in state.handled.values():
        if (
            not isinstance(record, dict)
            or record.get("status") != "ML_FILTERED"
            or "ml_outcome" in record
            or not isinstance(record.get("signal"), dict)
        ):
            continue
        try:
            signal = OriginLimitSignal(**record["signal"])
        except TypeError as error:
            raise ValueError(f"Invalid filtered ML signal: {error}") from error
        if signal.instrument != instrument:
            continue
        status = _price_path_status(
            signal,
            bid_bars=bid_bars,
            ask_bars=ask_bars,
            require_ask_fill=False,
        )
        if status is None:
            record["last_evaluated_time_utc"] = at.astimezone(UTC).isoformat()
            continue
        record["filter_status"] = status
        _record_ml_outcome(record, status=status, signal=signal, at=at)


def scan_markets(''',
    )
    replace_once(
        path,
        '''        _update_active_position(
            state,
            instrument=instrument,
            bid_bars=bid_bars,
            ask_bars=ask_bars,
            at=as_of,
        )
        signals = find_fresh_origin_limit_signals(
''',
        '''        _update_active_position(
            state,
            instrument=instrument,
            bid_bars=bid_bars,
            ask_bars=ask_bars,
            at=as_of,
        )
        _update_filtered_outcomes(
            state,
            instrument=instrument,
            bid_bars=bid_bars,
            ask_bars=ask_bars,
            at=as_of,
        )
        signals = find_fresh_origin_limit_signals(
''',
    )
    regex_once(
        path,
        r"            validated = validate_signal_actionability\(\n.*?            publishable = replace\(validated, published_time_utc=as_of\.isoformat\(\)\)\n",
        '''            validated = validate_signal_actionability(
                signal,
                bid_bars=bid_bars,
                ask_bars=ask_bars,
                quote=quote,
                as_of=as_of,
                max_signal_age_seconds=max_signal_age_seconds,
                max_quote_age_seconds=max_quote_age_seconds,
                max_entry_deviation_r=max_entry_deviation_r,
            )
            if instrument in state.positions:
                validated = replace(
                    validated,
                    actionability_status=ACTIVE_POSITION_EXISTS,
                )
            if validated.actionability_status != ACTIONABLE:
                _record_rejection(
                    state,
                    validated,
                    status=validated.actionability_status,
                    at=as_of,
                )
                _save_state(state_path, state)
                print(
                    f"{signal.symbol}: rejected {signal.key} "
                    f"({validated.actionability_status})"
                )
                continue
            from wickless_ml.runtime import annotate_signal

            validated, prediction = annotate_signal(validated)
            if prediction.should_block:
                filtered = replace(
                    validated,
                    actionability_status=ML_FILTER_REJECTED,
                    published_time_utc=as_of.isoformat(),
                )
                _record_rejection(
                    state,
                    filtered,
                    status=ML_FILTER_REJECTED,
                    at=as_of,
                )
                state.handled[signal.key] = {
                    "status": "ML_FILTERED",
                    "timestamp": as_of.isoformat(),
                    "last_evaluated_time_utc": as_of.isoformat(),
                    "signal": asdict(filtered),
                }
                _save_state(state_path, state)
                print(
                    f"{signal.symbol}: ML filtered {signal.key} "
                    f"({prediction.decision})"
                )
                continue
            publishable = replace(validated, published_time_utc=as_of.isoformat())
''',
    )


def patch_health() -> None:
    path = ROOT / "scripts" / "system_health.py"
    replace_once(
        path,
        '''from production_session import PRODUCTION_RELEASE_SHA, SESSION_LABEL
''',
        '''from production_session import PRODUCTION_RELEASE_SHA, SESSION_LABEL
from wickless_ml.features import FEATURE_SCHEMA_VERSION
from wickless_ml.model import MODEL_FORMAT_VERSION
''',
    )
    replace_once(
        path,
        '''    nightly_workflow = (
        root / ".github/workflows/autoresearch-nightly.yml"
    ).read_text(encoding="utf-8")

    checks = [
''',
        '''    nightly_workflow = (
        root / ".github/workflows/autoresearch-nightly.yml"
    ).read_text(encoding="utf-8")
    ml_policy = json.loads(
        (root / "wickless_ml" / "policy.json").read_text(encoding="utf-8")
    )
    ml_registry = json.loads(
        (root / "wickless_ml" / "registry.json").read_text(encoding="utf-8")
    )
    ml_features = (root / "wickless_ml" / "features.py").read_text(encoding="utf-8")
    ml_model = (root / "wickless_ml" / "model.py").read_text(encoding="utf-8")
    ml_runtime = (root / "wickless_ml" / "runtime.py").read_text(encoding="utf-8")
    ml_monitor = (root / "wickless_ml" / "monitor.py").read_text(encoding="utf-8")
    bot_source = (root / "wickless_bot.py").read_text(encoding="utf-8")
    ml_learning_workflow = (
        root / ".github/workflows/ml-learning.yml"
    ).read_text(encoding="utf-8")
    ml_live_workflow = (
        root / ".github/workflows/ml-live-monitor.yml"
    ).read_text(encoding="utf-8")

    checks = [
''',
    )
    replace_once(
        path,
        '''        _check(
            "phase1_workflow_dataset",
''',
        '''        _check(
            "phase3_meta_label_learning",
            ml_policy.get("profile") == "wickless-meta-label-learning-v1"
            and int(ml_policy["training"]["minimum_total_samples"]) >= 60
            and FEATURE_SCHEMA_VERSION in ml_features
            and "fit_isotonic_calibrator" in ml_model
            and "chronological_split" in (
                root / "wickless_ml" / "training.py"
            ).read_text(encoding="utf-8")
            and "annotate_signal" in bot_source
            and "ML_FILTER_REJECTED" in bot_source
            and "deterministic strategy wins" in ml_runtime,
            f"features={FEATURE_SCHEMA_VERSION} model_format={MODEL_FORMAT_VERSION}",
        ),
        _check(
            "phase4_automated_production_learning",
            ml_registry.get("production_release_sha") == PRODUCTION_RELEASE_SHA
            and bool(ml_policy["deployment"]["automatic_canary_enabled"])
            and bool(ml_policy["deployment"]["automatic_active_enabled"])
            and int(ml_policy["deployment"]["minimum_shadow_outcomes"]) >= 50
            and int(ml_policy["deployment"]["minimum_canary_outcomes"]) >= 30
            and int(ml_policy["deployment"]["canary_percent"]) == 20
            and "ROLLED_BACK" in ml_monitor
            and "contents: write" in ml_learning_workflow
            and "contents: write" in ml_live_workflow
            and "wickless-autoresearch-data-v2-walk-forward" in ml_learning_workflow
            and "wickless-signal-state-" in ml_live_workflow
            and "contents: write" not in live_workflow,
            (
                f"deployment={ml_registry.get('deployment', {}).get('mode', 'shadow')} "
                f"champion={(ml_registry.get('champion') or {}).get('model_id', 'none')}"
            ),
        ),
        _check(
            "phase1_workflow_dataset",
''',
    )
    replace_once(
        path,
        '''        "phase2_optimizer_profile": (optimizer_settings or {}).get("profile"),
        "checks": [asdict(check) for check in checks],
''',
        '''        "phase2_optimizer_profile": (optimizer_settings or {}).get("profile"),
        "phase3_feature_schema": FEATURE_SCHEMA_VERSION,
        "phase4_deployment_mode": ml_registry.get("deployment", {}).get("mode"),
        "ml_champion_model_id": (ml_registry.get("champion") or {}).get("model_id"),
        "checks": [asdict(check) for check in checks],
''',
    )


def write_tests() -> None:
    path = ROOT / "tests" / "test_ml_learning.py"
    path.write_text(TESTS, encoding="utf-8")


TESTS = r'''from __future__ import annotations

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
        probabilities = [0.9, 0.85, 0.8, 0.2, 0.15, 0.1]
        outcomes = [1, 1, 1, 0, 0, 0]
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
'''


def main() -> None:
    patch_paths()
    patch_scanner()
    patch_health()
    write_tests()


if __name__ == "__main__":
    main()
