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
