"""Canned chat skill tests (V10 Phase 12-i)."""

from __future__ import annotations

import pytest

from deskmate_agent.skills import canned_reply_composer


@pytest.mark.asyncio
async def test_greeting_produces_hey_line() -> None:
    compose = canned_reply_composer()
    for greet in ("hi", "Hello", "HEY", "你好", "嗨"):
        assert (await compose(greet)) == "hey 👋  (LLM chat arrives next phase.)"


@pytest.mark.asyncio
async def test_farewell_produces_bye_line() -> None:
    compose = canned_reply_composer()
    for word in ("bye", "Goodbye", "cya", "再见"):
        assert (await compose(word)) == "bye 👋"


@pytest.mark.asyncio
async def test_thanks_produces_welcome_line() -> None:
    compose = canned_reply_composer()
    assert (await compose("thanks!")) == "you're welcome 💛"
    assert (await compose("谢谢")) == "you're welcome 💛"


@pytest.mark.asyncio
async def test_question_triggers_honest_dont_know() -> None:
    compose = canned_reply_composer()
    reply = await compose("what time is it?")
    assert reply is not None
    assert reply.startswith("good question")


@pytest.mark.asyncio
async def test_fallback_echo_truncates_long_inputs() -> None:
    compose = canned_reply_composer()
    long_text = "a" * 200
    reply = await compose(long_text)
    assert reply is not None
    assert reply.startswith("you said: ")
    # 77 'a's + ellipsis
    assert "…" in reply
    assert len(reply) <= len("you said: ") + 80


@pytest.mark.asyncio
async def test_empty_input_returns_none() -> None:
    compose = canned_reply_composer()
    assert (await compose("")) is None
    assert (await compose("   \t\n")) is None
