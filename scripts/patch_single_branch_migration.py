#!/usr/bin/env python3
"""Narrow and finalize the one-time single-branch migration runner."""

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

old_scan = '''    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(ROOT)
        if relative.parts[:2] == ("autoresearch", "runs"):
            continue
        if path.suffix.lower() not in {".py", ".md", ".json", ".yml", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for old in ("autoresearch/framework-v1", "autoresearch/nightly"):
            if old in text:
                hits.append(f"{relative}: {old}")
    if hits:
        raise RuntimeError("Old branch references remain: " + "; ".join(hits))
'''
new_scan = '''    nightly_workflow = (
        ROOT / ".github/workflows/autoresearch-nightly.yml"
    ).read_text(encoding="utf-8")
    forbidden_mechanics = (
        "FRAMEWORK_BRANCH:",
        "NIGHTLY_BRANCH:",
        "ref: autoresearch/framework-v1",
        'git push origin "$NIGHTLY_BRANCH"',
    )
    hits = [item for item in forbidden_mechanics if item in nightly_workflow]
    policy = json.loads(
        (ROOT / "autoresearch/policy.json").read_text(encoding="utf-8")
    )
    if policy.get("repository_branch") != "main":
        hits.append("policy repository_branch is not main")
    if hits:
        raise RuntimeError("Old branch mechanics remain: " + "; ".join(hits))
'''
if old_scan not in text:
    raise SystemExit("Expected naive repository text scan was not found")
text = text.replace(old_scan, new_scan, 1)

old_staging = '''        "tests",
        "ops",
        "reports/maintenance/latest",
    )
'''
new_staging = '''        "tests",
        "ops",
    )
'''
if old_staging not in text:
    raise SystemExit("Expected obsolete diagnostic staging path was not found")
text = text.replace(old_staging, new_staging, 1)

old_workflow_call = '''    import_nightly_state()
    rewrite_workflows()
    rewrite_code_and_docs()
'''
new_workflow_call = '''    import_nightly_state()
    # Workflow YAML is installed through the GitHub connector, which has workflow permission.
    rewrite_code_and_docs()
'''
if old_workflow_call not in text:
    raise SystemExit("Expected workflow rewrite call was not found")
text = text.replace(old_workflow_call, new_workflow_call, 1)

old_workflow_cleanup = '''        ".github/workflows/single-branch-migration.yml",
        "ops/single-branch-migration.request",
'''
new_workflow_cleanup = '''        "ops/single-branch-migration.request",
'''
if old_workflow_cleanup not in text:
    raise SystemExit("Expected migration workflow cleanup entry was not found")
text = text.replace(old_workflow_cleanup, new_workflow_cleanup, 1)

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
