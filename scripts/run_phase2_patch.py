from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import apply_phase2_patch as patch


def patch_program() -> None:
    path = ROOT / "autoresearch" / "program.md"
    old = (
        "Temporarily edit only the literal `candidate.py`, evaluate it with the "
        "Phase 1 walk-forward policy, append the report and ledgers, and keep only "
        "candidates that pass every gate and beat the incumbent. Neighbourhood "
        "backtests run only after the main candidate passes its base gates. After "
        "the batch, restore the neutral production candidate. The workflow verifies "
        "the changed-file allowlist before committing one audit update to `main`.\n"
    )
    new = (
        "Temporarily edit only the literal `candidate.py`, evaluate it with the "
        "Phase 1 walk-forward policy, append the report and ledgers, and keep only "
        "candidates that pass every gate and beat the incumbent. Phase 2 ranks the "
        "bounded untested proposal pool with an exact-profile constrained surrogate, "
        "reserves at least 20% of each trained batch for uncertainty-led exploration, "
        "and falls back to deterministic diversification until enough comparable "
        "observations exist. The optimiser may choose tests but can never promote a "
        "candidate or change a gate. Neighbourhood backtests run only after the main "
        "candidate passes its base gates. After the batch, restore the neutral "
        "production candidate. The workflow verifies the changed-file allowlist "
        "before committing one audit update to `main`.\n"
    )
    patch.replace_once(path, old, new)


patch.patch_program = patch_program
patch.main()
