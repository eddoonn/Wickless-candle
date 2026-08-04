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
            self.assertIn("close < open", source)
        self.assertIn("open - low <= tolerance", indicator)
        self.assertIn("high - open <= tolerance", indicator)
        self.assertIn("open - low <= maximumWickTicks * tick", strategy)
        self.assertIn("high - open <= maximumWickTicks * tick", strategy)
        self.assertIn("alertcondition(", indicator)
        self.assertIn("emaLength = input.int(50", strategy)
        self.assertIn("emaSlopeLookback = input.int(5", strategy)
        self.assertIn('input.session("0500-1330"', strategy)
        self.assertIn('"America/New_York"', strategy)
        self.assertIn("stopBufferTicks = input.int(1", strategy)
        self.assertIn('atrLength = input.int(14', strategy)
        self.assertIn('minimumStopAtr = input.float(0.40', strategy)
        self.assertIn('maximumStopAtr = input.float(1.50', strategy)
        self.assertIn('minimumBodyRatio = input.float(0.80', strategy)
        self.assertIn('minimumRangeAtr = input.float(0.50', strategy)
        self.assertIn('maximumRangeAtr = input.float(2.00', strategy)
        self.assertIn("riskValid = risk >= minimumRisk and risk <= maximumRisk", strategy)
        self.assertIn("stopPrice = low - stopBuffer", strategy)
        self.assertIn("stopPrice = high + stopBuffer", strategy)
        self.assertIn("process_orders_on_close = true", strategy)
        self.assertIn("alert_message = payload", strategy)
        self.assertIn("pyramiding = 0", strategy)
        self.assertIn("strategy.position_size == 0", strategy)
        self.assertNotIn("retraceMarginPercent", strategy)
        self.assertNotIn("Retrace window", strategy)

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
        self.assertIn("validated signal-close entry", workflow)
        self.assertIn("--max-signal-age-seconds 900", workflow)
        self.assertIn("--max-quote-age-seconds 120", workflow)
        self.assertIn("--max-entry-deviation-r 0.25", workflow)
        self.assertIn("--research-lookback-seconds 2700", workflow)
        self.assertNotIn("--retrace-margin-percent", workflow)
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
