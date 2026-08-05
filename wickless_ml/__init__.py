"""Wickless meta-label learning, deployment, and monitoring.

The package is deliberately standard-library only. Models rank or filter already
valid Wickless setups; they never generate trades, alter risk, or change strategy
execution.
"""

from .model import MODEL_FORMAT_VERSION

__all__ = ["MODEL_FORMAT_VERSION"]
