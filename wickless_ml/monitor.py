"""Evaluate live shadow outcomes and manage guarded deployment transitions."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from production_session import PRODUCTION_RELEASE_SHA
from wickless_ml.model import classification_metrics, trade_metrics


UTC = timezone.utc
HERE = Path(__file__).resolve().parent


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _resolved_rows(state: dict[str, Any], model_id: str) -> list[dict[str, Any]]:
    handled = state.get("handled", {})
    if not isinstance(handled, dict):
        return []
    rows: list[dict[str, Any]] = []
    for signal_id, record in handled.items():
        if not isinstance(record, dict) or record.get("ml_outcome") not in {0, 1}:
            continue
        signal = record.get("signal")
        if not isinstance(signal, dict) or signal.get("ml_model_id") != model_id:
            continue
        probability = signal.get("ml_probability")
        if probability is None:
            continue
        rows.append(
            {
                "signal_id": signal_id,
                "timestamp": str(record.get("closed_time_utc") or record.get("timestamp") or ""),
                "probability": float(probability),
                "threshold": float(signal.get("ml_threshold", 0.5)),
                "uncertainty": float(signal.get("ml_uncertainty", 1.0)),
                "ood_score": float(signal.get("ml_ood_score", 0.0)),
                "applied": bool(signal.get("ml_applied", False)),
                "decision": str(signal.get("ml_decision", "UNKNOWN")),
                "outcome": int(record["ml_outcome"]),
                "realized_r": float(record.get("ml_realized_r", 0.0)),
            }
        )
    rows.sort(key=lambda row: (row["timestamp"], row["signal_id"]))
    return rows


def _live_metrics(rows: Sequence[dict[str, Any]], maximum_ood: float) -> dict[str, Any]:
    if not rows:
        return {
            "outcomes": 0,
            "classification": None,
            "baseline_classification": None,
            "all_trades": trade_metrics([]),
            "accepted_trades": trade_metrics([]),
            "filter_coverage": 0.0,
            "ood_rate": 0.0,
            "uncertainty_mean": None,
        }
    probabilities = [float(row["probability"]) for row in rows]
    outcomes = [int(row["outcome"]) for row in rows]
    baseline_probability = sum(outcomes) / len(outcomes)
    accepted = [
        row for row in rows if float(row["probability"]) >= float(row["threshold"])
    ]
    return {
        "outcomes": len(rows),
        "classification": classification_metrics(probabilities, outcomes),
        "baseline_classification": classification_metrics(
            [baseline_probability] * len(rows), outcomes
        ),
        "all_trades": trade_metrics(row["realized_r"] for row in rows),
        "accepted_trades": trade_metrics(row["realized_r"] for row in accepted),
        "filter_coverage": len(accepted) / len(rows),
        "ood_rate": sum(float(row["ood_score"]) > maximum_ood for row in rows) / len(rows),
        "uncertainty_mean": sum(float(row["uncertainty"]) for row in rows) / len(rows),
    }


def _evidence_gates(metrics: dict[str, Any], settings: dict[str, Any]) -> dict[str, bool]:
    if not metrics["outcomes"] or metrics["classification"] is None:
        return {"has_outcomes": False}
    classification = metrics["classification"]
    baseline = metrics["baseline_classification"]
    accepted = metrics["accepted_trades"]
    all_trades = metrics["all_trades"]
    accepted_expectancy = float(accepted["expectancy_r"] or -1e9)
    all_expectancy = float(all_trades["expectancy_r"] or 0.0)
    return {
        "has_outcomes": True,
        "brier_not_worse_than_live_constant": float(classification["brier_score"])
        <= float(baseline["brier_score"]),
        "maximum_expected_calibration_error": float(
            classification["expected_calibration_error"]
        )
        <= float(settings["maximum_live_expected_calibration_error"]),
        "maximum_ood_rate": float(metrics["ood_rate"])
        <= float(settings["maximum_live_ood_rate"]),
        "minimum_filter_coverage": float(metrics["filter_coverage"])
        >= float(settings["minimum_live_filter_coverage"]),
        "maximum_filter_coverage": float(metrics["filter_coverage"])
        <= float(settings["maximum_live_filter_coverage"]),
        "accepted_trades_exist": int(accepted["trades"]) >= 5,
        "accepted_expectancy_improves": accepted_expectancy
        >= all_expectancy + float(settings["minimum_live_expectancy_improvement_r"]),
        "accepted_drawdown_not_worse": float(accepted["maximum_drawdown_r"])
        <= float(all_trades["maximum_drawdown_r"]),
    }


def monitor_and_transition(
    *,
    state_path: Path,
    registry_path: Path,
    policy_path: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    generated = datetime.now(UTC).replace(microsecond=0)
    registry = _read_json(registry_path)
    policy = _read_json(policy_path)
    if registry.get("production_release_sha") != PRODUCTION_RELEASE_SHA:
        raise ValueError("ML registry is stale for the production release")
    champion = registry.get("champion")
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": generated.isoformat(),
        "production_release_sha": PRODUCTION_RELEASE_SHA,
    }
    if not isinstance(champion, dict):
        report.update(
            {
                "status": "NO_CHAMPION",
                "deployment": registry.get("deployment"),
                "resolved_outcomes": 0,
            }
        )
        _write_json(reports_dir / "latest.json", report)
        _write_json(reports_dir / f"live-{generated:%Y%m%dT%H%M%SZ}.json", report)
        return report
    state = _read_json(state_path) if state_path.exists() else {"handled": {}}
    model_id = str(champion["model_id"])
    rows = _resolved_rows(state, model_id)
    training_settings = policy["training"]
    deployment_settings = policy["deployment"]
    all_metrics = _live_metrics(rows, float(training_settings["maximum_ood_score"]))
    applied_rows = [row for row in rows if row["applied"]]
    applied_metrics = _live_metrics(
        applied_rows, float(training_settings["maximum_ood_score"])
    )
    gates = _evidence_gates(all_metrics, deployment_settings)
    applied_gates = _evidence_gates(applied_metrics, deployment_settings)
    deployment = dict(registry.get("deployment", {}))
    previous_mode = str(deployment.get("mode", "shadow"))
    mode = previous_mode if previous_mode in {"shadow", "canary", "active"} else "shadow"
    reason = "Collecting live evidence."
    status = "COLLECTING_LIVE_EVIDENCE"

    rollback_window = int(deployment_settings["rollback_window"])
    recent_metrics = _live_metrics(
        rows[-rollback_window:], float(training_settings["maximum_ood_score"])
    )
    recent_classification = recent_metrics.get("classification")
    rollback = bool(recent_classification) and (
        float(recent_classification["expected_calibration_error"])
        > float(deployment_settings["rollback_maximum_expected_calibration_error"])
        or float(recent_metrics["ood_rate"])
        > float(deployment_settings["rollback_maximum_ood_rate"])
    )
    if mode in {"canary", "active"} and rollback:
        mode = "shadow"
        status = "ROLLED_BACK"
        reason = "Recent calibration or feature-support drift breached rollback limits."
    elif mode == "shadow":
        if (
            bool(deployment_settings["automatic_canary_enabled"])
            and len(rows) >= int(deployment_settings["minimum_shadow_outcomes"])
            and all(gates.values())
        ):
            mode = "canary"
            status = "CANARY"
            reason = "Live shadow evidence passed; deterministic canary enabled."
    elif mode == "canary":
        if (
            bool(deployment_settings["automatic_active_enabled"])
            and len(applied_rows) >= int(deployment_settings["minimum_canary_outcomes"])
            and all(applied_gates.values())
        ):
            mode = "active"
            status = "ACTIVE"
            reason = "Applied canary evidence passed; full ML filter enabled."
        else:
            status = "CANARY"
            reason = "Canary remains active while applied outcomes accumulate."
    else:
        status = "ACTIVE"
        reason = "Full ML filter remains within live monitoring limits."

    deployment.update(
        {
            "mode": mode,
            "status": status,
            "canary_percent": int(deployment_settings["canary_percent"]),
            "updated_at_utc": generated.isoformat(),
            "reason": reason,
            "resolved_outcomes": len(rows),
            "resolved_applied_outcomes": len(applied_rows),
        }
    )
    registry["deployment"] = deployment
    history = list(registry.get("history", []))
    if mode != previous_mode or status == "ROLLED_BACK":
        history.append(
            {
                "at_utc": generated.isoformat(),
                "event": "DEPLOYMENT_TRANSITION",
                "model_id": model_id,
                "from": previous_mode,
                "to": mode,
                "status": status,
                "reason": reason,
            }
        )
    registry["history"] = history[-100:]
    _write_json(registry_path, registry)
    report.update(
        {
            "status": status,
            "model_id": model_id,
            "previous_mode": previous_mode,
            "deployment": deployment,
            "all_live_evidence": all_metrics,
            "all_live_gates": gates,
            "applied_live_evidence": applied_metrics,
            "applied_live_gates": applied_gates,
            "recent_rollback_window": recent_metrics,
        }
    )
    _write_json(reports_dir / "latest.json", report)
    _write_json(reports_dir / f"live-{generated:%Y%m%dT%H%M%SZ}.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".signal-state/seen.json"))
    parser.add_argument("--registry", type=Path, default=HERE / "registry.json")
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--reports", type=Path, default=HERE / "live")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = monitor_and_transition(
        state_path=args.state,
        registry_path=args.registry,
        policy_path=args.policy,
        reports_dir=args.reports,
    )
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
