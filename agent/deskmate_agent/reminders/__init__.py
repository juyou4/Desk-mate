"""Reminder skill (V10 Phase 5 / L2-#4).

First plug-in skill that both owns its own model/state (in-memory) and
drives :class:`CompanionIntent` emission. The design deliberately mirrors
what future LLM-driven skills will do:

- Pure data model (:mod:`.model`).
- Observable in-memory store (:mod:`.store`).
- An asyncio scheduler that turns store state + wall-clock into typed
  intents (:mod:`.scheduler`).
"""

from __future__ import annotations

from .model import Reminder, ReminderStatus
from .scheduler import ReminderScheduler
from .store import ReminderStore, ReminderStoreEvent

__all__ = [
    "Reminder",
    "ReminderScheduler",
    "ReminderStatus",
    "ReminderStore",
    "ReminderStoreEvent",
]
