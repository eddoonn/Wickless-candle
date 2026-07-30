from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_pine_scripts_enforce_fifteen_minutes_and_use_opening_side_rules(
        self,
    ) -> None:
        indicator = (ROOT / "Wickless_Candles_v1_0.pine").read_text(encoding="utf-8")
        strategy = (ROOT / "Wickless_Reversal_Strategy_v1_0.pine").read_text(
            encoding="utf-8"
        )
        for source in (indicator, strategy):
            self.assertIn("//@version=6", source)
            self.assertIn("timeframe.multiplier == 15", source)
            self.assertIn("close > open", source)
            self.assertIn("open - low <= tolerance", source)
            self.assertIn("close < open", source)
            self.assertIn("high - open <= tolerance", source)
        self.assertIn("alertcondition(", indicator)
        self.assertIn("alert(payload, alert.freq_once_per_bar_close)", strategy)
        self.assertIn('3,\n     "Retrace window (15m bars)"', strategy)
        self.assertIn('0.5,\n     "Origin-price margin (%)"', strategy)
        self.assertIn("age >= 1 and age <= retraceBars", strategy)
        self.assertIn("low <= upperBand and high >= lowerBand", strategy)
        self.assertIn("if bullishWickless", strategy)
        self.assertIn("if bearishWickless", strategy)
        self.assertNotIn(
            "buySignal = flat and enableLongs and bullishWickless",
            strategy,
        )
        self.assertNotIn(
            "sellSignal = flat and enableShorts and bearishWickless",
            strategy,
        )

    def test_workflow_scans_on_fifteen_minute_schedule_and_uses_secret(
        self,
    ) -> None:
        workflow = (ROOT / ".github/workflows/live-signals.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "*/15 * * * *"', workflow)
        self.assertIn("secrets.DISCORD_WEBHOOK_URL", workflow)
        self.assertIn("actions/cache/restore@", workflow)
        self.assertIn("actions/cache/save@", workflow)
        self.assertIn("--retrace-margin-percent 0.5", workflow)
        self.assertNotIn("discord.com/api/webhooks/", workflow)

    def test_no_discord_webhook_token_is_committed(self) -> None:
        pattern = re.compile(
            r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/"
            r"\d{17,20}/[A-Za-z0-9_-]{40,}"
        )
        ignored = {".git", "__pycache__", ".runtime-data", ".signal-state"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in ignored for part in path.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIsNone(pattern.search(content))

    def test_secret_files_and_runtime_state_are_ignored(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for expected in (".env", ".runtime-data/", ".signal-state/"):
            self.assertIn(expected, ignore)


if __name__ == "__main__":
    unittest.main()
