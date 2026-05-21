"""Phase presentation policy tests."""

from __future__ import annotations

import pytest

from deskmate_agent.agent_phase import presentation_for_phase
from deskmate_agent.protocol.state import Priority
from deskmate_agent.sessions import SessionPhase


@pytest.mark.parametrize(
    ("phase", "priority", "pet_state", "fallback"),
    [
        (SessionPhase.WAITING_FOR_APPROVAL, Priority.P0, "alert", "needs approval"),
        (SessionPhase.WAITING_FOR_ANSWER, Priority.P0, "alert", "waiting for your answer"),
        (SessionPhase.FAILED, Priority.P1, "alert", "failed"),
        (SessionPhase.RUNNING_TOOL, Priority.P1, "working", "running a tool"),
        (SessionPhase.EDITING, Priority.P1, "working", "editing code"),
        (SessionPhase.TESTING, Priority.P1, "working", "running tests"),
        (SessionPhase.THINKING, Priority.P1, "thinking", "thinking"),
        (SessionPhase.COMPLETED, Priority.P2, "happy", "completed"),
        (SessionPhase.RUNNING, Priority.P2, "working", "running"),
    ],
)
def test_presentation_for_phase_defaults(phase, priority, pet_state, fallback) -> None:
    got = presentation_for_phase(phase, source="codex")

    assert got.priority is priority
    assert got.pet_state == pet_state
    assert fallback in got.island_detail


def test_presentation_prefers_prompt_summary_title() -> None:
    assert (
        presentation_for_phase(
            SessionPhase.WAITING_FOR_APPROVAL,
            source="claude_code",
            prompt="Allow Bash?",
            summary="summary",
            title="title",
        ).island_detail
        == "Allow Bash?"
    )
    assert (
        presentation_for_phase(
            SessionPhase.EDITING,
            source="cursor",
            summary="Edited app.py",
            title="title",
        ).island_detail
        == "Edited app.py"
    )
