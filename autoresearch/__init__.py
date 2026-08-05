"""Constrained autonomous research tools for the Wickless strategy."""

from production_session import install_production_session


# Baselines, bootstrap searches, and every future candidate use the same
# production London-or-New-York session union before evaluator import.
install_production_session()

# Phase 1 decorates the evaluator only when the separate walk-forward policy is
# supplied. The production June/July policy retains its existing behaviour.
from autoresearch.phase1_validation import install_phase1_validation

install_phase1_validation()
