"""Proactive-chain composition (V10 L2-#5 / L3-B9).

The proactive chain runs the cheap deterministic rule filter first, then
consults a decision engine for the final yes/no. User-initiated messages
*never* route through this package — see ``dispatcher.py``.
"""

from __future__ import annotations

from .cooldown import CooldownTracker
from .engine import ProactiveEngine, ProactiveResult
from .nudges import NudgeComposer, NudgeSelector
from .rule_filter import RuleFilter, RuleResult

__all__ = [
    "CooldownTracker",
    "NudgeComposer",
    "NudgeSelector",
    "ProactiveEngine",
    "ProactiveResult",
    "RuleFilter",
    "RuleResult",
]
