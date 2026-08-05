from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "apply_ml_learning_patch.py"))
    namespace["main"]()
    path = ROOT / "tests" / "test_ml_learning.py"
    source = path.read_text(encoding="utf-8")
    old = "probabilities = [0.9, 0.85, 0.8, 0.2, 0.15, 0.1]"
    new = "probabilities = [0.99, 0.98, 0.97, 0.03, 0.02, 0.01]"
    if source.count(old) != 1:
        raise RuntimeError("Expected one ML monitor calibration fixture")
    path.write_text(source.replace(old, new), encoding="utf-8")


if __name__ == "__main__":
    main()
