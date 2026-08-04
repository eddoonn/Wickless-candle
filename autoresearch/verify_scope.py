#!/usr/bin/env python3
"""Reject experiment branches that modify the fixed research harness."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ALLOWED_EXACT = {
    "autoresearch/candidate.py",
    "autoresearch/incumbent.json",
    "autoresearch/results.jsonl",
    "autoresearch/attempts.log",
    "autoresearch/playbook.md",
    "autoresearch/coach_state.json",
}
ALLOWED_PREFIX = "autoresearch/runs/"


def changed_files(base: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def verify(paths: list[str]) -> list[str]:
    return [
        path
        for path in paths
        if path not in ALLOWED_EXACT
        and not (path.startswith(ALLOWED_PREFIX) and path.endswith(".json"))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    paths = changed_files(args.base)
    forbidden = verify(paths)
    if forbidden:
        print("Protected autoresearch scope changed:")
        for path in forbidden:
            print(f"- {path}")
        return 1
    print(f"Scope OK: {len(paths)} allowed changed file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

