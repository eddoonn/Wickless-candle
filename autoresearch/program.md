# Wickless autoresearch program

You are running controlled strategy research inside `eddoonn/Wickless-candle`.
Profitability and robustness are more important than trade count.

## Setup

1. Propose a short run tag and create a fresh branch named
   `autoresearch/<run-tag>` from `autoresearch/framework-v1`.
2. Never commit, push, merge, or update `main`.
3. Read `autoresearch/README.md`, `autoresearch/policy.json`, and
   `autoresearch/candidate.py`.
4. Confirm the data root contains the June and July 2026 Dukascopy M1 BID/ASK
   directories named in `policy.json`.
5. Run the unchanged baseline once if `autoresearch/incumbent.json` does not
   exist.

## What you may change

Edit only `autoresearch/candidate.py`. It must remain a literal `CANDIDATE`
dictionary. Change one coherent idea per experiment and explain it in the name
and description. Do not modify the evaluator, policy, runner, production
strategy, live scanner, workflows, or tests.

The literal parser and branch scope check enforce this boundary. Protected risk
and execution fields are not exposed as candidate parameters.

## Experiment loop

Repeat until the human interrupts:

1. Inspect the incumbent and recent `results.jsonl` records.
2. Form one testable idea. Prefer a simple one-parameter change before a
   combination.
3. Edit only `autoresearch/candidate.py` and commit it on the current
   `autoresearch/<run-tag>` branch.
4. Run:

   ```bash
   python -m autoresearch.run_experiment --data-root "$WICKLESS_DATA_ROOT" \
     > autoresearch/run.log 2>&1
   ```

5. Read only the JSON summary or the end of `autoresearch/run.log`.
6. Commit the generated `results.jsonl`, `incumbent.json`, and `runs/*.json`.
7. If status is `keep`, retain the candidate and continue from it.
8. If status is `discard`, revert the candidate commit after committing the
   audit output. Never erase the discarded experiment from branch history.
9. Push only the current `autoresearch/<run-tag>` branch.

Crashes are failures. Fix a trivial implementation error once; otherwise record
the failed idea and move on. Never weaken the policy to make a candidate pass.

## Interpretation

July was used to design the production strategy, so a larger July result alone
is not sufficient. The evaluator first prioritizes the worst fold, then total
net R. More trades matter only after profitability, profit factor, and drawdown.

