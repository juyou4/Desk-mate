"""Canned chat skill (V10 Phase 12-i).

A deterministic reply composer used as the default plug-in for the
reactive chain until a real LLM skill lands. Lets the full Swift ↔
Python loop demo a "user types → pet answers" interaction without
any network dependency.

Intentionally minimal: this module is a *substitute* for LLM content,
not a chatbot. The moment an LLM composer wires in, it replaces this
composer at the same :data:`dispatcher.ReplyComposer` seam — the rest
of the pipeline (dispatcher / intent sink / bridge / Swift UI) stays
unchanged.
"""

from __future__ import annotations

from ..dispatcher import ReplyComposer

_GREETINGS = ("hi", "hello", "hey", "yo", "你好", "嗨", "哈喽")
_FAREWELLS = ("bye", "goodbye", "cya", "再见", "拜拜")
_THANKS = ("thanks", "thank you", "thx", "谢谢", "多谢")


def canned_reply_composer() -> ReplyComposer:
    """Return a :data:`ReplyComposer` that answers short greetings /
    thanks / farewells with canned lines and falls back to a visible
    echo for everything else. Returns ``None`` for empty input so the
    dispatcher can render the default placeholder.
    """

    async def compose(text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        lower = stripped.lower()

        if any(tok in lower for tok in _GREETINGS):
            return "hey 👋  (LLM chat arrives next phase.)"

        if any(tok in lower for tok in _FAREWELLS):
            return "bye 👋"

        if any(tok in lower for tok in _THANKS):
            return "you're welcome 💛"

        if stripped.endswith("?") or stripped.endswith("？"):
            return (
                "good question — I can't reason about it until the "
                "LLM skill ships. For now I'm just parroting."
            )

        # Echo — truncate to keep the bubble compact.
        clipped = stripped if len(stripped) <= 80 else stripped[:77] + "…"
        return f"you said: {clipped}"

    return compose


__all__ = ["canned_reply_composer"]
