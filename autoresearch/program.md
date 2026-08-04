# Wickless autoresearch worker program

You are the worker in a controlled strategy-research loop. Profitability and
robustness matter more than trade count.

## Setup

1. Work only on an `autoresearch/<run-tag>` branch created from
   `autoresearch/framework-v1`; never update `main`.
2. Read `README.md`, `policy.json`, `playbook.md`, the incumbent, and recent
   `attempts.log` records before choosing an idea.
3. Confirm the fixed June and July Dukascopy M1 BID/ASK data is available.
4. Never change the evaluator, policy, objective, acceptance gates, tests,
   production strategy, live scanner, or workflow while running experiments.

## Editable surface

Edit only `candidate.py`. It must remain one literal `CANDIDATE` dictionary.
Change one coherent idea per attempt and describe it in one sentence. Protected
risk and execution fields are not exposed as candidate parameters.

## Attempt loop

1. Follow `Explore next` in `playbook.md` and avoid every category listed under
   `Do not try`.
2. Prefer a one-parameter test before a two-category combination.
3. Edit and commit `candidate.py`.
4. Run `python -m autoresearch.run_experiment --data-root "$WICKLESS_DATA_ROOT"`.
5. Commit the generated report, ledger, incumbent, and attempt log.
6. Keep an accepted candidate; after a discard, restore the incumbent candidate
   without erasing the discarded attempt from history.
7. Push only the current autoresearch branch.

After every attempt, one line must be appended to `attempts.log` in this exact
CSV field order: timestamp, a one-sentence description of what was tried, the
category of idea, the unchanged objective score, and `KEPT` or `DISCARDED`.
Never edit or delete previous lines. Never summarize attempts into one line.
`run_experiment.py` enforces this append automatically.

## Worker/coach handoff

After every 20 cumulative attempts, run the coach once. The nightly dispatcher
does this automatically, including when attempt 20 occurs in the middle of a
batch. The next worker selection must reread the resulting `playbook.md`.

Crashes are failures. Fix a trivial implementation error once; otherwise record
the failed idea and move on. Never weaken success criteria to make an idea pass.
