#!/usr/bin/env python3
"""One-use migration for gate-progress-first nightly diagnostic ranking."""

from __future__ import annotations

from pathlib import Path


NIGHTLY_OLD = '''def select_best_experiment(
    outputs: list[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the strongest experiment that changed realized trades."""

    trade_changed = [
        output for output in outputs if output.get("effect") == "trade-changed"
    ]
    if not trade_changed:
        return None
    return max(trade_changed, key=lambda output: objective_tuple(output, policy))
'''

NIGHTLY_NEW = '''def _best_test_rank(
    output: dict[str, Any], policy: dict[str, Any]
) -> tuple[Any, ...]:
    """Rank diagnostic tests by gate progress before the profit objective."""

    checks = output.get("acceptance_gates", {}).get("checks", {})
    passed_gates = sum(bool(value) for value in checks.values())
    fold_trade_floor = min(
        int(value["trades"]) for value in output["folds"].values()
    )
    total_trades = int(output["overall"]["trades"])
    return (
        passed_gates,
        fold_trade_floor,
        total_trades,
        objective_tuple(output, policy),
    )


def select_best_experiment(
    outputs: list[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the trade-changing test closest to full acceptance."""

    trade_changed = [
        output for output in outputs if output.get("effect") == "trade-changed"
    ]
    if not trade_changed:
        return None
    return max(
        trade_changed,
        key=lambda output: _best_test_rank(output, policy),
    )
'''

TEST_START = "    def test_best_experiment_uses_policy_order_and_only_trade_changes"
TEST_END = "    def test_discord_summary_reports_trade_funnel_and_no_effect_counts"
TEST_NEW = '''    def test_best_experiment_prioritizes_gate_and_trade_progress(self) -> None:
        policy = {
            "objective_order": [
                "worst_fold_net_r",
                "total_net_r",
                "overall_profit_factor",
                "negative_overall_drawdown_r",
                "total_trades",
            ]
        }

        def row(
            name: str,
            effect: str,
            *,
            passed_gates: int,
            june: int,
            july: int,
            total: int,
            worst: float,
            total_net: float,
        ) -> dict:
            return {
                "candidate": name,
                "effect": effect,
                "acceptance_gates": {
                    "checks": {
                        f"gate_{index}": index < passed_gates
                        for index in range(10)
                    }
                },
                "folds": {
                    "june_2026": {"trades": june},
                    "july_2026": {"trades": july},
                },
                "overall": {"trades": total},
                "objective": {
                    "worst_fold_net_r": worst,
                    "total_net_r": total_net,
                    "overall_profit_factor": 2.0,
                    "negative_overall_drawdown_r": -2.0,
                    "total_trades": total,
                },
            }

        no_effect = row(
            "identical",
            "no-effect",
            passed_gates=10,
            june=99,
            july=99,
            total=198,
            worst=99.0,
            total_net=198.0,
        )
        funnel_only = row(
            "funnel-only",
            "funnel-only",
            passed_gates=10,
            june=99,
            july=99,
            total=198,
            worst=100.0,
            total_net=200.0,
        )
        degenerate = row(
            "zero-trade",
            "trade-changed",
            passed_gates=5,
            june=0,
            july=0,
            total=0,
            worst=0.0,
            total_net=0.0,
        )
        lower_coverage = row(
            "lower-coverage",
            "trade-changed",
            passed_gates=8,
            june=3,
            july=15,
            total=18,
            worst=-2.0,
            total_net=10.0,
        )
        closer_to_monthly_gate = row(
            "closer-to-monthly-gate",
            "trade-changed",
            passed_gates=8,
            june=4,
            july=14,
            total=18,
            worst=-3.0,
            total_net=8.0,
        )
        selected = select_best_experiment(
            [
                no_effect,
                funnel_only,
                degenerate,
                lower_coverage,
                closer_to_monthly_gate,
            ],
            policy,
        )
        self.assertEqual(selected["candidate"], "closer-to-monthly-gate")
        self.assertIsNone(select_best_experiment([no_effect, funnel_only], policy))

'''


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    if old not in source:
        if new in source:
            return
        raise SystemExit(f"Expected migration block not found in {path}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    replace_once(
        Path("autoresearch/nightly_batch.py"),
        NIGHTLY_OLD,
        NIGHTLY_NEW,
    )
    tests = Path("tests/test_nightly_batch.py")
    source = tests.read_text(encoding="utf-8")
    if "test_best_experiment_prioritizes_gate_and_trade_progress" not in source:
        start = source.index(TEST_START)
        end = source.index(TEST_END, start)
        tests.write_text(source[:start] + TEST_NEW + source[end:], encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
