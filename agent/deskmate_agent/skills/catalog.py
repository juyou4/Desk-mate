"""Default skill catalog (V10 Phase 10).

Centralises the :class:`SkillMetadata` + optional body loaders for
every first-party skill so the :class:`App` wiring is a single
``populate_default_registry(registry)`` call. Third-party / character
packs can add more entries via ``registry.register(...)`` without
touching this module.

Body loaders only run on demand (first ``registry.load_body`` call
for that id). A metadata-only entry — e.g. the build-status watcher,
which has nothing to inject into the LLM prompt — simply omits the
loader.
"""

from __future__ import annotations

from .registry import SkillBody, SkillMetadata, SkillRegistry

_CHAT_BODY = SkillBody(
    system_prompt=(
        "You are Deskmate, a tiny, warm, and witty macOS desktop pet. "
        "Reply in ≤ 60 words. Prefer plain conversational text. "
        "No markdown, no code fences, no lists."
    ),
    context_hints=(
        "When the user greets you, reply warmly and briefly.",
    ),
)


async def _load_chat_body() -> SkillBody:
    return _CHAT_BODY


_CODING_SESSION_BODY = SkillBody(
    system_prompt=(
        "The user appears to be asking about their coding activity. "
        "If you don't know the exact numbers, tell them they can run "
        "`deskmate today` in a terminal to get a precise breakdown "
        "by IDE."
    ),
    context_hints=(
        "Do not invent session durations or project names.",
    ),
)


async def _load_coding_session_body() -> SkillBody:
    return _CODING_SESSION_BODY


_BUILD_STATUS_BODY = SkillBody(
    system_prompt=(
        "If the user is asking about build or test status, suggest "
        "running the project's test command and point out that the "
        "island pill will light up automatically when results land."
    ),
    context_hints=(
        "The build-status island updates from "
        "~/.deskmate/build-status.json on a timer.",
    ),
)


async def _load_build_status_body() -> SkillBody:
    return _BUILD_STATUS_BODY


def default_skill_metadata() -> list[SkillMetadata]:
    """Return the metadata list without touching any body loaders.

    Exposed separately so tests that want to inspect the catalog
    without building a full :class:`SkillRegistry` instance can do so.
    """
    return [
        # ``chat.default`` is *not* proactive-safe on purpose: when the
        # agent speaks unprompted it goes through ``NudgeSelector``
        # (canned, deterministic). Injecting the conversational chat
        # prompt for a proactive turn would burn LLM tokens on a path
        # that has no user input to anchor the reply.
        SkillMetadata(
            id="chat.default",
            title="Conversational Chat",
            summary="Short, warm conversational replies to arbitrary text.",
            triggers=(
                "hi", "hello", "hey", "thanks", "thank you",
                "bye", "goodbye", "sup", "?",
            ),
            capabilities=("chat",),
            cost_class="llm_tokens",
            version="0.1.0",
            proactive_safe=False,
        ),
        # ``skill.coding_session`` is read-only — the
        # :class:`CodingSessionTracker` only observes perception ticks
        # and the :class:`CodingSessionStore` projects rollups. No
        # write surface, so it is safe for the agent to consult on a
        # proactive turn (e.g. composing a "long_day" nudge that
        # references today's coding minutes).
        SkillMetadata(
            id="skill.coding_session",
            title="Coding Session Awareness",
            summary="Answers questions about the user's current coding run.",
            triggers=(
                "coding", "coded", "how long", "today's coding",
                "xcode", "vscode", "cursor", "focus",
            ),
            capabilities=("chat", "perception_observer"),
            cost_class="free",
            version="0.1.0",
            proactive_safe=True,
        ),
        # ``skill.build_status`` is read-only — the watcher polls
        # ``~/.deskmate/build-status.json`` and exposes the result;
        # the skill itself never writes back. Safe for proactive
        # nudges that reference the latest CI signal.
        SkillMetadata(
            id="skill.build_status",
            title="Build / Test Status",
            summary="Points users at the build-status island pill.",
            triggers=("build", "tests", "ci", "green", "red", "failing"),
            capabilities=("chat", "watcher"),
            cost_class="free",
            version="0.1.0",
            proactive_safe=True,
        ),
    ]


def populate_default_registry(registry: SkillRegistry) -> SkillRegistry:
    """Register every first-party skill on ``registry`` in place.

    Returns the registry for chaining. Safe to call more than once:
    re-registering re-uses :meth:`SkillRegistry.register`'s override
    semantics and drops any cached body.
    """
    for meta in default_skill_metadata():
        loader = {
            "chat.default": _load_chat_body,
            "skill.coding_session": _load_coding_session_body,
            "skill.build_status": _load_build_status_body,
        }.get(meta.id)
        registry.register(meta, body_loader=loader)
    return registry


__all__ = [
    "default_skill_metadata",
    "populate_default_registry",
]
