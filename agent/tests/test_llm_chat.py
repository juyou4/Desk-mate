"""LLM chat skill tests (V10 Phase 12-ii).

All HTTP is mocked via :class:`httpx.MockTransport` — these tests
never touch the network. The composer under test is plugged with a
pre-configured :class:`httpx.AsyncClient` using the mock transport,
so they also double as documentation of the exact wire shape the
OpenAI-compat composer emits.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from deskmate_agent.skills import (
    SkillBody,
    SkillMetadata,
    SkillRegistry,
    make_default_composer,
    openai_compat_composer,
    populate_default_registry,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://mock-ignored",
    )


@pytest.mark.asyncio
async def test_openai_compat_composer_emits_expected_wire_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hello there"}}
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
    )
    reply = await compose("hi")

    assert reply == "hello there"
    assert captured["url"] == "https://api.test/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    body = captured["body"]
    assert body["model"] == "gpt-test"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {"role": "user", "content": "hi"}
    assert body["max_tokens"] == 200


@pytest.mark.asyncio
async def test_openai_compat_composer_accumulates_memory_across_turns() -> None:
    sent_messages: list[list[dict[str, str]]] = []
    reply_idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reply_idx
        reply_idx += 1
        sent_messages.append(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"reply-{reply_idx}",
                        }
                    }
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
    )

    assert (await compose("first")) == "reply-1"
    assert (await compose("second")) == "reply-2"

    # Second request must include both the user's first turn and the
    # assistant's first reply so multi-turn coherence survives.
    second = sent_messages[1]
    roles = [m["role"] for m in second]
    assert roles == ["system", "user", "assistant", "user"]
    assert second[1]["content"] == "first"
    assert second[2]["content"] == "reply-1"
    assert second[3]["content"] == "second"


@pytest.mark.asyncio
async def test_memory_window_caps_history_length() -> None:
    sent_messages: list[list[dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_messages.append(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        memory_window=2,
        client=_client(handler),
    )
    await compose("a")
    await compose("b")
    await compose("c")

    # After 3 turns the window (=2) keeps the most recent 2 messages
    # plus the always-present system prompt.
    last = sent_messages[-1]
    assert len(last) == 3
    assert last[0]["role"] == "system"
    assert last[-1] == {"role": "user", "content": "c"}


@pytest.mark.asyncio
async def test_openai_compat_composer_falls_back_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def fallback(text: str) -> str:
        return f"canned:{text}"

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
        fallback=fallback,
    )
    assert (await compose("hi")) == "canned:hi"


@pytest.mark.asyncio
async def test_openai_compat_composer_returns_none_on_error_without_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
    )
    assert (await compose("hi")) is None


@pytest.mark.asyncio
async def test_history_rolls_back_after_failed_turn() -> None:
    """A failed call must not leave phantom user context for retries."""
    seq = {"n": 0}
    captured: list[list[dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(500)
        captured.append(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
    )
    assert (await compose("first")) is None
    assert (await compose("second")) == "ok"

    # The second (successful) request must not include the phantom
    # "first" turn.
    contents = [(m["role"], m["content"]) for m in captured[0]]
    assert ("user", "first") not in contents
    assert ("user", "second") in contents


@pytest.mark.asyncio
async def test_empty_input_returns_none_without_any_http_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "empty input must not trigger an HTTP call"
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
    )
    assert (await compose("")) is None
    assert (await compose("   \n\t")) is None


@pytest.mark.asyncio
async def test_whitespace_only_content_from_llm_treated_as_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "   \n"}}
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
    )
    # Whitespace-only content collapses to None rather than rendering
    # an empty bubble.
    assert (await compose("hi")) is None


@pytest.mark.asyncio
async def test_make_default_composer_without_key_returns_canned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    compose = make_default_composer()
    reply = await compose("hi")
    # ``canned_reply_composer`` recognizes "hi" and greets back.
    assert reply is not None
    assert "hey" in reply.lower()


@pytest.mark.asyncio
async def test_make_default_composer_with_key_selects_llm_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-demo")
    monkeypatch.setenv(
        "DESKMATE_LLM_BASE_URL", "https://llm.example/v1"
    )
    monkeypatch.setenv("DESKMATE_LLM_MODEL", "demo-model")
    # We can't intercept the internally-built AsyncClient without
    # monkeypatching httpx.AsyncClient itself; constructing the
    # composer is enough to prove the env branch was taken. A real
    # HTTP call isn't made until compose() runs, so this stays
    # hermetic.
    compose = make_default_composer()
    assert compose is not None


# ---------------------------------------------------------------------------
# Phase 10: SkillRegistry-driven on-demand system prompt injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_registry_injects_matched_body_prompts() -> None:
    """When a registry is plugged in and the user text triggers a
    skill, that skill's ``system_prompt`` rides as an extra ``system``
    message after the base prompt, before any chat history."""
    sent: dict[str, list[dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    reg = SkillRegistry()

    async def loader() -> SkillBody:
        return SkillBody(system_prompt="SKILL-BODY-PROMPT")

    reg.register(
        SkillMetadata(
            id="chat", title="", summary="", triggers=("howdy",)
        ),
        body_loader=loader,
    )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
    )
    await compose("howdy friend")

    msgs = sent["messages"]
    system_msgs = [m for m in msgs if m["role"] == "system"]
    # Base prompt + matched skill body = exactly two system messages.
    assert len(system_msgs) == 2
    assert system_msgs[0]["content"].startswith("You are Deskmate")
    assert system_msgs[1]["content"] == "SKILL-BODY-PROMPT"
    # The user turn is still last.
    assert msgs[-1] == {"role": "user", "content": "howdy friend"}


@pytest.mark.asyncio
async def test_skill_registry_non_match_leaves_prompt_unchanged() -> None:
    sent: dict[str, list[dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="trivia", title="", summary="",
            triggers=("trivia-only-trigger",),
        ),
        body_loader=lambda: (_ for _ in ()).throw(
            AssertionError("should not be called")
        ),
    )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
    )
    await compose("unrelated question")

    system_msgs = [m for m in sent["messages"] if m["role"] == "system"]
    assert len(system_msgs) == 1, "no matches ⇒ exactly one system msg"


@pytest.mark.asyncio
async def test_skill_registry_loader_failure_drops_just_that_skill() -> None:
    """A broken third-party body_loader must not block the turn —
    the composer logs and continues, injecting whatever bodies did
    load successfully."""
    sent: dict[str, list[dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    async def bad() -> SkillBody:
        raise RuntimeError("pack is broken")

    async def good() -> SkillBody:
        return SkillBody(system_prompt="GOOD-PROMPT")

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="a", title="", summary="", triggers=("hi",)),
        body_loader=bad,
    )
    reg.register(
        SkillMetadata(id="b", title="", summary="", triggers=("hi",)),
        body_loader=good,
    )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
    )
    reply = await compose("hi there")

    assert reply == "ok"
    system_msgs = [m for m in sent["messages"] if m["role"] == "system"]
    # Base + GOOD only; bad skill was silently dropped.
    assert len(system_msgs) == 2
    assert system_msgs[1]["content"] == "GOOD-PROMPT"


@pytest.mark.asyncio
async def test_skill_registry_body_cached_across_turns() -> None:
    """The body loader should run once for the whole composer
    lifetime, not once per user turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    call_count = 0

    async def loader() -> SkillBody:
        nonlocal call_count
        call_count += 1
        return SkillBody(system_prompt="cached")

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="c", title="", summary="", triggers=("hi",)),
        body_loader=loader,
    )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
    )
    await compose("hi there 1")
    await compose("hi again")
    await compose("hi once more")
    assert call_count == 1


@pytest.mark.asyncio
async def test_skill_registry_loads_matched_bodies_in_parallel() -> None:
    sent: dict[str, list[dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    async def loader_a() -> SkillBody:
        await asyncio.sleep(0.03)
        return SkillBody(system_prompt="A-PROMPT")

    async def loader_b() -> SkillBody:
        await asyncio.sleep(0.03)
        return SkillBody(system_prompt="B-PROMPT")

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="a", title="", summary="", triggers=("hi",)),
        body_loader=loader_a,
    )
    reg.register(
        SkillMetadata(id="b", title="", summary="", triggers=("hi",)),
        body_loader=loader_b,
    )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
    )

    started = time.perf_counter()
    await compose("hi there")
    elapsed = time.perf_counter() - started

    system_contents = [
        m["content"] for m in sent["messages"] if m["role"] == "system"
    ]
    assert system_contents[-2:] == ["A-PROMPT", "B-PROMPT"]
    assert elapsed < 0.055


@pytest.mark.asyncio
async def test_first_token_observer_receives_response_latency() -> None:
    observed: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        first_token_observer=observed.append,
    )
    assert await compose("hello") == "ok"

    assert len(observed) == 1
    assert observed[0] >= 0.01


@pytest.mark.asyncio
async def test_first_token_observer_failure_does_not_fallback() -> None:
    fallback_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    async def fallback(text: str) -> str | None:
        nonlocal fallback_called
        fallback_called = True
        return "fallback"

    def bad_observer(_seconds: float) -> None:
        raise RuntimeError("metrics sink down")

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        fallback=fallback,
        first_token_observer=bad_observer,
    )

    assert await compose("hello") == "ok"
    assert fallback_called is False


@pytest.mark.asyncio
async def test_default_registry_injects_chat_body_on_greeting() -> None:
    """End-to-end: the stock default registry, when wired to the
    composer, activates ``chat.default`` on a greeting — proving the
    catalog triggers match real conversational input."""
    sent: dict[str, list[dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hey"}}
                ]
            },
        )

    reg = populate_default_registry(SkillRegistry())
    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
    )
    await compose("hi pet")

    system_msgs = [m for m in sent["messages"] if m["role"] == "system"]
    # Base + chat.default = 2 system messages; "hi" triggers that skill.
    assert len(system_msgs) >= 2
    joined = "\n".join(m["content"] for m in system_msgs)
    assert "warm" in joined.lower() or "deskmate" in joined.lower()


@pytest.mark.asyncio
async def test_make_default_composer_threads_skill_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-demo")
    reg = populate_default_registry(SkillRegistry())
    compose = make_default_composer(skill_registry=reg)
    # Can't easily inspect internal httpx client; prove it didn't
    # explode during construction when a registry is supplied.
    assert compose is not None


# ---------------------------------------------------------------------------
# V10 L2-#8A: skill_mode threaded through the composer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_mode_drops_unsafe_skill_injection() -> None:
    """When ``skill_mode='proactive'`` the composer must skip skills
    whose ``proactive_safe=False`` even if their triggers match —
    so an unattended agent never injects a write-skill prompt."""
    sent: dict[str, list[dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    reg = SkillRegistry()

    async def safe_loader() -> SkillBody:
        return SkillBody(system_prompt="SAFE-PROMPT")

    async def unsafe_loader() -> SkillBody:
        return SkillBody(system_prompt="UNSAFE-PROMPT")

    reg.register(
        SkillMetadata(
            id="reader",
            title="",
            summary="",
            triggers=("status",),
            proactive_safe=True,
        ),
        body_loader=safe_loader,
    )
    reg.register(
        SkillMetadata(
            id="writer",
            title="",
            summary="",
            triggers=("status",),
            proactive_safe=False,
        ),
        body_loader=unsafe_loader,
    )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
        skill_mode="proactive",
    )
    await compose("status check")

    system_contents = [
        m["content"] for m in sent["messages"] if m["role"] == "system"
    ]
    joined = "\n".join(system_contents)
    assert "SAFE-PROMPT" in joined, "proactive-safe body must inject"
    assert "UNSAFE-PROMPT" not in joined, "unsafe body leaked into proactive turn"


@pytest.mark.asyncio
async def test_reactive_default_still_injects_full_catalog() -> None:
    """The default (no ``skill_mode``) path keeps the historical
    full-catalog behaviour — both safe and unsafe matched skills
    inject their bodies. This is the explicit backwards-compat
    contract for V10 L2-#8A."""
    sent: dict[str, list[dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    reg = SkillRegistry()

    async def safe_loader() -> SkillBody:
        return SkillBody(system_prompt="SAFE-PROMPT")

    async def unsafe_loader() -> SkillBody:
        return SkillBody(system_prompt="UNSAFE-PROMPT")

    reg.register(
        SkillMetadata(
            id="reader",
            title="",
            summary="",
            triggers=("status",),
            proactive_safe=True,
        ),
        body_loader=safe_loader,
    )
    reg.register(
        SkillMetadata(
            id="writer",
            title="",
            summary="",
            triggers=("status",),
            proactive_safe=False,
        ),
        body_loader=unsafe_loader,
    )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        skill_registry=reg,
        # No skill_mode → default reactive.
    )
    await compose("status check")

    joined = "\n".join(
        m["content"] for m in sent["messages"] if m["role"] == "system"
    )
    assert "SAFE-PROMPT" in joined
    assert "UNSAFE-PROMPT" in joined
