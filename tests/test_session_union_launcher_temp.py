from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUEST = ROOT / "ops" / "session-union-launch.request"


class TemporarySessionUnionLauncherTests(unittest.TestCase):
    def test_apply_reviewed_session_union_in_actions_worktree(self) -> None:
        if os.environ.get("GITHUB_ACTIONS") != "true" or not REQUEST.exists():
            self.skipTest("temporary launcher runs only in the requested GitHub Actions job")

        worktree = Path("/tmp/wickless-session-union")
        shutil.rmtree(worktree, ignore_errors=True)
        subprocess.run(
            ["git", "fetch", "origin", "session-union-main"],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--force",
                str(worktree),
                "origin/session-union-main",
            ],
            cwd=ROOT,
            check=True,
        )
        workflow = (
            worktree / ".github" / "workflows" / "apply-session-union-temp.yml"
        ).read_text(encoding="utf-8")
        marker = "        run: |\n"
        self.assertEqual(workflow.count(marker), 1)
        script = textwrap.dedent(workflow.split(marker, 1)[1])
        subprocess.run(["bash", "-c", script], cwd=worktree, check=True)


if __name__ == "__main__":
    unittest.main()
