#!/usr/bin/env python3
"""Narrow branch-reference assertions in the one-time migration runner."""

from __future__ import annotations

from pathlib import Path


PATH = Path(__file__).with_name("single_branch_migration.py")
text = PATH.read_text(encoding="utf-8")

old_health = '''            "autoresearch/framework-v1" not in nightly_workflow
            and "autoresearch/nightly" not in nightly_workflow
            and 'git push origin HEAD:"$MAIN_BRANCH"' in nightly_workflow,
'''
new_health = '''            "FRAMEWORK_BRANCH:" not in nightly_workflow
            and "NIGHTLY_BRANCH:" not in nightly_workflow
            and "ref: autoresearch/framework-v1" not in nightly_workflow
            and 'git push origin "$NIGHTLY_BRANCH"' not in nightly_workflow
            and 'git push origin HEAD:"$MAIN_BRANCH"' in nightly_workflow,
'''
if old_health not in text:
    raise SystemExit("Expected broad system-health branch check was not found")
text = text.replace(old_health, new_health, 1)

old_nightly_test = '''        self.assertNotIn("autoresearch/framework-v1", workflow)
        self.assertNotIn("autoresearch/nightly", workflow)

        policy = json.loads'''
new_nightly_test = '''        self.assertIn("autoresearch/nightly-run.request", workflow)
        self.assertNotIn("FRAMEWORK_BRANCH:", workflow)
        self.assertNotIn("NIGHTLY_BRANCH:", workflow)
        self.assertNotIn("ref: autoresearch/framework-v1", workflow)
        self.assertNotIn('git push origin "$NIGHTLY_BRANCH"', workflow)

        policy = json.loads'''
if old_nightly_test not in text:
    raise SystemExit("Expected broad nightly workflow assertion was not found")
text = text.replace(old_nightly_test, new_nightly_test, 1)

old_guard_test = '''        self.assertNotIn("startsWith(github.head_ref", workflow)
        self.assertNotIn("autoresearch/framework-v1", workflow)
        self.assertNotIn("autoresearch/nightly", workflow)


'''
new_guard_test = '''        self.assertNotIn("startsWith(github.head_ref", workflow)
        self.assertNotIn("FRAMEWORK_BRANCH:", workflow)
        self.assertNotIn("NIGHTLY_BRANCH:", workflow)
        self.assertNotIn("ref: autoresearch/framework-v1", workflow)


'''
if old_guard_test not in text:
    raise SystemExit("Expected broad guard assertions were not found")
text = text.replace(old_guard_test, new_guard_test, 1)

old_cleanup = '''        "scripts/single_branch_migration.py",
        "reports/maintenance/latest/single-branch-migration.json",
'''
new_cleanup = '''        "scripts/single_branch_migration.py",
        "scripts/patch_single_branch_migration.py",
        "reports/maintenance/latest/single-branch-migration.json",
'''
if old_cleanup not in text:
    raise SystemExit("Expected temporary-file cleanup list was not found")
text = text.replace(old_cleanup, new_cleanup, 1)

PATH.write_text(text, encoding="utf-8")
