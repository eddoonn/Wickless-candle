"""Constrained autonomous research tools for the Wickless strategy."""

from production_session import install_production_session


# Baselines, bootstrap searches, and every future candidate use the same
# production London-or-New-York session union before evaluator import.
install_production_session()
