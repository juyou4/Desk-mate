"""Shared phase policy for agent sessions.

All external agent inputs eventually normalize to ``SessionPhase``. This module
keeps the user-visible mapping in one place so hooks, Codex.app server events,
Claude JSONL, and future IDE adapters produce consistent island copy.
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol.state import Priority
from .sessions import SessionPhase


@dataclass(frozen=True)
class PhasePresentation:
    priority: Priority
    pet_state: str
    island_detail: str
    force_notification: bool = False


def presentation_for_phase(
    phase: SessionPhase,
    *,
    source: str,
    summary: str = "",
    title: str = "",
    prompt: str = "",
) -> PhasePresentation:
    label = _source_label(source)
    text = _clip(prompt or summary or title)

    if phase is SessionPhase.WAITING_FOR_APPROVAL:
        return PhasePresentation(
            priority=Priority.P0,
            pet_state="alert",
            island_detail=text or f"{label} needs approval.",
            force_notification=True,
        )
    if phase is SessionPhase.WAITING_FOR_ANSWER:
        return PhasePresentation(
            priority=Priority.P0,
            pet_state="alert",
            island_detail=text or f"{label} is waiting for your answer.",
            force_notification=True,
        )
    if phase is SessionPhase.FAILED:
        return PhasePresentation(
            priority=Priority.P1,
            pet_state="alert",
            island_detail=text or f"{label} failed.",
        )
    if phase is SessionPhase.RUNNING_TOOL:
        return PhasePresentation(
            priority=Priority.P1,
            pet_state="working",
            island_detail=text or f"{label} is running a tool.",
        )
    if phase is SessionPhase.EDITING:
        return PhasePresentation(
            priority=Priority.P1,
            pet_state="working",
            island_detail=text or f"{label} is editing code.",
        )
    if phase is SessionPhase.TESTING:
        return PhasePresentation(
            priority=Priority.P1,
            pet_state="working",
            island_detail=text or f"{label} is running tests.",
        )
    if phase is SessionPhase.THINKING:
        return PhasePresentation(
            priority=Priority.P1,
            pet_state="thinking",
            island_detail=text or f"{label} is thinking.",
        )
    if phase is SessionPhase.COMPLETED:
        return PhasePresentation(
            priority=Priority.P2,
            pet_state="happy",
            island_detail=text or f"{label} completed.",
        )
    return PhasePresentation(
        priority=Priority.P2,
        pet_state="working",
        island_detail=text or f"{label} is running.",
    )


def _source_label(source: str) -> str:
    return {
        "codex": "Codex",
        "claude": "Claude",
        "claude_code": "Claude Code",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
        "vscode": "VS Code",
        "xcode": "Xcode",
        "jetbrains": "JetBrains",
        "opencode": "OpenCode",
    }.get(source, source.replace("_", " ").title() or "Agent")


def _clip(value: str, *, limit: int = 96) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


__all__ = ["PhasePresentation", "presentation_for_phase"]
