#!/usr/bin/env python3
"""Consolidate Wickless onto main, preserve audit state, and delete other refs."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD_BRANCHES = (
    "agent/autoresearch-polish",
    "agent/best-test-ranking",
    "agent/london-new-york-production",
    "agent/production-backtest-fix",
    "agent/system-polish-main",
    "autoresearch/framework-v1-pre-session-union-20260805",
    "autoresearch/framework-v1",
    "autoresearch/nightly",
    "backup-nightly-20260805",
    "session-comparison-results",
    "session-union-direct",
    "session-union-main",
)


def run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=capture,
    )


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.strip() + "\n", encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    content = target.read_text(encoding="utf-8")
    if old not in content:
        raise RuntimeError(f"Expected text not found in {path}: {old[:100]!r}")
    target.write_text(content.replace(old, new, 1), encoding="utf-8")


def import_nightly_state() -> None:
    run(
        "git",
        "fetch",
        "origin",
        "refs/heads/autoresearch/nightly:refs/remotes/origin/autoresearch/nightly",
    )
    paths = (
        "autoresearch/results.jsonl",
        "autoresearch/attempts.log",
        "autoresearch/incumbent.json",
        "autoresearch/runs",
        "autoresearch/playbook.md",
        "autoresearch/coach_state.json",
    )
    for path in paths:
        exists = run(
            "git",
            "cat-file",
            "-e",
            f"origin/autoresearch/nightly:{path}",
            check=False,
        ).returncode == 0
        if exists:
            run("git", "checkout", "origin/autoresearch/nightly", "--", path)


def rewrite_workflows() -> None:
    write(
        ".github/workflows/autoresearch-nightly.yml",
        r'''
name: Nightly Wickless autoresearch

on:
  push:
    branches:
      - main
    paths:
      - autoresearch/baseline-refresh.request
      - autoresearch/bootstrap-benchmark.request
      - autoresearch/nightly-run.request
      - autoresearch/maintenance.request
  schedule:
    - cron: "0 23 * * *"
      timezone: "Europe/London"
  workflow_dispatch:
    inputs:
      batch_size:
        description: "Number of diversified controlled experiments (1-20)"
        required: false
        default: "12"
      refresh_baseline:
        description: "Re-evaluate production defaults as the release-current reference"
        required: false
        type: boolean
        default: false
      bootstrap_benchmark:
        description: "Search the fixed-session space and promote the strongest valid result"
        required: false
        type: boolean
        default: false
      bootstrap_limit:
        description: "Maximum bootstrap candidates; 0 evaluates the complete search space"
        required: false
        default: "0"

permissions:
  contents: write

concurrency:
  group: wickless-autoresearch-nightly
  cancel-in-progress: false

jobs:
  research:
    runs-on: ubuntu-latest
    timeout-minutes: 360
    env:
      MAIN_BRANCH: main
      DATA_ROOT: .runtime-data/autoresearch-datasets
      BI5_CACHE: .runtime-data/duka-bi5
      BATCH_SIZE: ${{ github.event.inputs.batch_size || '12' }}
      REFRESH_INPUT: ${{ github.event.inputs.refresh_baseline || 'false' }}
      BOOTSTRAP_INPUT: ${{ github.event.inputs.bootstrap_benchmark || 'false' }}
      BOOTSTRAP_LIMIT: ${{ github.event.inputs.bootstrap_limit || '0' }}
    steps:
      - name: Check out main
        uses: actions/checkout@v4
        with:
          ref: main
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Capture immutable starting point
        shell: bash
        run: |
          git config user.name "wickless-autoresearch[bot]"
          git config user.email "wickless-autoresearch[bot]@users.noreply.github.com"
          echo "BASE_SHA=$(git rev-parse HEAD)" >> "$GITHUB_ENV"

      - name: Resolve requested mode
        shell: bash
        run: |
          mode=nightly
          if [[ "$GITHUB_EVENT_NAME" == "push" ]]; then
            changed="$(git diff --name-only "${{ github.event.before }}" "${{ github.sha }}")"
            if grep -qx 'autoresearch/bootstrap-benchmark.request' <<<"$changed"; then
              mode=bootstrap
            elif grep -qx 'autoresearch/baseline-refresh.request' <<<"$changed"; then
              mode=refresh
            elif grep -qx 'autoresearch/maintenance.request' <<<"$changed"; then
              mode=validate
            fi
          elif [[ "$BOOTSTRAP_INPUT" == "true" ]]; then
            mode=bootstrap
          elif [[ "$REFRESH_INPUT" == "true" ]]; then
            mode=refresh
          fi
          echo "RUN_MODE=$mode" >> "$GITHUB_ENV"
          echo "Resolved autoresearch mode: $mode"

      - name: Check Discord configuration
        id: discord-config
        continue-on-error: true
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        shell: bash
        run: test -n "$DISCORD_WEBHOOK_URL"

      - name: Restore fixed BID ASK research data
        if: env.RUN_MODE != 'validate'
        id: dataset-cache
        uses: actions/cache/restore@v4
        with:
          path: |
            .runtime-data/autoresearch-datasets
            .runtime-data/duka-bi5
          key: wickless-autoresearch-data-v1-complete
          restore-keys: |
            wickless-autoresearch-data-v1-partial-

      - name: Build missing June and July BID ASK datasets
        if: env.RUN_MODE != 'validate'
        shell: bash
        run: |
          pairs=(EURUSD GBPUSD USDJPY USDCHF USDCAD AUDUSD NZDUSD)
          june="$DATA_ROOT/dukascopy_m1_bidask_2026-06"
          july="$DATA_ROOT/dukascopy_m1_bidask_2026-07"
          retry_download() {
            local attempt
            for attempt in 1 2 3; do
              "$@" && return 0
              sleep $((attempt * 5))
            done
            return 1
          }
          if [[ ! -s "$june/manifest.json" ]]; then
            retry_download python scripts/download_m1_bid_ask.py "${pairs[@]}" \
              --start 2026-05-27 --end 2026-06-30 \
              --output "$june" --cache "$BI5_CACHE"
          fi
          if [[ ! -s "$july/manifest.json" ]]; then
            retry_download python scripts/download_m1_bid_ask.py "${pairs[@]}" \
              --start 2026-06-29 --end 2026-07-31 \
              --output "$july" --cache "$BI5_CACHE"
          fi
          test "$(find "$june" -name '*.csv.gz' | wc -l)" -eq 7
          test "$(find "$july" -name '*.csv.gz' | wc -l)" -eq 7

      - name: Save complete research data
        if: env.RUN_MODE != 'validate' && success() && steps.dataset-cache.outputs.cache-hit != 'true'
        uses: actions/cache/save@v4
        with:
          path: |
            .runtime-data/autoresearch-datasets
            .runtime-data/duka-bi5
          key: wickless-autoresearch-data-v1-complete

      - name: Save partial download for automatic retry
        if: env.RUN_MODE != 'validate' && failure() && steps.dataset-cache.outcome == 'success' && steps.dataset-cache.outputs.cache-hit != 'true'
        uses: actions/cache/save@v4
        with:
          path: |
            .runtime-data/autoresearch-datasets
            .runtime-data/duka-bi5
          key: wickless-autoresearch-data-v1-partial-${{ github.run_id }}

      - name: Ensure production reference benchmark
        if: env.RUN_MODE == 'nightly'
        shell: bash
        run: |
          python -m autoresearch.reference_benchmark \
            --data-root "$DATA_ROOT" \
            --policy autoresearch/policy.json \
            --ledger autoresearch/results.jsonl \
            --incumbent autoresearch/incumbent.json \
            --runs autoresearch/runs \
            --attempts autoresearch/attempts.log \
            | tee .runtime-reference.json

      - name: Refresh production reference benchmark
        if: env.RUN_MODE == 'refresh'
        shell: bash
        run: |
          python -m autoresearch.reference_benchmark \
            --data-root "$DATA_ROOT" \
            --policy autoresearch/policy.json \
            --ledger autoresearch/results.jsonl \
            --incumbent autoresearch/incumbent.json \
            --runs autoresearch/runs \
            --attempts autoresearch/attempts.log \
            --force \
            | tee .runtime-reference.json

      - name: Bootstrap the strongest valid benchmark
        if: env.RUN_MODE == 'bootstrap'
        shell: bash
        run: |
          rm -f /tmp/bootstrap-incumbent.json
          python -m autoresearch.bootstrap_benchmark \
            --data-root "$DATA_ROOT" \
            --policy autoresearch/policy.json \
            --ledger autoresearch/results.jsonl \
            --attempts autoresearch/attempts.log \
            --incumbent /tmp/bootstrap-incumbent.json \
            --runs autoresearch/runs \
            --max-candidates "$BOOTSTRAP_LIMIT"
          if [[ -s /tmp/bootstrap-incumbent.json ]]; then
            cp /tmp/bootstrap-incumbent.json autoresearch/incumbent.json
          fi

      - name: Run worker and coach experiment loop
        if: env.RUN_MODE == 'nightly'
        run: >-
          python -m autoresearch.nightly_batch
          --data-root "$DATA_ROOT"
          --batch-size "$BATCH_SIZE"
          --coach-interval 20
          --no-discord

      - name: Restore neutral candidate surface
        shell: bash
        run: |
          cat > autoresearch/candidate.py <<'PY'
          """The only strategy file an autoresearch agent may edit."""

          CANDIDATE = {
              "name": "production-baseline",
              "description": (
                  "Wickless production defaults using the fixed London and New York session union."
              ),
              "parameters": {},
          }
          PY

      - name: Validate code, health, and changed-file scope
        shell: bash
        run: |
          python -m compileall -q \
            wickless_bot.py no_wick_research.py live_data.py live_scan.py \
            run_daemon.py production_session.py time_display.py \
            autoresearch scripts tests
          python -m unittest discover -s tests -v
          mkdir -p .runtime-output
          python scripts/system_health.py --output .runtime-output/system-health.json
          git diff --name-only "$BASE_SHA" > .runtime-output/changed-files.txt
          python - <<'PY'
          from pathlib import Path

          allowed_exact = {
              "autoresearch/candidate.py",
              "autoresearch/incumbent.json",
              "autoresearch/results.jsonl",
              "autoresearch/attempts.log",
              "autoresearch/playbook.md",
              "autoresearch/coach_state.json",
          }
          changed = [
              line.strip()
              for line in Path(".runtime-output/changed-files.txt").read_text().splitlines()
              if line.strip()
          ]
          forbidden = [
              path for path in changed
              if path not in allowed_exact
              and not (path.startswith("autoresearch/runs/") and path.endswith(".json"))
          ]
          if forbidden:
              raise SystemExit("Protected files changed during research: " + ", ".join(forbidden))
          print(f"Scope OK: {len(changed)} allowed changed file(s)")
          PY

      - name: Upload durable autoresearch audit artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: autoresearch-${{ env.RUN_MODE }}-${{ github.run_id }}
          path: |
            autoresearch/runs
            autoresearch/results.jsonl
            autoresearch/attempts.log
            autoresearch/incumbent.json
            .runtime-reference.json
            .runtime-output/system-health.json
            .runtime-output/changed-files.txt
          if-no-files-found: warn
          retention-days: 30

      - name: Publish audit history to main
        id: publish
        shell: bash
        run: |
          git add -A -- \
            autoresearch/candidate.py \
            autoresearch/incumbent.json \
            autoresearch/results.jsonl \
            autoresearch/attempts.log \
            autoresearch/playbook.md \
            autoresearch/coach_state.json \
            autoresearch/runs
          if git diff --cached --quiet; then
            echo "No audit changes to publish."
            exit 0
          fi
          git commit -m "autoresearch: publish $RUN_MODE audit state"
          for attempt in 1 2 3; do
            git pull --rebase origin "$MAIN_BRANCH" && \
              git push origin HEAD:"$MAIN_BRANCH" && exit 0
            git rebase --abort 2>/dev/null || true
            sleep $((attempt * 5))
          done
          exit 1

      - name: Notify Discord of nightly benchmark and best test
        if: env.RUN_MODE == 'nightly' && steps.publish.outcome == 'success'
        continue-on-error: true
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: python -m autoresearch.notifications nightly --path autoresearch/runs

      - name: Notify Discord of bootstrap result
        if: env.RUN_MODE == 'bootstrap' && steps.publish.outcome == 'success'
        continue-on-error: true
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: python -m autoresearch.notifications bootstrap --path autoresearch/runs/bootstrap-summary.json

      - name: Notify Discord of refreshed reference
        if: env.RUN_MODE == 'refresh' && steps.publish.outcome == 'success'
        continue-on-error: true
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: python -m autoresearch.notifications refresh --path .runtime-reference.json

      - name: Write automation summary
        if: always()
        shell: bash
        run: |
          {
            echo "# Wickless autoresearch automation"
            echo
            echo "- Mode: \`$RUN_MODE\`"
            echo "- Repository branch: \`$MAIN_BRANCH\`"
            echo "- Discord configured: \`${{ steps.discord-config.outcome == 'success' }}\`"
          } >> "$GITHUB_STEP_SUMMARY"

      - name: Alert Discord if the batch fails
        if: failure()
        continue-on-error: true
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: python -m autoresearch.notifications failure --run-url "$RUN_URL"
        ''',
    )

    write(
        ".github/workflows/autoresearch.yml",
        r'''
name: Wickless autoresearch validation

on:
  push:
    branches:
      - main
    paths:
      - "autoresearch/**/*.py"
      - "autoresearch/policy.json"
      - "autoresearch/program.md"
      - "autoresearch/README.md"
      - ".github/workflows/autoresearch.yml"
      - ".github/workflows/autoresearch-nightly.yml"
      - "tests/**"
  pull_request:
    branches:
      - main
    paths:
      - "autoresearch/**/*.py"
      - "autoresearch/policy.json"
      - "autoresearch/program.md"
      - "autoresearch/README.md"
      - ".github/workflows/autoresearch.yml"
      - ".github/workflows/autoresearch-nightly.yml"
      - "tests/**"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: autoresearch-validation-${{ github.ref }}
  cancel-in-progress: true

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Compile and run repository tests
        run: |
          python -m compileall -q \
            wickless_bot.py no_wick_research.py live_data.py live_scan.py \
            run_daemon.py production_session.py time_display.py \
            autoresearch scripts tests
          python -m unittest discover -s tests -v
          python scripts/system_health.py --output /tmp/system-health.json
        ''',
    )

    replace_once(
        ".github/workflows/system-health.yml",
        "      - main\n      - autoresearch/framework-v1\n",
        "      - main\n",
    )


def rewrite_code_and_docs() -> None:
    replace_once(
        "autoresearch/nightly_batch.py",
        'NIGHTLY_BRANCH = "autoresearch/nightly"',
        'RESULTS_BRANCH = "main"',
    )
    replace_once(
        "autoresearch/nightly_batch.py",
        '{REPOSITORY_URL}/tree/{NIGHTLY_BRANCH}',
        '{REPOSITORY_URL}/tree/{RESULTS_BRANCH}',
    )
    replace_once(
        "autoresearch/verify_scope.py",
        '"""Reject experiment branches that modify the fixed research harness."""',
        '"""Reject research worktrees that modify the fixed research harness."""',
    )

    health_path = ROOT / "scripts/system_health.py"
    health = health_path.read_text(encoding="utf-8")
    marker = '''        _check(
            "single_flight_live_scans",
'''
    if "single_main_branch_automation" not in health:
        check = '''        _check(
            "single_main_branch_automation",
            "autoresearch/framework-v1" not in nightly_workflow
            and "autoresearch/nightly" not in nightly_workflow
            and 'git push origin HEAD:"$MAIN_BRANCH"' in nightly_workflow,
            "autoresearch code and durable audit state use main only",
        ),
'''
        if marker not in health:
            raise RuntimeError("system health insertion marker missing")
        health = health.replace(marker, check + marker, 1)
    health = health.replace(
        '"incumbent is created automatically on the nightly audit branch"',
        '"incumbent is created automatically on main by the nightly workflow"',
    )
    health_path.write_text(health, encoding="utf-8")

    policy_path = ROOT / "autoresearch/policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy.pop("framework_branch", None)
    policy["repository_branch"] = "main"
    policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")

    write(
        "autoresearch/README.md",
        '''
# Wickless autoresearch

This directory runs controlled strategy experiments while protecting production code and execution safeguards.

## Single-branch boundary

- The repository has one durable branch: `main`.
- The nightly workflow records its starting commit and rejects worktree changes outside the audit allowlist.
- Durable research state is limited to `candidate.py`, `incumbent.json`, `results.jsonl`, `attempts.log`, `playbook.md`, `coach_state.json`, and JSON reports under `runs/`.
- `candidate.py` is restored to the neutral production baseline before publication.
- Production code, evaluator code, policy, scoring, tests, risk limits, and execution safeguards remain immutable during experiments.

## Scoring and operation

June and July 2026 are evaluated independently. Candidates must pass every hard gate and beat the incumbent objective. Every attempt appends one tamper-evident ledger record and one append-only attempt row. After every 20 cumulative attempts, the deterministic coach may update only bounded search guidance.

The scheduled workflow runs at 23:00 Europe/London, normally evaluates 12 diversified candidates, uploads a durable artifact, commits one validated audit-state update to `main`, and then sends Discord. The `main` branch and Actions artifacts are the source of truth.
        ''',
    )
    write(
        "autoresearch/program.md",
        '''
# Wickless autoresearch worker program

You are the worker in a controlled strategy-research loop. Profitability and robustness matter more than trade count.

## Setup

1. Run only inside the nightly Actions worktree checked out from `main`.
2. Record the starting commit before changing candidate or audit files.
3. Read the policy, playbook, incumbent, and recent attempts before selecting an idea.
4. Never change production code, evaluator code, policy, scoring, gates, tests, or workflows during a research run.

## Attempt loop

Temporarily edit only the literal `candidate.py`, evaluate it with fixed June/July BID/ASK data, append the report and ledgers, and keep only candidates that pass all gates and beat the incumbent. After the batch, restore the neutral production candidate. The workflow verifies the changed-file allowlist before committing one audit update to `main`.

After every 20 cumulative attempts, run the bounded coach. Never weaken success criteria or protected execution rules.
        ''',
    )

    automation_path = ROOT / "docs/AUTOMATION.md"
    automation = automation_path.read_text(encoding="utf-8")
    replacements = (
        (
            "This repository is designed to operate without routine manual intervention while preserving strict separation between production code and research audit history.",
            "This repository is designed to operate without routine manual intervention while using one durable branch, `main`, and strict file-scope separation between production code and research audit history.",
        ),
        (
            "`Nightly Wickless autoresearch` runs at 23:00 London time and writes audit history only to `autoresearch/nightly`.",
            "`Nightly Wickless autoresearch` runs at 23:00 London time and writes validated audit history to protected paths on `main`.",
        ),
        (
            "Branch roles:\n\n- `main`: production code and user-facing workflows.\n- `autoresearch/framework-v1`: fixed evaluator, planner, tests, policy, and automation framework.\n- `autoresearch/nightly`: append-only experiment results and generated audit artifacts.\n",
            "Repository model:\n\n- `main` is the only branch. Production code and audit state are separated by a strict changed-file allowlist.\n- Every nightly run records its starting commit, restores the neutral candidate, and rejects production-code changes.\n",
        ),
        (
            "1. Checks out the fixed framework.\n2. Resumes the isolated nightly branch and merges the framework forward.\n",
            "1. Checks out `main` and records the immutable starting commit.\n2. Runs experiments in the Actions worktree while enforcing the audit-file allowlist.\n",
        ),
        (
            "13. Pushes only `autoresearch/nightly`, then posts a benchmark-versus-best-trade-changing-test Discord summary.",
            "13. Commits one validated audit-state update to `main`, then posts a benchmark-versus-best-trade-changing-test Discord summary.",
        ),
        (
            "GitHub branches and uploaded artifacts are the source of truth. Discord is a delivery channel.",
            "The `main` branch and uploaded artifacts are the source of truth. Discord is a delivery channel.",
        ),
    )
    for old, new in replacements:
        if old not in automation:
            raise RuntimeError(f"AUTOMATION replacement missing: {old[:80]}")
        automation = automation.replace(old, new, 1)
    automation_path.write_text(automation, encoding="utf-8")

    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    old = "The repository includes an isolated research loop under `autoresearch/`, adapted\nfrom `eddoonn/autoresearch`. Experiments run only on `autoresearch/...` branches;\nthey cannot update `main`, and the editable candidate surface cannot change the\nstrategy's reward/risk, stop bounds, spread/cost limits, slippage, or\none-position safeguards."
    new = "The repository includes a constrained research loop under `autoresearch/`, adapted\nfrom `eddoonn/autoresearch`. The repository uses only `main`; each nightly run\nrecords its starting commit and may publish only allowlisted audit files. The\neditable candidate surface cannot change reward/risk, stop bounds, spread/cost\nlimits, slippage, or one-position safeguards."
    if old not in readme:
        raise RuntimeError("README branch paragraph missing")
    readme = readme.replace(old, new, 1)
    readme = readme.replace(
        "`autoresearch/nightly/autoresearch/runs/session-comparison/`.",
        "`autoresearch/runs/session-comparison/` on `main`.",
        1,
    )
    readme_path.write_text(readme, encoding="utf-8")


def rewrite_tests() -> None:
    path = ROOT / "tests/test_nightly_batch.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("    def test_workflow_runs_at_eleven_pm_london_and_pushes_only_nightly")
    end = text.index("    def test_space_is_large_deterministic_unique_and_has_no_clock_mutations", start)
    method = '''    def test_workflow_runs_at_eleven_pm_london_and_publishes_scoped_state_to_main(self) -> None:
        workflow = (ROOT / ".github/workflows/autoresearch-nightly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "0 23 * * *"', workflow)
        self.assertIn('timezone: "Europe/London"', workflow)
        self.assertIn('MAIN_BRANCH: main', workflow)
        self.assertIn('git push origin HEAD:"$MAIN_BRANCH"', workflow)
        self.assertIn('default: "12"', workflow)
        self.assertIn("--coach-interval 20", workflow)
        self.assertIn("refresh_baseline:", workflow)
        self.assertIn("bootstrap_benchmark:", workflow)
        self.assertIn("Ensure production reference benchmark", workflow)
        self.assertIn("Bootstrap the strongest valid benchmark", workflow)
        self.assertIn("Run worker and coach experiment loop", workflow)
        self.assertIn("--no-discord", workflow)
        self.assertNotIn("--git-commits", workflow)
        self.assertIn("Validate code, health, and changed-file scope", workflow)
        self.assertIn("Publish audit history to main", workflow)
        self.assertLess(
            workflow.index("Upload durable autoresearch audit artifact"),
            workflow.index("Publish audit history to main"),
        )
        self.assertLess(
            workflow.index("Publish audit history to main"),
            workflow.index("Notify Discord of nightly benchmark and best test"),
        )
        self.assertNotIn("autoresearch/framework-v1", workflow)
        self.assertNotIn("autoresearch/nightly", workflow)

        policy = json.loads((ROOT / "autoresearch/policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["repository_branch"], "main")
        folds = {fold["name"]: fold for fold in policy["folds"]}
        self.assertEqual(folds["june_2026"]["minimum_trades"], 10)
        self.assertEqual(folds["july_2026"]["minimum_trades"], 10)
        self.assertLess(policy["acceptance"]["minimum_net_r_each_fold"], -1e8)

'''
    path.write_text(text[:start] + method + text[end:], encoding="utf-8")

    path = ROOT / "tests/test_automation_polish.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("class GuardTests")
    end = text.index("class HealthTests", start)
    guard = '''class GuardTests(unittest.TestCase):
    def test_autoresearch_validation_targets_main_only(self) -> None:
        workflow = (ROOT / ".github/workflows/autoresearch.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pull_request:", workflow)
        self.assertIn("      - main", workflow)
        self.assertNotIn("startsWith(github.head_ref", workflow)
        self.assertNotIn("autoresearch/framework-v1", workflow)
        self.assertNotIn("autoresearch/nightly", workflow)


'''
    text = text[:start] + guard + text[end:]
    marker = '        self.assertEqual(checks["single_flight_live_scans"]["status"], "pass")\n'
    if marker not in text:
        raise RuntimeError("Health test marker missing")
    text = text.replace(
        marker,
        marker + '        self.assertEqual(checks["single_main_branch_automation"]["status"], "pass")\n',
        1,
    )
    path.write_text(text, encoding="utf-8")


def remove_temporary_files() -> None:
    paths = (
        ".github/workflows/single-branch-migration.yml",
        "ops/single-branch-migration.request",
        "scripts/single_branch_migration.py",
        "reports/maintenance/latest/single-branch-migration.json",
    )
    for path in paths:
        target = ROOT / path
        if target.exists():
            run("git", "rm", "-f", path)


def validate() -> None:
    run(
        sys.executable,
        "-m",
        "compileall",
        "-q",
        "wickless_bot.py",
        "live_scan.py",
        "time_display.py",
        "no_wick_research.py",
        "live_data.py",
        "run_daemon.py",
        "production_session.py",
        "autoresearch",
        "scripts",
        "tests",
    )
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    run(sys.executable, "scripts/system_health.py", "--output", "/tmp/system-health.json")

    hits: list[str] = []
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


def commit_and_push() -> None:
    run(
        "git",
        "add",
        "-A",
        "--",
        ".github/workflows",
        "autoresearch",
        "docs",
        "README.md",
        "scripts",
        "tests",
        "ops",
        "reports/maintenance/latest",
    )
    run("git", "status", "--short")
    run("git", "commit", "-m", "maintenance: consolidate repository onto main")
    for attempt in range(1, 4):
        pull = run("git", "pull", "--rebase", "origin", "main", check=False)
        if pull.returncode == 0:
            push = run("git", "push", "origin", "HEAD:main", check=False)
            if push.returncode == 0:
                return
        run("git", "rebase", "--abort", check=False)
        time.sleep(attempt * 5)
    raise RuntimeError("Failed to publish the single-branch migration")


def delete_old_branches() -> None:
    for branch in OLD_BRANCHES:
        exists = run(
            "git",
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
            check=False,
        ).returncode == 0
        if exists:
            run("git", "push", "origin", "--delete", branch)
    remaining = run("git", "ls-remote", "--heads", "origin", capture=True).stdout
    branches = [line for line in remaining.splitlines() if line.strip()]
    print(remaining, end="")
    if len(branches) != 1 or not branches[0].endswith("refs/heads/main"):
        raise RuntimeError(f"Expected only main, found {len(branches)} branches")


def main() -> int:
    run("git", "config", "user.name", "wickless-maintenance[bot]")
    run("git", "config", "user.email", "wickless-maintenance[bot]@users.noreply.github.com")
    import_nightly_state()
    rewrite_workflows()
    rewrite_code_and_docs()
    rewrite_tests()
    remove_temporary_files()
    validate()
    commit_and_push()
    delete_old_branches()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
