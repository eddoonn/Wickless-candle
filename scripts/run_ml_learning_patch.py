from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one ML fixture occurrence: {old}")
    return source.replace(old, new)


def main() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "apply_ml_learning_patch.py"))
    namespace["main"]()
    path = ROOT / "tests" / "test_ml_learning.py"
    source = path.read_text(encoding="utf-8")
    source = replace_once(
        source,
        "probabilities = [0.9, 0.85, 0.8, 0.2, 0.15, 0.1]",
        "probabilities = [0.99, 0.98, 0.97, 0.96, 0.95, 0.03, 0.02, 0.01]",
    )
    source = replace_once(
        source,
        "outcomes = [1, 1, 1, 0, 0, 0]",
        "outcomes = [1, 1, 1, 1, 1, 0, 0, 0]",
    )
    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
