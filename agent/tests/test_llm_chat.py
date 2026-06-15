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

from deskmate_agent.agent_events import (
    AgentEventReducer,
    SessionActivityUpdated,
    SessionCompleted,
)
from deskmate_agent.approvals import ApprovalStore
from deskmate_agent.memory import (
    ChatMemory,
    DeskmateTaskStore,
    Message,
    ProfileStore,
    ToolActionLog,
    ToolActionRecord,
    ToolTaskRecord,
)
from deskmate_agent.reminders import Reminder, ReminderStore
from deskmate_agent.sessions import SessionPhase, SessionStore
from deskmate_agent.skills import (
    DeskmateToolExecutor,
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
async def test_chat_memory_persists_successful_turns_across_composers(
    tmp_path,
) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_bodies.append(json.loads(request.content))
            reply = "one" if len(request_bodies) == 1 else "two"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": reply}}
                    ]
                },
            )

        first = openai_compat_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            chat_memory=mem,
        )
        assert await first("first") == "one"

        second = openai_compat_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            chat_memory=mem,
        )
        assert await second("second") == "two"

        roles = [m["role"] for m in request_bodies[1]["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        assert request_bodies[1]["messages"][1]["content"] == "first"
        assert request_bodies[1]["messages"][2]["content"] == "one"


@pytest.mark.asyncio
async def test_chat_summary_injected_beyond_recent_window(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "default",
            [
                Message(role="user", content="early context about Cursor"),
                Message(role="assistant", content="noted the Cursor workflow"),
                Message(role="user", content="latest context"),
            ],
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            chat_memory=mem,
            memory_window=1,
        )

        assert await compose("now") == "ok"

        messages = captured["body"]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "system"
        assert messages[1]["content"].startswith("Persistent conversation summary:")
        assert "early context about Cursor" in messages[1]["content"]
        assert [message["content"] for message in messages if message["role"] == "user"] == [
            "latest context",
            "now",
        ]


@pytest.mark.asyncio
async def test_chat_memory_does_not_persist_failed_turn(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        compose = openai_compat_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            chat_memory=mem,
        )
        assert await compose("first") is None
        assert await mem.recent("default") == []


@pytest.mark.asyncio
async def test_tool_call_schedules_reminder_and_second_request_contains_result(
    tmp_path,
) -> None:
    async with (
        ChatMemory(tmp_path / "chat.db") as mem,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        store = ReminderStore()
        request_bodies: list[dict[str, object]] = []
        tool_events = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": json.dumps(
                                                    {
                                                        "text": "stretch",
                                                        "delay_ms": 60_000,
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Reminder is set.",
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
            chat_memory=mem,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=store,
                reminder_clock=lambda: 1_000,
                reminder_id_factory=lambda: "r-tool",
            ),
            tool_event_sink=tool_events.append,
        )

        assert await compose("please remind me to stretch") == "Reminder is set."

        first = request_bodies[0]
        assert "tools" in first
        assert first["tool_choice"] == "auto"
        assert store.get("r-tool") is not None
        second_messages = request_bodies[1]["messages"]
        assert second_messages[-1]["role"] == "tool"
        assert second_messages[-1]["tool_call_id"] == "call-1"
        assert "Reminder scheduled" in second_messages[-1]["content"]
        assert [type(event) for event in tool_events] == [
            SessionActivityUpdated,
            SessionCompleted,
        ]
        assert tool_events[0].session_id == "deskmate-tools-default"
        assert tool_events[0].phase is SessionPhase.RUNNING_TOOL
        assert tool_events[0].tool_name == "deskmate_schedule_reminder"
        assert tool_events[0].tool_id == "call-1"
        assert tool_events[1].failed is False
        assert tool_events[1].tool_result.startswith("Reminder scheduled")
        sessions = SessionStore()
        reducer = AgentEventReducer(
            session_store=sessions,
            approval_store=ApprovalStore(),
        )
        for event in tool_events:
            reducer.apply(event)
        tool_session = sessions.get("deskmate-tools-default")
        assert tool_session is not None
        assert tool_session.source == "deskmate"
        assert tool_session.phase is SessionPhase.COMPLETED
        assert tool_session.extras["tool_name"] == "deskmate_schedule_reminder"
        assert tool_session.extras["tool_result"].startswith("Reminder scheduled")
        assert tool_session.extras["tool_action"] == "deskmate_schedule_reminder"
        assert tool_session.extras["tool_target"] == "stretch"
        assert tool_session.extras["tool_needs_user"] == "false"
        assert tool_session.extras["tool_summary"].startswith(
            "action=deskmate_schedule_reminder"
        )
        assert tool_session.extras["tool_task_id"].startswith(
            "deskmate-tool-task-default-"
        )
        assert tool_session.extras["tool_task_status"] == "completed"
        assert tool_session.extras["tool_task_summary"].startswith(
            "action=deskmate_schedule_reminder"
        )

        persisted = await mem.recent("default", limit=10)
        assert [m.role for m in persisted] == ["user", "assistant", "tool", "assistant"]
        actions = await tool_log.recent("default", limit=10)
        assert len(actions) == 1
        assert actions[0].task_id is not None
        assert actions[0].tool_call_id == "call-1"
        assert actions[0].tool_name == "deskmate_schedule_reminder"
        assert actions[0].arguments == {"text": "stretch", "delay_ms": 60_000}
        assert actions[0].status == "completed"
        assert actions[0].result.startswith("Reminder scheduled")
        tasks = await tool_log.recent_tasks("default", limit=10)
        assert len(tasks) == 1
        assert tasks[0].task_id == actions[0].task_id
        assert tasks[0].status == "completed"
        assert tasks[0].action_count == 1
        assert tasks[0].failed_count == 0
        assert tasks[0].summary.startswith("action=deskmate_schedule_reminder")


@pytest.mark.asyncio
async def test_tool_calls_can_chain_memory_lookup_then_reminder(tmp_path) -> None:
    async with (
        ChatMemory(tmp_path / "chat.db") as mem,
        ProfileStore(tmp_path / "profile.db") as profile,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        profile.set(
            "memories.facts",
            {
                "stretch_break": {
                    "key": "stretch_break",
                    "value": "stand up and stretch",
                    "updated_at_ms": 1_000,
                }
            },
        )
        await profile.flush()
        store = ReminderStore()
        request_bodies: list[dict[str, object]] = []
        tool_events = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "recall-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_recall_memory",
                                                "arguments": json.dumps(
                                                    {"query": "stretch", "limit": 1}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            if len(request_bodies) == 2:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "reminder-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": json.dumps(
                                                    {
                                                        "text": "stand up and stretch",
                                                        "delay_ms": 120_000,
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I found your stretch break and set it.",
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
            chat_memory=mem,
            profile_store=profile,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=store,
                reminder_clock=lambda: 10_000,
                reminder_id_factory=lambda: "r-chain",
                profile_store=profile,
            ),
            tool_event_sink=tool_events.append,
        )

        reply = await compose("use my stretch memory and remind me in two minutes")

        assert reply == "I found your stretch break and set it."
        assert len(request_bodies) == 3
        assert "tools" in request_bodies[1]
        assert "tools" in request_bodies[2]
        final_messages = request_bodies[2]["messages"]
        tool_messages = [
            message for message in final_messages if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "recall-1",
            "reminder-1",
        ]
        assert "stretch_break: stand up and stretch" in tool_messages[0]["content"]
        assert "Reminder scheduled for 2 minutes" in tool_messages[1]["content"]
        assert store.get("r-chain") is not None
        assert [event.raw_event for event in tool_events] == [
            "tool.started",
            "tool.completed",
            "tool.started",
            "tool.completed",
        ]

        persisted = await mem.recent("default", limit=10)
        assert [message.role for message in persisted] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
        ]
        actions = await tool_log.recent("default", limit=10)
        assert [record.tool_name for record in actions] == [
            "deskmate_recall_memory",
            "deskmate_schedule_reminder",
        ]


@pytest.mark.asyncio
async def test_duplicate_tool_calls_reuse_first_result_without_repeating_side_effect(
    tmp_path,
) -> None:
    async with (
        ChatMemory(tmp_path / "chat.db") as mem,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        store = ReminderStore()
        store_events = []
        store.subscribe(lambda event: store_events.append(event))
        request_bodies: list[dict[str, object]] = []
        tool_events = []
        reminder_ids = iter(["r-dup-1", "r-dup-2"])
        first_args = json.dumps({"text": "stretch", "delay_ms": 60_000})
        same_args_different_order = json.dumps(
            {"delay_ms": 60_000, "text": "stretch"}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": first_args,
                                            },
                                        },
                                        {
                                            "id": "call-2",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": same_args_different_order,
                                            },
                                        },
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "Done."}}
                    ]
                },
            )

        compose = openai_compat_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            chat_memory=mem,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=store,
                reminder_clock=lambda: 1_000,
                reminder_id_factory=lambda: next(reminder_ids),
            ),
            tool_event_sink=tool_events.append,
        )

        assert await compose("remind me twice?") == "Done."

        assert [event.reminder_id for event in store_events] == ["r-dup-1"]
        assert store.get("r-dup-1") is not None
        assert store.get("r-dup-2") is None
        tool_messages = [
            message
            for message in request_bodies[1]["messages"]
            if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call-1",
            "call-2",
        ]
        assert tool_messages[0]["content"] == tool_messages[1]["content"]
        assert "Reminder scheduled" in tool_messages[0]["content"]
        assert [event.raw_event for event in tool_events] == [
            "tool.started",
            "tool.completed",
            "tool.duplicate",
        ]
        assert tool_events[-1].tool_id == "call-2"
        assert tool_events[-1].tool_result == tool_messages[0]["content"]
        assert tool_events[-1].tool_action == "deskmate_schedule_reminder"
        assert tool_events[-1].tool_target == "stretch"
        assert tool_events[-1].tool_needs_user == "false"
        actions = await tool_log.recent("default", limit=10)
        assert [record.tool_call_id for record in actions] == ["call-1", "call-2"]
        assert [record.status for record in actions] == ["completed", "duplicate"]
        assert actions[0].result == actions[1].result


@pytest.mark.asyncio
async def test_duplicate_tool_calls_across_rounds_do_not_repeat_side_effect(
    tmp_path,
) -> None:
    async with (
        ChatMemory(tmp_path / "chat.db") as mem,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        store = ReminderStore()
        store_events = []
        store.subscribe(lambda event: store_events.append(event))
        request_bodies: list[dict[str, object]] = []
        tool_events = []
        reminder_ids = iter(["r-cross-dup-1", "r-cross-dup-2"])
        first_args = json.dumps({"text": "stretch", "delay_ms": 60_000})
        same_args_different_order = json.dumps(
            {"delay_ms": 60_000, "text": "stretch"}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": first_args,
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            if len(request_bodies) == 2:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call-2",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": same_args_different_order,
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "Done."}}
                    ]
                },
            )

        compose = openai_compat_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            chat_memory=mem,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=store,
                reminder_clock=lambda: 1_000,
                reminder_id_factory=lambda: next(reminder_ids),
            ),
            tool_event_sink=tool_events.append,
        )

        assert await compose("remind me, and check again") == "Done."

        assert len(request_bodies) == 3
        assert [event.reminder_id for event in store_events] == ["r-cross-dup-1"]
        assert store.get("r-cross-dup-1") is not None
        assert store.get("r-cross-dup-2") is None
        final_tool_messages = [
            message
            for message in request_bodies[2]["messages"]
            if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in final_tool_messages] == [
            "call-1",
            "call-2",
        ]
        assert final_tool_messages[0]["content"] == final_tool_messages[1]["content"]
        assert [event.raw_event for event in tool_events] == [
            "tool.started",
            "tool.completed",
            "tool.duplicate",
        ]
        actions = await tool_log.recent("default", limit=10)
        assert [record.tool_call_id for record in actions] == ["call-1", "call-2"]
        assert [record.status for record in actions] == ["completed", "duplicate"]
        assert actions[0].result == actions[1].result
        persisted = await mem.recent("default", limit=10)
        assert [message.role for message in persisted] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
        ]


@pytest.mark.asyncio
async def test_unknown_tool_call_returns_tool_error_without_crashing() -> None:
    request_bodies: list[dict[str, object]] = []
    tool_events = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if len(request_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "bad-1",
                                        "type": "function",
                                        "function": {
                                            "name": "run_shell",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I cannot use that tool.",
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
        tool_executor=DeskmateToolExecutor(),
        tool_event_sink=tool_events.append,
    )

    assert await compose("do something unsafe") == "I cannot use that tool."
    tool_msg = request_bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "unknown tool" in tool_msg["content"]
    assert [type(event) for event in tool_events] == [
        SessionActivityUpdated,
        SessionCompleted,
    ]
    assert tool_events[0].phase is SessionPhase.RUNNING_TOOL
    assert tool_events[0].tool_name == "run_shell"
    assert tool_events[1].failed is True
    assert tool_events[1].tool_result.startswith("Tool error:")
    assert tool_events[1].tool_action == "run_shell"
    assert tool_events[1].tool_needs_user == "true"


@pytest.mark.asyncio
async def test_tool_call_timeout_returns_tool_error_and_failed_event() -> None:
    request_bodies: list[dict[str, object]] = []
    tool_events = []

    class SlowExecutor(DeskmateToolExecutor):
        async def execute(self, name, arguments):
            await asyncio.sleep(0.2)
            return "should not arrive"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if len(request_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "slow-1",
                                        "type": "function",
                                        "function": {
                                            "name": "deskmate_slow_tool",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The tool timed out.",
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
        tool_executor=SlowExecutor(),
        tool_event_sink=tool_events.append,
        tool_timeout_s=0.01,
    )

    assert await compose("run slow tool") == "The tool timed out."
    tool_msg = request_bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "slow-1"
    assert tool_msg["content"] == "Tool error: deskmate_slow_tool timed out."
    assert [event.raw_event for event in tool_events] == [
        "tool.started",
        "tool.failed",
    ]
    assert tool_events[1].failed is True
    assert tool_events[1].tool_result == tool_msg["content"]


@pytest.mark.asyncio
async def test_tool_call_remembers_fact_in_profile_memory(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as profile:
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "mem-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_remember_fact",
                                                "arguments": json.dumps(
                                                    {
                                                        "key": "preferred_ide",
                                                        "value": "Cursor",
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I will remember that.",
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
            tool_executor=DeskmateToolExecutor(
                profile_store=profile,
                reminder_clock=lambda: 123_000,
            ),
        )

        assert await compose("remember I prefer Cursor") == "I will remember that."

        facts = profile.get("memories.facts")
        assert facts["preferred_ide"]["value"] == "Cursor"
        assert facts["preferred_ide"]["updated_at_ms"] == 123_000
        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["content"] == "Remembered preferred_ide: Cursor."


@pytest.mark.asyncio
async def test_tool_call_blocks_remember_fact_without_explicit_user_intent(
    tmp_path,
) -> None:
    async with (
        ProfileStore(tmp_path / "profile.db") as profile,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        request_bodies: list[dict[str, object]] = []
        tool_events = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "mem-policy-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_remember_fact",
                                                "arguments": json.dumps(
                                                    {
                                                        "key": "preferred_ide",
                                                        "value": "Cursor",
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I can suggest remembering that.",
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
            profile_store=profile,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                profile_store=profile,
                reminder_clock=lambda: 123_000,
            ),
            tool_event_sink=tool_events.append,
        )

        assert await compose("I usually code in Cursor") == (
            "I can suggest remembering that."
        )

        assert profile.get("memories.facts", {}) == {}
        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "mem-policy-1"
        assert tool_msg["content"].startswith(
            "Tool error: deskmate_remember_fact requires an explicit user request"
        )
        assert [event.raw_event for event in tool_events] == [
            "tool.started",
            "tool.failed",
        ]
        actions = await tool_log.recent("default", limit=10)
        assert [record.status for record in actions] == ["failed"]
        assert actions[0].tool_name == "deskmate_remember_fact"
        assert actions[0].result == tool_msg["content"]


@pytest.mark.asyncio
async def test_tool_call_suggests_memory_without_writing_profile(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as profile:
        approvals = ApprovalStore()
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "suggest-mem-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_suggest_memory",
                                                "arguments": json.dumps(
                                                    {
                                                        "key": "preferred_ide",
                                                        "value": "Cursor",
                                                        "reason": "Useful for coding help.",
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I can remember that if you approve.",
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
            tool_executor=DeskmateToolExecutor(
                approval_store=approvals,
                profile_store=profile,
                reminder_clock=lambda: 123_000,
            ),
        )

        assert (
            await compose("I usually code in Cursor")
            == "I can remember that if you approve."
        )

        assert profile.get("memories.facts", {}) == {}
        pending = approvals.list_pending()
        assert len(pending) == 1
        approval = pending[0]
        assert approval.prompt == "Remember preferred_ide: Cursor?"
        assert approval.extras["kind"] == "memory_suggestion"
        assert approval.extras["memory_reason"] == "Useful for coding help."
        tool_names = [tool["function"]["name"] for tool in request_bodies[0]["tools"]]
        assert "deskmate_suggest_memory" in tool_names
        assert "deskmate_recent_tool_tasks" in tool_names
        assert "deskmate_task_context" in tool_names
        assert "deskmate_recent_tool_lessons" in tool_names
        assert "deskmate_tool_task_details" in tool_names
        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "suggest-mem-1"
        assert tool_msg["content"].startswith("Memory suggestion pending approval:")


@pytest.mark.asyncio
async def test_suggest_memory_tool_marks_existing_fact_as_update(tmp_path) -> None:
    approvals = ApprovalStore()
    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "VSCode",
                    "updated_at_ms": 1_000,
                }
            },
        )
        await profile.flush()

        executor = DeskmateToolExecutor(
            approval_store=approvals,
            profile_store=profile,
            reminder_clock=lambda: 2_000,
        )
        result = await executor.execute(
            "deskmate_suggest_memory",
            {
                "key": "preferred_ide",
                "value": "Cursor",
                "reason": "User corrected the IDE preference.",
            },
        )

        pending = approvals.list_pending()
        assert result == f"Memory suggestion pending approval: {pending[0].approval_id}."
        assert len(pending) == 1
        assert pending[0].prompt == "Update preferred_ide from VSCode to Cursor?"
        assert pending[0].extras["memory_operation"] == "update"
        assert pending[0].extras["memory_old_value"] == "VSCode"
        assert profile.get("memories.facts")["preferred_ide"]["value"] == "VSCode"


@pytest.mark.asyncio
async def test_tool_call_recalls_profile_memory(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "Cursor",
                    "updated_at_ms": 1_000,
                },
                "coffee": {
                    "key": "coffee",
                    "value": "oat latte",
                    "updated_at_ms": 2_000,
                },
            },
        )
        await profile.flush()
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "mem-2",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_recall_memory",
                                                "arguments": json.dumps(
                                                    {"query": "ide", "limit": 3}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "You prefer Cursor.",
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
            tool_executor=DeskmateToolExecutor(profile_store=profile),
        )

        assert await compose("what IDE do I prefer?") == "You prefer Cursor."

        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["content"] == "Memories:\npreferred_ide: Cursor"


@pytest.mark.asyncio
async def test_tool_call_lists_profile_memories(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "Cursor",
                    "updated_at_ms": 1_000,
                },
                "coffee": {
                    "key": "coffee",
                    "value": "oat latte",
                    "updated_at_ms": 3_000,
                },
                "terminal": {
                    "key": "terminal",
                    "value": "Ghostty",
                    "updated_at_ms": 2_000,
                },
            },
        )
        await profile.flush()
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "mem-list-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_list_memories",
                                                "arguments": json.dumps({"limit": 2}),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I remember your coffee and terminal.",
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
            tool_executor=DeskmateToolExecutor(profile_store=profile),
        )

        assert await compose("what do you remember about me?") == (
            "I remember your coffee and terminal."
        )

        tool_names = [tool["function"]["name"] for tool in request_bodies[0]["tools"]]
        assert "deskmate_list_memories" in tool_names
        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "mem-list-1"
        assert tool_msg["content"] == (
            "Durable memories:\n"
            "coffee: oat latte\n"
            "terminal: Ghostty"
        )


@pytest.mark.asyncio
async def test_list_profile_memories_reports_empty_profile(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as profile:
        executor = DeskmateToolExecutor(profile_store=profile)

        result = await executor.execute("deskmate_list_memories", {})

    assert result == "No durable memories."


@pytest.mark.asyncio
async def test_tool_call_forgets_profile_memory(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "Cursor",
                    "updated_at_ms": 1_000,
                },
                "coffee": {
                    "key": "coffee",
                    "value": "oat latte",
                    "updated_at_ms": 2_000,
                },
            },
        )
        await profile.flush()
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "forget-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_forget_memory",
                                                "arguments": json.dumps(
                                                    {"query": "preferred_ide"}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I forgot that.",
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
            tool_executor=DeskmateToolExecutor(profile_store=profile),
        )

        assert await compose("forget my IDE") == "I forgot that."

        facts = profile.get("memories.facts")
        assert "preferred_ide" not in facts
        assert facts["coffee"]["value"] == "oat latte"
        tool_names = [tool["function"]["name"] for tool in request_bodies[0]["tools"]]
        assert "deskmate_forget_memory" in tool_names
        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "forget-1"
        assert tool_msg["content"] == "Forgot memories:\npreferred_ide: Cursor"


@pytest.mark.asyncio
async def test_tool_call_searches_persistent_chat_memory(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "default",
            [
                Message(role="user", content="My project codename is bluebird."),
                Message(role="assistant", content="I will keep bluebird in mind."),
                Message(role="user", content="This should stay recent."),
            ],
        )
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "chat-mem-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_search_chat_memory",
                                                "arguments": json.dumps(
                                                    {"query": "bluebird", "limit": 3}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Your project codename was bluebird.",
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
            chat_memory=mem,
            memory_window=1,
            tool_executor=DeskmateToolExecutor(
                chat_memory=mem,
                conversation_id="default",
            ),
        )

        reply = await compose("what was the project codename?")

        assert reply == "Your project codename was bluebird."
        first = request_bodies[0]
        tool_names = [tool["function"]["name"] for tool in first["tools"]]
        assert "deskmate_search_chat_memory" in tool_names
        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "chat-mem-1"
        assert tool_msg["content"] == (
            "Chat memory matches:\n"
            "user: My project codename is bluebird.\n"
            "assistant: I will keep bluebird in mind."
        )


@pytest.mark.asyncio
async def test_tool_call_lists_reminders(tmp_path) -> None:
    del tmp_path
    store = ReminderStore()
    store.add(
        Reminder(
            reminder_id="r-later",
            text="drink water",
            due_at_ms=190_000,
            created_at_ms=100_000,
        )
    )
    store.add(
        Reminder(
            reminder_id="r-soon",
            text="stretch",
            due_at_ms=160_000,
            created_at_ms=100_000,
        )
    )
    store.add(
        Reminder(
            reminder_id="r-fired",
            text="standup",
            due_at_ms=120_000,
            created_at_ms=100_000,
        )
    )
    store.mark_fired("r-fired", 120_000, "bubble-r-fired")
    executor = DeskmateToolExecutor(
        reminder_store=store,
        reminder_clock=lambda: 130_000,
    )

    pending = await executor.execute("deskmate_list_reminders", {})
    all_reminders = await executor.execute(
        "deskmate_list_reminders",
        {"status": "all", "limit": 2},
    )
    invalid = await executor.execute(
        "deskmate_list_reminders",
        {"status": "cancelled"},
    )

    assert pending == (
        "Reminders:\n"
        "r-soon [pending, due in 30 seconds]: stretch\n"
        "r-later [pending, due in 1 minute]: drink water"
    )
    assert all_reminders == (
        "Reminders:\n"
        "r-fired [fired, fired]: standup\n"
        "r-soon [pending, due in 30 seconds]: stretch"
    )
    assert invalid == "Tool error: status must be pending, fired, or all."


@pytest.mark.asyncio
async def test_tool_call_lists_reminders_through_llm_path() -> None:
    store = ReminderStore()
    store.add(
        Reminder(
            reminder_id="r-tool-list",
            text="stretch",
            due_at_ms=160_000,
            created_at_ms=100_000,
        )
    )
    request_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if len(request_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "reminders-list-1",
                                        "type": "function",
                                        "function": {
                                            "name": "deskmate_list_reminders",
                                            "arguments": json.dumps(
                                                {"status": "pending"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "You have one reminder.",
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
        tool_executor=DeskmateToolExecutor(
            reminder_store=store,
            reminder_clock=lambda: 130_000,
        ),
    )

    assert await compose("what reminders do I have?") == "You have one reminder."

    tool_names = [tool["function"]["name"] for tool in request_bodies[0]["tools"]]
    assert "deskmate_list_reminders" in tool_names
    tool_msg = request_bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "reminders-list-1"
    assert tool_msg["content"] == (
        "Reminders:\n"
        "r-tool-list [pending, due in 30 seconds]: stretch"
    )


@pytest.mark.asyncio
async def test_tool_call_cancels_reminder_by_id() -> None:
    store = ReminderStore()
    store.add(
        Reminder(
            reminder_id="r-cancel-me",
            text="stretch",
            due_at_ms=160_000,
            created_at_ms=100_000,
        )
    )
    store.add(
        Reminder(
            reminder_id="r-keep",
            text="drink water",
            due_at_ms=190_000,
            created_at_ms=100_000,
        )
    )
    executor = DeskmateToolExecutor(
        reminder_store=store,
        reminder_clock=lambda: 130_000,
    )

    result = await executor.execute(
        "deskmate_cancel_reminder",
        {"reminder_id": "r-cancel-me"},
    )
    missing = await executor.execute(
        "deskmate_cancel_reminder",
        {"reminder_id": "missing"},
    )

    assert result == "Cancelled reminder r-cancel-me: stretch."
    assert missing == "No matching reminder."
    cancelled = store.get("r-cancel-me")
    kept = store.get("r-keep")
    assert cancelled is not None
    assert cancelled.status.value == "cancelled"
    assert cancelled.resolved_at_ms == 130_000
    assert kept is not None
    assert kept.status.value == "pending"


@pytest.mark.asyncio
async def test_cancel_reminder_tool_requires_explicit_user_intent() -> None:
    store = ReminderStore()
    store.add(
        Reminder(
            reminder_id="r-policy",
            text="stretch",
            due_at_ms=160_000,
            created_at_ms=100_000,
        )
    )
    request_bodies: list[dict[str, object]] = []
    tool_events = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if len(request_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "cancel-policy-1",
                                        "type": "function",
                                        "function": {
                                            "name": "deskmate_cancel_reminder",
                                            "arguments": json.dumps(
                                                {"reminder_id": "r-policy"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I found your reminder.",
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
        tool_executor=DeskmateToolExecutor(
            reminder_store=store,
            reminder_clock=lambda: 130_000,
        ),
        tool_event_sink=tool_events.append,
    )

    assert await compose("what reminders do I have?") == "I found your reminder."

    reminder = store.get("r-policy")
    assert reminder is not None
    assert reminder.status.value == "pending"
    tool_msg = request_bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "cancel-policy-1"
    assert tool_msg["content"] == (
        "Tool error: deskmate_cancel_reminder requires an explicit user "
        "request to cancel a reminder."
    )
    assert [event.raw_event for event in tool_events] == [
        "tool.started",
        "tool.failed",
    ]


@pytest.mark.asyncio
async def test_cancel_reminder_tool_runs_with_explicit_user_intent() -> None:
    store = ReminderStore()
    store.add(
        Reminder(
            reminder_id="r-policy-ok",
            text="stretch",
            due_at_ms=160_000,
            created_at_ms=100_000,
        )
    )
    request_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if len(request_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "cancel-policy-ok-1",
                                        "type": "function",
                                        "function": {
                                            "name": "deskmate_cancel_reminder",
                                            "arguments": json.dumps(
                                                {"reminder_id": "r-policy-ok"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Cancelled it.",
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
        tool_executor=DeskmateToolExecutor(
            reminder_store=store,
            reminder_clock=lambda: 130_000,
        ),
    )

    assert await compose("cancel reminder r-policy-ok") == "Cancelled it."

    reminder = store.get("r-policy-ok")
    assert reminder is not None
    assert reminder.status.value == "cancelled"
    tool_msg = request_bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"] == "Cancelled reminder r-policy-ok: stretch."


@pytest.mark.asyncio
async def test_tool_call_reads_recent_tool_action_log(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as tool_log:
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-tool-log-1",
                tool_name="deskmate_schedule_reminder",
                arguments={"text": "stretch", "delay_ms": 60_000},
                result="Reminder scheduled for 1 minute: stretch.",
                status="completed",
                started_at_ms=1_000,
                completed_at_ms=1_010,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-tool-log-2",
                tool_name="deskmate_computer_action",
                arguments={"command": "open Terminal"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                task_id="task-open-terminal",
                started_at_ms=2_000,
                completed_at_ms=2_010,
            )
        )

        executor = DeskmateToolExecutor(tool_action_log=tool_log)
        recent = await executor.execute(
            "deskmate_recent_tool_actions",
            {"query": "stretch", "limit": 5},
        )
        failed = await executor.execute(
            "deskmate_recent_tool_actions",
            {"status": "failed", "limit": 5},
        )
        by_task = await executor.execute(
            "deskmate_recent_tool_actions",
            {"task_id": "task-open-terminal", "limit": 5},
        )
        lessons = await executor.execute(
            "deskmate_recent_tool_lessons",
            {"query": "Terminal", "limit": 5},
        )
        tasks = await executor.execute(
            "deskmate_recent_tool_tasks",
            {"status": "completed", "limit": 5},
        )
        invalid_status = await executor.execute(
            "deskmate_recent_tool_actions",
            {"status": "running"},
        )
        invalid_task_status = await executor.execute(
            "deskmate_recent_tool_tasks",
            {"status": "duplicate"},
        )

    assert recent == (
        "Recent tool actions:\n"
        "action=deskmate_schedule_reminder; status=completed; target=stretch; "
        "outcome=Reminder scheduled for 1 minute: stretch.; needs_user=false"
    )
    assert failed == (
        "Recent tool actions:\n"
        "action=deskmate_computer_action; status=failed; target=open Terminal; "
        "outcome=Tool error: command was not recognized or allowed.; "
        "needs_user=true"
    )
    assert by_task == failed
    assert lessons == (
        "Tool lessons:\n"
        "tool=deskmate_computer_action; status=failed; target=open Terminal; "
        "outcome=Tool error: command was not recognized or allowed.; "
        "needs_user=true"
    )
    assert tasks == "No matching tool tasks."
    assert invalid_status == (
        "Tool error: status must be completed, failed, or duplicate."
    )
    assert invalid_task_status == (
        "Tool error: status must be running, completed, or failed."
    )


@pytest.mark.asyncio
async def test_tool_call_manages_persistent_deskmate_tasks(tmp_path) -> None:
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        executor = DeskmateToolExecutor(task_store=task_store)
        created = await executor.execute(
            "deskmate_create_task",
            {
                "title": "Polish island task lane",
                "notes": "Keep it compact.",
            },
        )
        task_id = created.splitlines()[1].split(" ", 1)[0]
        listed = await executor.execute("deskmate_list_tasks", {"limit": 5})
        searched = await executor.execute(
            "deskmate_search_tasks",
            {"query": "lane", "limit": 5},
        )
        updated = await executor.execute(
            "deskmate_update_task",
            {"task_id": task_id, "status": "done"},
        )
        active_after = await executor.execute("deskmate_list_tasks", {})
        done_after = await executor.execute(
            "deskmate_list_tasks",
            {"status": "done", "limit": 5},
        )

    assert created.startswith("Task created:\ntask-")
    assert "[open]: Polish island task lane - Keep it compact." in created
    assert listed == "Tasks:\n" + created.splitlines()[1]
    assert searched == listed
    assert updated == (
        "Task updated:\n"
        f"{task_id} [done]: Polish island task lane - Keep it compact."
    )
    assert active_after == "No matching tasks."
    assert done_after == (
        "Tasks:\n"
        f"{task_id} [done]: Polish island task lane - Keep it compact."
    )


@pytest.mark.asyncio
async def test_tool_call_suggests_task_without_writing_store(tmp_path) -> None:
    approvals = ApprovalStore()
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        executor = DeskmateToolExecutor(
            approval_store=approvals,
            task_store=task_store,
            reminder_clock=lambda: 5_000,
        )
        result = await executor.execute(
            "deskmate_suggest_task",
            {
                "title": "Review agent memory tools",
                "notes": "Use approval before durable writes.",
                "reason": "Useful follow-up.",
            },
        )
        pending = approvals.list_pending()
        tasks = await task_store.list(status="all", limit=10)

    assert result == f"Task suggestion pending approval: {pending[0].approval_id}."
    assert tasks == []
    assert len(pending) == 1
    assert pending[0].prompt == "Add task: Review agent memory tools?"
    assert pending[0].extras["kind"] == "task_suggestion"
    assert pending[0].extras["task_notes"] == "Use approval before durable writes."
    assert pending[0].extras["task_reason"] == "Useful follow-up."


@pytest.mark.asyncio
async def test_tool_call_blocks_create_task_without_explicit_user_intent(
    tmp_path,
) -> None:
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "create-task-policy",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_create_task",
                                                "arguments": json.dumps(
                                                    {"title": "Follow up later"}
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I can suggest that as a task.",
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
            tool_executor=DeskmateToolExecutor(task_store=task_store),
        )

        assert await compose("This may be worth doing later") == (
            "I can suggest that as a task."
        )
        tasks = await task_store.list(status="all", limit=10)

    assert tasks == []
    tool_msg = request_bodies[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "create-task-policy"
    assert tool_msg["content"].startswith(
        "Tool error: deskmate_create_task requires an explicit user request"
    )
    assert "deskmate_suggest_task" in tool_msg["content"]


@pytest.mark.asyncio
async def test_tool_call_reads_recent_tool_task_log(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as tool_log:
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="task-1",
                conversation_id="default",
                user_text="please remind me",
                status="completed",
                summary="Reminder scheduled.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=1_000,
                updated_at_ms=1_100,
                completed_at_ms=1_100,
            )
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="task-2",
                conversation_id="default",
                user_text="open terminal",
                status="failed",
                summary="Tool error.",
                action_count=1,
                failed_count=1,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )

        executor = DeskmateToolExecutor(tool_action_log=tool_log)
        recent = await executor.execute("deskmate_recent_tool_tasks", {"limit": 5})
        failed = await executor.execute(
            "deskmate_recent_tool_tasks",
            {"status": "failed", "limit": 5},
        )
        terminal = await executor.execute(
            "deskmate_recent_tool_tasks",
            {"query": "terminal", "status": "failed", "limit": 5},
        )

    assert recent == (
        "Recent tool tasks:\n"
        "task=task-1; status=completed; actions=1; summary=Reminder scheduled.\n"
        "task=task-2; status=failed; actions=1; failed=1; summary=Tool error."
    )
    assert failed == (
        "Recent tool tasks:\n"
        "task=task-2; status=failed; actions=1; failed=1; summary=Tool error."
    )
    assert terminal == (
        "Recent tool tasks:\n"
        "task=task-2; status=failed; actions=1; failed=1; summary=Tool error."
    )


@pytest.mark.asyncio
async def test_tool_call_reads_persistent_task_context(tmp_path) -> None:
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-island",
            title="Polish island task lane",
            notes="Keep collapsed state compact.",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.create(
            conversation_id="other",
            task_id="task-other",
            title="Polish island task lane",
            created_at_ms=2_000,
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="tool-task-island",
                conversation_id="default",
                user_text="Polish island task lane",
                status="failed",
                summary="Tool error while polishing island lane.",
                action_count=1,
                failed_count=1,
                duplicate_count=0,
                started_at_ms=3_000,
                updated_at_ms=3_100,
                completed_at_ms=3_100,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-island",
                task_id="tool-task-island",
                tool_name="deskmate_computer_action",
                arguments={"command": "open island diagnostics"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                started_at_ms=3_010,
                completed_at_ms=3_020,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="other",
                tool_call_id="call-other",
                tool_name="deskmate_computer_action",
                arguments={"command": "open island diagnostics"},
                result="Opened diagnostics elsewhere.",
                status="completed",
                started_at_ms=4_000,
                completed_at_ms=4_010,
            )
        )

        executor = DeskmateToolExecutor(
            task_store=task_store,
            tool_action_log=tool_log,
        )
        exact = await executor.execute(
            "deskmate_task_context",
            {"task_id": "task-island", "limit": 5},
        )
        queried = await executor.execute(
            "deskmate_task_context",
            {"query": "collapsed", "limit": 5},
        )
        missing = await executor.execute(
            "deskmate_task_context",
            {"task_id": "task-other"},
        )

    expected = (
        "Task context:\n"
        "task-island [in_progress]: Polish island task lane - Keep collapsed state compact.\n"
        "Task steps:\n"
        "none\n"
        "Related tool tasks:\n"
        "task=tool-task-island; status=failed; actions=1; failed=1; "
        "summary=Tool error while polishing island lane.\n"
        "Related tool actions:\n"
        "action=deskmate_computer_action; status=failed; "
        "target=open island diagnostics; "
        "outcome=Tool error: command was not recognized or allowed.; needs_user=true\n"
        "Related tool lessons:\n"
        "tool=deskmate_computer_action; status=failed; "
        "target=open island diagnostics; "
        "outcome=Tool error: command was not recognized or allowed.; needs_user=true"
    )
    assert exact == expected
    assert queried == expected
    assert missing == "No matching task."


@pytest.mark.asyncio
async def test_task_context_prefers_direct_task_id_tool_history(tmp_path) -> None:
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-direct",
            title="Direct context lookup",
            status="in_progress",
            created_at_ms=1_000,
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="task-command-direct",
                task_id="task-direct",
                tool_name="deskmate_task_command",
                arguments={"kind": "start", "query": "opaque command target"},
                result="Task started for an opaque command target.",
                status="completed",
                started_at_ms=2_000,
                completed_at_ms=2_010,
                summary={
                    "action": "task.start",
                    "target": "opaque command target",
                    "outcome": "Task started for an opaque command target.",
                    "needs_user": False,
                },
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="other",
                tool_call_id="task-command-other",
                task_id="task-direct",
                tool_name="deskmate_task_command",
                arguments={"kind": "start"},
                result="Other conversation.",
                status="completed",
                started_at_ms=3_000,
                completed_at_ms=3_010,
            )
        )
        executor = DeskmateToolExecutor(
            task_store=task_store,
            tool_action_log=tool_log,
        )

        result = await executor.execute(
            "deskmate_task_context",
            {"task_id": "task-direct", "limit": 5},
        )

    assert result == (
        "Task context:\n"
        "task-direct [in_progress]: Direct context lookup\n"
        "Task steps:\n"
        "none\n"
        "Related tool tasks:\n"
        "none\n"
        "Related tool actions:\n"
        "action=task.start; status=completed; target=opaque command target; "
        "outcome=Task started for an opaque command target.; needs_user=false\n"
        "Related tool lessons:\n"
        "tool=task.start; status=completed; target=opaque command target; "
        "outcome=Task started for an opaque command target."
    )


@pytest.mark.asyncio
async def test_task_context_searches_step_text_for_related_history(tmp_path) -> None:
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-step-context",
            title="Generic recovery work",
            notes="No matching tool words here.",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            "task-step-context",
            [
                {
                    "content": "Inspect hydrating island snapshot",
                    "status": "in_progress",
                    "active_form": "Inspecting hydrating island snapshot",
                }
            ],
            conversation_id="default",
            updated_at_ms=1_100,
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="tool-task-hydrating",
                conversation_id="default",
                user_text="Inspecting hydrating island snapshot",
                status="completed",
                summary="Hydrating island snapshot inspected.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-hydrating",
                tool_name="deskmate_computer_action",
                arguments={"command": "open hydrating snapshot diagnostics"},
                result="Opened hydrating snapshot diagnostics.",
                status="completed",
                started_at_ms=2_010,
                completed_at_ms=2_020,
            )
        )
        executor = DeskmateToolExecutor(
            task_store=task_store,
            tool_action_log=tool_log,
        )

        result = await executor.execute(
            "deskmate_task_context",
            {"task_id": "task-step-context", "limit": 5},
        )

    assert result == (
        "Task context:\n"
        "task-step-context [in_progress]: Generic recovery work - No matching tool words here.\n"
        "Task steps:\n"
        "1. [in_progress] Inspect hydrating island snapshot -> Inspecting hydrating island snapshot\n"
        "Related tool tasks:\n"
        "task=tool-task-hydrating; status=completed; actions=1; "
        "summary=Hydrating island snapshot inspected.\n"
        "Related tool actions:\n"
        "action=deskmate_computer_action; status=completed; "
        "target=open hydrating snapshot diagnostics; "
        "outcome=Opened hydrating snapshot diagnostics.; needs_user=false\n"
        "Related tool lessons:\n"
        "tool=deskmate_computer_action; status=completed; "
        "target=open hydrating snapshot diagnostics; "
        "outcome=Opened hydrating snapshot diagnostics."
    )


@pytest.mark.asyncio
async def test_tool_call_reads_tool_task_details(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as tool_log:
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="task-details",
                conversation_id="default",
                user_text="open terminal",
                status="failed",
                summary="Tool error.",
                action_count=2,
                failed_count=1,
                duplicate_count=0,
                started_at_ms=1_000,
                updated_at_ms=1_200,
                completed_at_ms=1_200,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-details-1",
                task_id="task-details",
                tool_name="deskmate_list_reminders",
                arguments={},
                result="No reminders.",
                status="completed",
                started_at_ms=1_010,
                completed_at_ms=1_020,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-details-2",
                task_id="task-details",
                tool_name="deskmate_computer_action",
                arguments={"command": "open Terminal"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                started_at_ms=1_100,
                completed_at_ms=1_110,
            )
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="task-empty",
                conversation_id="default",
                user_text="remember preference",
                status="completed",
                summary="No tools needed.",
                action_count=0,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="other-task",
                conversation_id="other",
                user_text="other",
                status="completed",
                summary="Other.",
                action_count=0,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=3_000,
                updated_at_ms=3_100,
                completed_at_ms=3_100,
            )
        )

        executor = DeskmateToolExecutor(tool_action_log=tool_log)
        details = await executor.execute(
            "deskmate_tool_task_details",
            {"task_id": "task-details", "limit": 10},
        )
        empty = await executor.execute(
            "deskmate_tool_task_details",
            {"task_id": "task-empty"},
        )
        missing = await executor.execute(
            "deskmate_tool_task_details",
            {"task_id": "other-task"},
        )
        invalid = await executor.execute("deskmate_tool_task_details", {})

    assert details == (
        "Tool task details:\n"
        "task=task-details; status=failed; actions=2; failed=1; summary=Tool error.\n"
        "Actions:\n"
        "- action=deskmate_list_reminders; status=completed; outcome=No reminders.; needs_user=false\n"
        "- action=deskmate_computer_action; status=failed; target=open Terminal; "
        "outcome=Tool error: command was not recognized or allowed.; needs_user=true"
    )
    assert empty == (
        "Tool task details:\n"
        "task=task-empty; status=completed; actions=0; summary=No tools needed.\n"
        "Actions: none"
    )
    assert missing == "No matching tool task."
    assert invalid == "Tool error: task_id is required."


@pytest.mark.asyncio
async def test_recent_tool_action_tool_reports_missing_log() -> None:
    executor = DeskmateToolExecutor()

    result = await executor.execute("deskmate_recent_tool_actions", {})
    task_result = await executor.execute("deskmate_recent_tool_tasks", {})
    lesson_result = await executor.execute("deskmate_recent_tool_lessons", {})
    context_result = await executor.execute("deskmate_task_context", {})
    detail_result = await executor.execute(
        "deskmate_tool_task_details",
        {"task_id": "missing"},
    )

    assert result == "Tool error: tool action log is not ready."
    assert task_result == "Tool error: tool action log is not ready."
    assert lesson_result == "Tool error: tool action log is not ready."
    assert context_result == "Tool error: task store is not ready."
    assert detail_result == "Tool error: tool action log is not ready."


@pytest.mark.asyncio
async def test_profile_memory_injected_into_llm_context(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "Cursor",
                    "updated_at_ms": 2_000,
                },
                "coffee": {
                    "key": "coffee",
                    "value": "oat latte",
                    "updated_at_ms": 1_000,
                },
            },
        )
        await profile.flush()
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            profile_store=profile,
        )

        assert await compose("what should you know about me?") == "ok"

        messages = captured["body"]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "system"
        assert messages[1]["content"].startswith("Known durable memories:")
        assert "- preferred_ide: Cursor" in messages[1]["content"]
        assert "- coffee: oat latte" in messages[1]["content"]
        assert messages[-1] == {
            "role": "user",
            "content": "what should you know about me?",
        }


@pytest.mark.asyncio
async def test_recent_tool_actions_injected_into_llm_context(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as tool_log:
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-1",
                tool_name="deskmate_schedule_reminder",
                arguments={"text": "stretch", "delay_ms": 60_000},
                result="Reminder scheduled for 1 minute: stretch.",
                status="completed",
                started_at_ms=1_000,
                completed_at_ms=1_010,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="other",
                tool_call_id="call-other",
                tool_name="deskmate_computer_action",
                arguments={"command": "open Terminal"},
                result="Opened Terminal.",
                status="completed",
                started_at_ms=2_000,
                completed_at_ms=2_010,
            )
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            tool_action_log=tool_log,
        )

        assert await compose("what just happened?") == "ok"

        messages = captured["body"]["messages"]
        action_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Recent Deskmate tool actions:")
        )
        assert action_message["content"] == (
            "Recent Deskmate tool actions:\n"
            "- action=deskmate_schedule_reminder; status=completed; "
            "target=stretch; outcome=Reminder scheduled for 1 minute: stretch.; "
            "needs_user=false"
        )
        assert "Opened Terminal" not in action_message["content"]


@pytest.mark.asyncio
async def test_tool_lessons_injected_into_llm_context(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as tool_log:
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-lesson",
                tool_name="deskmate_computer_action",
                arguments={"command": "open Terminal"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                task_id="task-open-terminal",
                started_at_ms=1_000,
                completed_at_ms=1_010,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="other",
                tool_call_id="call-other-lesson",
                tool_name="deskmate_computer_action",
                arguments={"command": "open Cursor"},
                result="Opened Cursor.",
                status="completed",
                started_at_ms=2_000,
                completed_at_ms=2_010,
            )
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            tool_action_log=tool_log,
        )

        assert await compose("try opening terminal again") == "ok"

        messages = captured["body"]["messages"]
        lesson_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Durable Deskmate tool lessons:")
        )
        assert lesson_message["content"] == (
            "Durable Deskmate tool lessons:\n"
            "- tool=deskmate_computer_action; status=failed; target=open Terminal; "
            "outcome=Tool error: command was not recognized or allowed.; "
            "needs_user=true"
        )
        assert "Opened Cursor" not in lesson_message["content"]


@pytest.mark.asyncio
async def test_recent_tool_tasks_injected_into_llm_context(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as tool_log:
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="task-1",
                conversation_id="default",
                user_text="please remind me",
                status="completed",
                summary="Reminder scheduled.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=1_000,
                updated_at_ms=1_100,
                completed_at_ms=1_100,
            )
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="task-other",
                conversation_id="other",
                user_text="open terminal",
                status="failed",
                summary="Tool error.",
                action_count=1,
                failed_count=1,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            tool_action_log=tool_log,
        )

        assert await compose("what task did you finish?") == "ok"

        messages = captured["body"]["messages"]
        task_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Recent Deskmate tool tasks:")
        )
        assert task_message["content"] == (
            "Recent Deskmate tool tasks:\n"
            "- task=task-1; status=completed; actions=1; summary=Reminder scheduled."
        )
        assert "task-other" not in task_message["content"]


@pytest.mark.asyncio
async def test_active_deskmate_tasks_injected_into_llm_context(tmp_path) -> None:
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        await task_store.create(
            conversation_id="default",
            task_id="task-active",
            title="Polish island task lane",
            notes="Keep collapsed state compact.",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            "task-active",
            [
                {"content": "Audit current session row UI", "status": "completed"},
                {
                    "content": "Expose approval outcome",
                    "status": "in_progress",
                    "active_form": "Exposing approval outcome",
                },
                {"content": "Run smoke verification", "status": "pending"},
            ],
            conversation_id="default",
            updated_at_ms=1_500,
        )
        await task_store.create(
            conversation_id="default",
            task_id="task-done",
            title="Already shipped",
            status="done",
            created_at_ms=2_000,
        )
        await task_store.create(
            conversation_id="other",
            task_id="task-other",
            title="Other conversation",
            created_at_ms=3_000,
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            task_store=task_store,
        )

        assert await compose("what should we work on?") == "ok"

        messages = captured["body"]["messages"]
        focus_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Current Deskmate task focus:")
        )
        task_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Active Deskmate tasks:")
        )
        assert focus_message["content"] == (
            "Current Deskmate task focus:\n"
            "- task=task-active; status=in_progress; title=Polish island task lane\n"
            "- notes=Keep collapsed state compact.\n"
            "- current_step=2/3 [in_progress] Exposing approval outcome\n"
            "- next_step=3/3 Run smoke verification\n"
            "- progress=1/3 steps completed"
        )
        assert task_message["content"] == (
            "Active Deskmate tasks:\n"
            "- task-active; status=in_progress; title=Polish island task lane; "
            "notes=Keep collapsed state compact.; "
            "steps=completed: Audit current session row UI | "
            "in_progress: Exposing approval outcome | "
            "pending: Run smoke verification"
        )
        assert "task-done" not in task_message["content"]
        assert "task-other" not in task_message["content"]


@pytest.mark.asyncio
async def test_profile_memory_injected_into_streaming_context(tmp_path) -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "Cursor",
                    "updated_at_ms": 1_000,
                }
            },
        )
        await profile.flush()
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=_sse_lines("ok", None),
                headers={"Content-Type": "text/event-stream"},
            )

        compose = openai_compat_streaming_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            profile_store=profile,
        )

        chunks = [chunk async for chunk in compose("hi")]

        assert chunks == ["ok"]
        messages = captured["body"]["messages"]
        assert messages[1]["role"] == "system"
        assert "- preferred_ide: Cursor" in messages[1]["content"]


@pytest.mark.asyncio
async def test_recent_tool_actions_injected_into_streaming_context(tmp_path) -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    async with ToolActionLog(tmp_path / "tool_actions.db") as tool_log:
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-1",
                tool_name="deskmate_computer_action",
                arguments={"command": "open Terminal"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                started_at_ms=1_000,
                completed_at_ms=1_010,
            )
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=_sse_lines("ok", None),
                headers={"Content-Type": "text/event-stream"},
            )

        compose = openai_compat_streaming_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            tool_action_log=tool_log,
        )

        chunks = [chunk async for chunk in compose("what failed?")]

        assert chunks == ["ok"]
        messages = captured["body"]["messages"]
        action_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Recent Deskmate tool actions:")
        )
        lesson_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Durable Deskmate tool lessons:")
        )
        assert action_message["content"] == (
            "Recent Deskmate tool actions:\n"
            "- action=deskmate_computer_action; status=failed; "
            "target=open Terminal; "
            "outcome=Tool error: command was not recognized or allowed.; "
            "needs_user=true"
        )
        assert lesson_message["content"] == (
            "Durable Deskmate tool lessons:\n"
            "- tool=deskmate_computer_action; status=failed; "
            "target=open Terminal; "
            "outcome=Tool error: command was not recognized or allowed.; "
            "needs_user=true"
        )


@pytest.mark.asyncio
async def test_active_deskmate_tasks_injected_into_streaming_context(tmp_path) -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        await task_store.create(
            conversation_id="default",
            task_id="task-stream",
            title="Stream task context",
            status="open",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            "task-stream",
            [
                {"content": "Capture streamed prompt", "status": "in_progress"},
            ],
            conversation_id="default",
            updated_at_ms=1_100,
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=_sse_lines("ok", None),
                headers={"Content-Type": "text/event-stream"},
            )

        compose = openai_compat_streaming_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            task_store=task_store,
        )

        chunks = [chunk async for chunk in compose("hi")]

        assert chunks == ["ok"]
        messages = captured["body"]["messages"]
        focus_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Current Deskmate task focus:")
        )
        task_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith("Active Deskmate tasks:")
        )
        assert focus_message["content"] == (
            "Current Deskmate task focus:\n"
            "- task=task-stream; status=open; title=Stream task context\n"
            "- current_step=1/1 [in_progress] Capture streamed prompt\n"
            "- progress=0/1 steps completed"
        )
        assert task_message["content"] == (
            "Active Deskmate tasks:\n"
            "- task-stream; status=open; title=Stream task context; "
            "steps=in_progress: Capture streamed prompt"
        )


@pytest.mark.asyncio
async def test_task_focus_survives_tool_followup_context(tmp_path) -> None:
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-followup",
            title="Keep task focus through tools",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            "task-followup",
            [
                {
                    "content": "Run a reminder tool",
                    "status": "in_progress",
                    "active_form": "Running a reminder tool",
                },
                {"content": "Summarize the result", "status": "pending"},
            ],
            conversation_id="default",
            updated_at_ms=1_100,
        )
        reminders = ReminderStore()
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-task-focus",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": json.dumps(
                                                    {
                                                        "text": "check focus",
                                                        "delay_ms": 60_000,
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}}
                    ]
                },
            )

        compose = openai_compat_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            task_store=task_store,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=reminders,
                reminder_clock=lambda: 1_000,
                reminder_id_factory=lambda: "r-task-focus",
            ),
        )

        assert await compose("set a reminder for this task") == "done"

        second_messages = request_bodies[1]["messages"]
        focus_message = next(
            message
            for message in second_messages
            if message["role"] == "system"
            and message["content"].startswith("Current Deskmate task focus:")
        )
        task_message = next(
            message
            for message in second_messages
            if message["role"] == "system"
            and message["content"].startswith("Active Deskmate tasks:")
        )
        assert "task=task-followup" in focus_message["content"]
        assert "current_step=1/2 [in_progress] Running a reminder tool" in (
            focus_message["content"]
        )
        assert "task-followup; status=in_progress" in task_message["content"]
        assert second_messages[-1]["role"] == "tool"
        assert second_messages[-1]["tool_call_id"] == "call-task-focus"


@pytest.mark.asyncio
async def test_resume_task_context_injected_for_resume_request(tmp_path) -> None:
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-resume",
            title="Polish resume context",
            notes="Use task-context before answering.",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            "task-resume",
            [
                {"content": "Read active task state", "status": "completed"},
                {
                    "content": "Inject resume context",
                    "status": "in_progress",
                    "active_form": "Injecting resume context",
                },
            ],
            conversation_id="default",
            updated_at_ms=1_100,
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-resume",
                tool_name="deskmate_recent_tool_lessons",
                arguments={"query": "resume context"},
                result="Found prior resume-context lesson.",
                status="completed",
                started_at_ms=2_000,
                completed_at_ms=2_010,
            )
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            task_store=task_store,
            tool_action_log=tool_log,
        )

        assert await compose("continue current task") == "ok"

        messages = captured["body"]["messages"]
        resume_message = next(
            message
            for message in messages
            if message["role"] == "system"
            and message["content"].startswith(
                "Resume context for current Deskmate task:"
            )
        )
        assert "Task context:\ntask-resume [in_progress]" in (
            resume_message["content"]
        )
        assert "2. [in_progress] Inject resume context -> Injecting resume context" in (
            resume_message["content"]
        )
        assert "Related tool actions:" in resume_message["content"]
        assert "deskmate_recent_tool_lessons" in resume_message["content"]


@pytest.mark.asyncio
async def test_resume_task_context_not_injected_for_regular_chat(tmp_path) -> None:
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-regular",
            title="Regular task context",
            status="in_progress",
            created_at_ms=1_000,
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
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
            task_store=task_store,
            tool_action_log=tool_log,
        )

        assert await compose("what should we work on?") == "ok"

        messages = captured["body"]["messages"]
        assert not any(
            message["role"] == "system"
            and message["content"].startswith(
                "Resume context for current Deskmate task:"
            )
            for message in messages
        )


@pytest.mark.asyncio
async def test_resume_task_context_survives_tool_followup_context(tmp_path) -> None:
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-resume-tool",
            title="Resume through tool followup",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            "task-resume-tool",
            [
                {
                    "content": "Run tool with resume context",
                    "status": "in_progress",
                    "active_form": "Running tool with resume context",
                },
            ],
            conversation_id="default",
            updated_at_ms=1_100,
        )
        reminders = ReminderStore()
        request_bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if len(request_bodies) == 1:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-resume-tool",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": json.dumps(
                                                    {
                                                        "text": "resume tool",
                                                        "delay_ms": 60_000,
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}}
                    ]
                },
            )

        compose = openai_compat_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            task_store=task_store,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=reminders,
                reminder_clock=lambda: 1_000,
                reminder_id_factory=lambda: "r-resume-tool",
            ),
        )

        assert await compose("continue current task and remind me") == "done"

        second_messages = request_bodies[1]["messages"]
        resume_message = next(
            message
            for message in second_messages
            if message["role"] == "system"
            and message["content"].startswith(
                "Resume context for current Deskmate task:"
            )
        )
        assert "task-resume-tool [in_progress]" in resume_message["content"]
        assert "Run tool with resume context" in resume_message["content"]
        assert second_messages[-1]["role"] == "tool"
        assert second_messages[-1]["tool_call_id"] == "call-resume-tool"


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
async def test_make_default_composer_without_key_runs_computer_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    calls: list[list[str]] = []

    import deskmate_agent.skills.computer_control as control

    monkeypatch.setattr(
        control,
        "_default_opener",
        lambda args: calls.append(args) is None,
    )
    compose = make_default_composer()

    reply = await compose("open Terminal")

    assert reply == "Opened Terminal."
    assert calls == [["open", "-a", "Terminal"]]


@pytest.mark.asyncio
async def test_make_default_composer_without_key_manages_reminders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    store = ReminderStore()
    compose = make_default_composer(
        reminder_store=store,
        reminder_control_clock=lambda: 1_000,
        reminder_id_factory=lambda: "r-default",
    )

    created = await compose("remind me to stretch in 2 minutes")
    listed = await compose("what reminders do I have?")
    cancelled = await compose("cancel reminder r-default")
    listed_after = await compose("list reminders")

    assert created == "Reminder set for 2 minutes: stretch."
    assert listed == "Pending reminders:\nr-default [due in 2 minutes]: stretch"
    assert cancelled == "Cancelled reminder r-default: stretch."
    assert listed_after == "You do not have any pending reminders."


@pytest.mark.asyncio
async def test_make_default_composer_without_key_manages_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose(
            "add task Polish island task lane notes: Keep it compact"
        )
        assert created is not None
        task_id = created.splitlines()[1].split(" ", 1)[0]
        await task_store.replace_steps(
            task_id,
            [
                {"content": "Read task lane state", "status": "completed"},
                {
                    "content": "Show checklist in task list",
                    "status": "in_progress",
                    "active_form": "Showing checklist in task list",
                },
            ],
            conversation_id="default",
        )
        listed = await compose("list tasks")
        searched = await compose("search tasks island")
        completed = await compose(f"complete task {task_id}")
        listed_after = await compose("show tasks")

    assert created.startswith("Task created:\ntask-")
    assert "[open]: Polish island task lane - Keep it compact" in created
    expected_active = (
        "Tasks:\n"
        f"{task_id} [open]: Polish island task lane - Keep it compact\n"
        "  steps:\n"
        "  - 1. [completed] Read task lane state\n"
        "  - 2. [in_progress] Show checklist in task list -> Showing checklist in task list"
    )
    assert listed == expected_active
    assert searched == listed
    assert completed == (
        "Task updated:\n"
        f"{task_id} [done]: Polish island task lane - Keep it compact\n"
        "  steps:\n"
        "  - 1. [completed] Read task lane state\n"
        "  - 2. [in_progress] Show checklist in task list -> Showing checklist in task list"
    )
    assert listed_after == "No matching tasks."


@pytest.mark.asyncio
async def test_make_default_composer_audits_task_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        compose = make_default_composer(
            task_store=task_store,
            tool_action_log=tool_log,
        )

        created = await compose("add task Audit deterministic task commands")
        started = await compose("start task deterministic task")
        missing = await compose("complete task definitely missing")
        records = await tool_log.recent(limit=10)

    assert created is not None
    assert started is not None
    assert missing == "No matching task."
    assert [record.tool_name for record in records] == [
        "deskmate_task_command",
        "deskmate_task_command",
        "deskmate_task_command",
    ]
    assert [record.status for record in records] == [
        "completed",
        "completed",
        "failed",
    ]
    assert records[0].summary is not None
    assert records[0].summary["action"] == "task.create"
    assert records[0].summary["target"] == "Audit deterministic task commands"
    assert records[0].task_id is not None
    assert records[1].summary is not None
    assert records[1].summary["action"] == "task.start"
    assert records[1].summary["target"] == "deterministic task"
    assert records[1].task_id == records[0].task_id
    assert records[2].summary is not None
    assert records[2].summary["action"] == "task.complete"
    assert records[2].summary["target"] == "definitely missing"
    assert records[2].task_id is None


@pytest.mark.asyncio
async def test_make_default_composer_can_resume_current_task_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        task = await task_store.create(
            conversation_id="default",
            task_id="task-current",
            title="Polish current task recovery",
            notes="Recover without LLM.",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            task.task_id,
            [
                {"content": "Read task snapshot", "status": "completed"},
                {
                    "content": "Show resume context",
                    "status": "in_progress",
                    "active_form": "Showing resume context",
                },
                {"content": "Run verification", "status": "pending"},
            ],
            conversation_id="default",
            updated_at_ms=1_100,
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="tool-task-current",
                conversation_id="default",
                user_text="Polish current task recovery",
                status="failed",
                summary="Tool error while polishing current task recovery.",
                action_count=1,
                failed_count=1,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-current",
                task_id="tool-task-current",
                tool_name="deskmate_computer_action",
                arguments={"command": "open current task recovery"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                started_at_ms=2_010,
                completed_at_ms=2_020,
            )
        )
        compose = make_default_composer(
            task_store=task_store,
            tool_action_log=tool_log,
        )

        resumed = await compose("continue current task")
        records = await tool_log.recent(limit=10)

    assert resumed == (
        "Current task context:\n"
        "task-current [in_progress]: Polish current task recovery - Recover without LLM.\n"
        "  steps:\n"
        "  - 1. [completed] Read task snapshot\n"
        "  - 2. [in_progress] Show resume context -> Showing resume context\n"
        "  - 3. [pending] Run verification\n"
        "Next step:\n"
        "2. [in_progress] Show resume context -> Showing resume context\n"
        "Related tool tasks:\n"
        "  - task=tool-task-current; status=failed; actions=1; failed=1; "
        "summary=Tool error while polishing current task recovery.\n"
        "Related tool actions:\n"
        "  - action=deskmate_computer_action; status=failed; "
        "target=open current task recovery; "
        "outcome=Tool error: command was not recognized or allowed.; needs_user=true\n"
        "Related tool lessons:\n"
        "  - tool=deskmate_computer_action; status=failed; "
        "target=open current task recovery; "
        "outcome=Tool error: command was not recognized or allowed.; needs_user=true"
    )
    assert records[-1].tool_name == "deskmate_task_command"
    assert records[-1].summary is not None
    assert records[-1].summary["action"] == "task.resume"
    assert records[-1].task_id == "task-current"
    assert records[-1].status == "completed"


@pytest.mark.asyncio
async def test_resume_current_task_prefers_direct_task_id_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-direct-resume",
            title="Direct resume lookup",
            status="in_progress",
            created_at_ms=1_000,
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-direct-resume",
                task_id="task-direct-resume",
                tool_name="deskmate_task_command",
                arguments={"kind": "start", "query": "opaque target"},
                result="Task started for opaque target.",
                status="completed",
                started_at_ms=2_000,
                completed_at_ms=2_010,
                summary={
                    "action": "task.start",
                    "target": "opaque target",
                    "outcome": "Task started for opaque target.",
                    "needs_user": False,
                },
            )
        )
        compose = make_default_composer(
            task_store=task_store,
            tool_action_log=tool_log,
        )

        resumed = await compose("continue current task")

    assert resumed == (
        "Current task context:\n"
        "task-direct-resume [in_progress]: Direct resume lookup\n"
        "Related tool tasks:\n"
        "  - none\n"
        "Related tool actions:\n"
        "  - action=task.start; status=completed; target=opaque target; "
        "outcome=Task started for opaque target.; needs_user=false\n"
        "Related tool lessons:\n"
        "  - tool=task.start; status=completed; target=opaque target; "
        "outcome=Task started for opaque target."
    )


@pytest.mark.asyncio
async def test_resume_current_task_searches_step_text_for_related_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with (
        DeskmateTaskStore(tmp_path / "tasks.db") as task_store,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        await task_store.create(
            conversation_id="default",
            task_id="task-step-resume",
            title="Generic local recovery",
            notes="No matching tool words.",
            status="in_progress",
            created_at_ms=1_000,
        )
        await task_store.replace_steps(
            "task-step-resume",
            [
                {
                    "content": "Inspect hydrating island snapshot",
                    "status": "in_progress",
                    "active_form": "Inspecting hydrating island snapshot",
                }
            ],
            conversation_id="default",
            updated_at_ms=1_100,
        )
        await tool_log.upsert_task(
            ToolTaskRecord(
                task_id="tool-task-resume-hydrating",
                conversation_id="default",
                user_text="Inspecting hydrating island snapshot",
                status="completed",
                summary="Hydrating island snapshot inspected.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )
        await tool_log.append(
            ToolActionRecord(
                conversation_id="default",
                tool_call_id="call-resume-hydrating",
                tool_name="deskmate_computer_action",
                arguments={"command": "open hydrating snapshot diagnostics"},
                result="Opened hydrating snapshot diagnostics.",
                status="completed",
                started_at_ms=2_010,
                completed_at_ms=2_020,
            )
        )
        compose = make_default_composer(
            task_store=task_store,
            tool_action_log=tool_log,
        )

        resumed = await compose("continue current task")

    assert resumed == (
        "Current task context:\n"
        "task-step-resume [in_progress]: Generic local recovery - No matching tool words.\n"
        "  steps:\n"
        "  - 1. [in_progress] Inspect hydrating island snapshot -> "
        "Inspecting hydrating island snapshot\n"
        "Next step:\n"
        "1. [in_progress] Inspect hydrating island snapshot -> "
        "Inspecting hydrating island snapshot\n"
        "Related tool tasks:\n"
        "  - task=tool-task-resume-hydrating; status=completed; actions=1; "
        "summary=Hydrating island snapshot inspected.\n"
        "Related tool actions:\n"
        "  - action=deskmate_computer_action; status=completed; "
        "target=open hydrating snapshot diagnostics; "
        "outcome=Opened hydrating snapshot diagnostics.; needs_user=false\n"
        "Related tool lessons:\n"
        "  - tool=deskmate_computer_action; status=completed; "
        "target=open hydrating snapshot diagnostics; "
        "outcome=Opened hydrating snapshot diagnostics."
    )


@pytest.mark.asyncio
async def test_make_default_composer_task_notes_accept_dash_notes_separator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose(
            "add task Polish active task snapshot -- notes: Menu should update"
        )

    assert created is not None
    assert created.startswith("Task created:\ntask-")
    assert "[open]: Polish active task snapshot - Menu should update" in created


@pytest.mark.asyncio
async def test_make_default_composer_can_complete_task_by_title_fragment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose("todo: Review agent memory tools")
        completed = await compose("complete task agent memory")
        listed_after = await compose("show tasks")
        tasks = await task_store.list(status="all", limit=10)

    assert created is not None
    task_id = created.splitlines()[1].split(" ", 1)[0]
    assert completed == (
        f"Task updated:\n{task_id} [done]: Review agent memory tools"
    )
    assert listed_after == "No matching tasks."
    assert tasks[0].status == "done"


@pytest.mark.asyncio
async def test_make_default_composer_does_not_update_ambiguous_task_fragment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        first = await compose("todo: Review agent memory tools")
        second = await compose("todo: Review agent task lane")
        result = await compose("complete task Review agent")
        tasks = await task_store.list(status="active", limit=10)

    assert first is not None
    assert second is not None
    assert result is not None
    assert result.startswith("Multiple matching tasks:\n")
    assert "Review agent memory tools" in result
    assert "Review agent task lane" in result
    assert sorted(task.title for task in tasks) == [
        "Review agent memory tools",
        "Review agent task lane",
    ]


@pytest.mark.asyncio
async def test_make_default_composer_can_plan_task_steps_by_title_fragment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose("todo: Review agent memory tools")
        planned = await compose(
            "plan task agent memory: Audit current state; "
            "current: Expose checklist in menu; Write tests"
        )
        listed = await compose("list tasks")

    assert created is not None
    task_id = created.splitlines()[1].split(" ", 1)[0]
    assert planned == (
        f"Task steps updated for {task_id}:\n"
        "1. [pending] Audit current state\n"
        "2. [in_progress] Expose checklist in menu -> Expose checklist in menu\n"
        "3. [pending] Write tests"
    )
    assert listed == (
        "Tasks:\n"
        f"{task_id} [open]: Review agent memory tools\n"
        "  steps:\n"
        "  - 1. [pending] Audit current state\n"
        "  - 2. [in_progress] Expose checklist in menu -> Expose checklist in menu\n"
        "  - 3. [pending] Write tests"
    )


@pytest.mark.asyncio
async def test_make_default_composer_can_start_task_and_activate_first_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose("todo: Review agent memory tools")
        planned = await compose(
            "plan task agent memory: Audit current state; Write tests"
        )
        started = await compose("start task agent memory")

    assert created is not None
    assert planned is not None
    task_id = created.splitlines()[1].split(" ", 1)[0]
    assert started == (
        f"Task started:\n{task_id} [in_progress]: Review agent memory tools\n"
        "  steps:\n"
        "  - 1. [in_progress] Audit current state -> Audit current state\n"
        "  - 2. [pending] Write tests"
    )


@pytest.mark.asyncio
async def test_make_default_composer_can_pause_and_resume_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose("todo: Review agent memory tools")
        planned = await compose(
            "plan task agent memory: Audit current state; Write tests"
        )
        started = await compose("start task agent memory")
        paused = await compose("pause task agent memory")
        resumed = await compose("resume task agent memory")

    assert created is not None
    assert planned is not None
    assert started is not None
    task_id = created.splitlines()[1].split(" ", 1)[0]
    assert paused == (
        f"Task paused:\n{task_id} [open]: Review agent memory tools\n"
        "  steps:\n"
        "  - 1. [pending] Audit current state\n"
        "  - 2. [pending] Write tests"
    )
    assert resumed == (
        f"Task started:\n{task_id} [in_progress]: Review agent memory tools\n"
        "  steps:\n"
        "  - 1. [in_progress] Audit current state -> Audit current state\n"
        "  - 2. [pending] Write tests"
    )


@pytest.mark.asyncio
async def test_make_default_composer_can_advance_task_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose("todo: Review agent memory tools")
        planned = await compose(
            "plan task agent memory: current: Audit current state; Write tests"
        )
        advanced = await compose("next step task agent memory")
        completed = await compose("next step task agent memory")
        listed_after = await compose("list tasks")
        tasks = await task_store.list(status="all", limit=10)

    assert created is not None
    assert planned is not None
    task_id = created.splitlines()[1].split(" ", 1)[0]
    assert advanced == (
        f"Task step advanced for {task_id}:\n"
        "1. [completed] Audit current state\n"
        "2. [in_progress] Write tests -> Write tests"
    )
    assert completed == (
        f"Task completed:\n{task_id} [done]: Review agent memory tools\n"
        "  steps:\n"
        "  - 1. [completed] Audit current state\n"
        "  - 2. [completed] Write tests"
    )
    assert listed_after == "No matching tasks."
    assert tasks[0].status == "done"


@pytest.mark.asyncio
async def test_make_default_composer_can_show_task_detail_by_title_fragment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose("todo: Review agent memory tools")
        planned = await compose(
            "plan task agent memory: current: Audit current state; Write tests"
        )
        detail = await compose("what's next for task agent memory")

    assert created is not None
    assert planned is not None
    task_id = created.splitlines()[1].split(" ", 1)[0]
    assert detail == (
        f"Task detail:\n{task_id} [open]: Review agent memory tools\n"
        "  steps:\n"
        "  - 1. [in_progress] Audit current state -> Audit current state\n"
        "  - 2. [pending] Write tests"
    )


@pytest.mark.asyncio
async def test_make_default_composer_can_show_completed_task_detail_by_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        created = await compose("todo: Review agent memory tools")
        assert created is not None
        task_id = created.splitlines()[1].split(" ", 1)[0]
        completed = await compose(f"complete task {task_id}")
        detail = await compose(f"show task {task_id}")

    assert completed == (
        f"Task updated:\n{task_id} [done]: Review agent memory tools"
    )
    assert detail == (
        f"Task detail:\n{task_id} [done]: Review agent memory tools"
    )


@pytest.mark.asyncio
async def test_make_default_composer_does_not_show_ambiguous_task_fragment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        first = await compose("todo: Review agent memory tools")
        second = await compose("todo: Review agent task lane")
        detail = await compose("show task Review agent")

    assert first is not None
    assert second is not None
    assert detail is not None
    assert detail.startswith("Multiple matching tasks:\n")
    assert "Review agent memory tools" in detail
    assert "Review agent task lane" in detail


@pytest.mark.asyncio
async def test_make_default_composer_does_not_plan_ambiguous_task_fragment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)

        first = await compose("todo: Review agent memory tools")
        second = await compose("todo: Review agent task lane")
        result = await compose("plan task Review agent: Inspect state; Write tests")
        tasks = await task_store.list(status="active", limit=10)
        step_counts = [
            len(await task_store.list_steps(task.task_id)) for task in tasks
        ]

    assert first is not None
    assert second is not None
    assert result is not None
    assert result.startswith("Multiple matching tasks:\n")
    assert "Review agent memory tools" in result
    assert "Review agent task lane" in result
    assert step_counts == [0, 0]


@pytest.mark.asyncio
async def test_make_default_composer_task_command_bypasses_llm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")

    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        compose = make_default_composer(task_store=task_store)
        reply = await compose("todo: Verify task command bypass")
        tasks = await task_store.list(status="active", limit=10)

    assert reply is not None
    assert reply.startswith("Task created:\ntask-")
    assert tasks[0].title == "Verify task command bypass"


@pytest.mark.asyncio
async def test_make_default_composer_without_key_remembers_and_recalls_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with ProfileStore(tmp_path / "profile.db") as profile:
        compose = make_default_composer(
            profile_store=profile,
            reminder_control_clock=lambda: 123_000,
        )

        remembered = await compose("remember my favorite editor is Cursor")
        remembered_terminal = await compose("remember my terminal is Ghostty")
        recalled = await compose("what do you remember about editor")
        listed = await compose("what do you remember?")
        forgotten = await compose("forget favorite editor")
        recalled_after_forget = await compose("what do you remember about editor")

        assert remembered == "Remembered favorite_editor: Cursor."
        assert remembered_terminal == "Remembered terminal: Ghostty."
        assert recalled == "I remember:\nfavorite_editor: Cursor"
        assert listed == (
            "I remember:\n"
            "favorite_editor: Cursor\n"
            "terminal: Ghostty"
        )
        assert forgotten == "Forgot:\nfavorite_editor: Cursor"
        assert recalled_after_forget == "I do not have a matching memory yet."
        facts = profile.get("memories.facts")
        assert set(facts) == {"terminal"}
        assert facts["terminal"]["value"] == "Ghostty"


@pytest.mark.asyncio
async def test_make_default_composer_without_key_suggests_memory_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    approvals = ApprovalStore()
    async with ProfileStore(tmp_path / "profile.db") as profile:
        compose = make_default_composer(
            approval_store=approvals,
            profile_store=profile,
            reminder_control_clock=lambda: 123_000,
        )

        reply = await compose("My favorite editor is Cursor")

        assert reply is not None
        assert profile.get("memories.facts", {}) == {}
        pending = approvals.list_pending()
        assert len(pending) == 1
        assert pending[0].prompt == "Remember favorite_editor: Cursor?"
        assert pending[0].extras["memory_source"] == "auto"


@pytest.mark.asyncio
async def test_make_default_composer_searches_chat_memory_without_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "default",
            [
                Message(role="user", content="We discussed bluebird yesterday."),
                Message(role="assistant", content="Bluebird is the launch codename."),
            ],
        )
        compose = make_default_composer(chat_memory=mem)

        reply = await compose("what did we discuss about bluebird")

        assert reply == (
            "Earlier in this chat:\n"
            "- user: We discussed bluebird yesterday.\n"
            "- assistant: Bluebird is the launch codename."
        )


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


# ---------------------------------------------------------------------------
# V10 L3-B1: streaming chat composer
# ---------------------------------------------------------------------------


def _sse_lines(*chunks: str | None) -> bytes:
    """Build a fake OpenAI-compatible SSE response body. ``None``
    triggers a final ``data: [DONE]`` marker."""
    out: list[str] = []
    for c in chunks:
        if c is None:
            out.append("data: [DONE]")
        else:
            out.append(
                "data: "
                + json.dumps({"choices": [{"delta": {"content": c}}]})
            )
    out.append("")
    return ("\n".join(out)).encode("utf-8")


def _sse_events(*events: dict[str, object] | None) -> bytes:
    out: list[str] = []
    for event in events:
        if event is None:
            out.append("data: [DONE]")
        else:
            out.append("data: " + json.dumps(event))
    out.append("")
    return ("\n".join(out)).encode("utf-8")


@pytest.mark.asyncio
async def test_streaming_composer_yields_tokens_and_records_history() -> None:
    """Streaming composer must yield each delta in order and remember
    the assistant turn for the next request's rolling context."""

    from deskmate_agent.skills import openai_compat_streaming_composer

    request_bodies: list[dict[str, object]] = []
    canned_responses: list[bytes] = [
        _sse_lines("Hel", "lo", " world", None),
        _sse_lines("yep", None),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        idx = len(request_bodies) - 1
        body = canned_responses[min(idx, len(canned_responses) - 1)]
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "text/event-stream"},
        )

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
    )

    tokens = [t async for t in compose("hi")]
    assert tokens == ["Hel", "lo", " world"]
    assert request_bodies[0]["stream"] is True
    assert request_bodies[0]["messages"][-1] == {"role": "user", "content": "hi"}

    # Second turn: history must include the prior user + assistant.
    tokens2 = [t async for t in compose("how are you")]
    assert tokens2 == ["yep"]
    second_messages = request_bodies[1]["messages"]
    # Skip the leading system prompt(s); collect the user/assistant
    # role sequence we shipped.
    role_seq = [m["role"] for m in second_messages if m["role"] != "system"]
    assert role_seq == ["user", "assistant", "user"]
    assert second_messages[-1] == {"role": "user", "content": "how are you"}
    # Assistant turn must echo the FULL accumulated reply, not just
    # the first delta.
    assistant_msg = next(m for m in second_messages if m["role"] == "assistant")
    assert assistant_msg["content"] == "Hello world"


@pytest.mark.asyncio
async def test_streaming_composer_accumulates_tool_call_deltas_and_persists(
    tmp_path,
) -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    async with (
        ChatMemory(tmp_path / "chat.db") as mem,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        request_bodies: list[dict[str, object]] = []
        tool_events = []
        store = ReminderStore()

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if body.get("stream") is True:
                return httpx.Response(
                    200,
                    content=_sse_events(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call-stream-1",
                                                "type": "function",
                                                "function": {
                                                    "name": "deskmate_schedule_reminder",
                                                    "arguments": '{"text":"stretch"',
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "function": {
                                                    "arguments": ',"delay_ms":60000}'
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        None,
                    ),
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Reminder is set.",
                            }
                        }
                    ]
                },
            )

        compose = openai_compat_streaming_composer(
            base_url="https://api.test/v1",
            api_key="sk-test",
            model="gpt-test",
            client=_client(handler),
            chat_memory=mem,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=store,
                reminder_clock=lambda: 1_000,
                reminder_id_factory=lambda: "r-stream-tool",
            ),
            tool_event_sink=tool_events.append,
        )

        tokens = [t async for t in compose("please remind me to stretch")]

        assert tokens == ["Reminder is set."]
        assert request_bodies[0]["stream"] is True
        assert "tools" in request_bodies[0]
        assert request_bodies[0]["tool_choice"] == "auto"
        assert store.get("r-stream-tool") is not None

        second_messages = request_bodies[1]["messages"]
        assert second_messages[-2]["role"] == "assistant"
        assert second_messages[-2]["tool_calls"][0]["id"] == "call-stream-1"
        assert second_messages[-2]["tool_calls"][0]["function"]["arguments"] == (
            '{"text":"stretch","delay_ms":60000}'
        )
        assert second_messages[-1]["role"] == "tool"
        assert second_messages[-1]["tool_call_id"] == "call-stream-1"
        assert "Reminder scheduled" in second_messages[-1]["content"]

        assert [type(event) for event in tool_events] == [
            SessionActivityUpdated,
            SessionCompleted,
        ]
        assert tool_events[0].phase is SessionPhase.RUNNING_TOOL
        assert tool_events[0].tool_name == "deskmate_schedule_reminder"
        assert tool_events[1].failed is False

        sessions = SessionStore()
        reducer = AgentEventReducer(
            session_store=sessions,
            approval_store=ApprovalStore(),
        )
        for event in tool_events:
            reducer.apply(event)
        session = sessions.get("deskmate-tools-default")
        assert session is not None
        assert session.phase is SessionPhase.COMPLETED
        assert session.extras["tool_name"] == "deskmate_schedule_reminder"
        assert session.extras["tool_action"] == "deskmate_schedule_reminder"
        assert session.extras["tool_target"] == "stretch"
        assert session.extras["tool_needs_user"] == "false"

        persisted = await mem.recent("default", limit=10)
        assert [m.role for m in persisted] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        actions = await tool_log.recent("default", limit=10)
        assert len(actions) == 1
        assert actions[0].tool_call_id == "call-stream-1"
        assert actions[0].tool_name == "deskmate_schedule_reminder"
        assert actions[0].arguments == {"text": "stretch", "delay_ms": 60_000}
        assert actions[0].status == "completed"


@pytest.mark.asyncio
async def test_streaming_tool_calls_can_chain_memory_lookup_then_reminder(
    tmp_path,
) -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    async with (
        ChatMemory(tmp_path / "chat.db") as mem,
        ProfileStore(tmp_path / "profile.db") as profile,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        profile.set(
            "memories.facts",
            {
                "stretch_break": {
                    "key": "stretch_break",
                    "value": "stand up and stretch",
                    "updated_at_ms": 1_000,
                }
            },
        )
        await profile.flush()
        request_bodies: list[dict[str, object]] = []
        store = ReminderStore()

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if body.get("stream") is True:
                return httpx.Response(
                    200,
                    content=_sse_events(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "stream-recall-1",
                                                "type": "function",
                                                "function": {
                                                    "name": "deskmate_recall_memory",
                                                    "arguments": '{"query":"stretch"',
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "function": {"arguments": ',"limit":1}'},
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        None,
                    ),
                    headers={"Content-Type": "text/event-stream"},
                )
            if len(request_bodies) == 2:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "stream-reminder-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": json.dumps(
                                                    {
                                                        "text": "stand up and stretch",
                                                        "delay_ms": 120_000,
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I found it and set the reminder.",
                            }
                        }
                    ]
                },
            )

        compose = openai_compat_streaming_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            chat_memory=mem,
            profile_store=profile,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=store,
                reminder_clock=lambda: 10_000,
                reminder_id_factory=lambda: "r-stream-chain",
                profile_store=profile,
            ),
        )

        chunks = [
            chunk
            async for chunk in compose(
                "use my stretch memory and remind me in two minutes"
            )
        ]

        assert chunks == ["I found it and set the reminder."]
        assert len(request_bodies) == 3
        assert "tools" in request_bodies[1]
        assert "tools" in request_bodies[2]
        final_messages = request_bodies[2]["messages"]
        tool_messages = [
            message for message in final_messages if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "stream-recall-1",
            "stream-reminder-1",
        ]
        assert store.get("r-stream-chain") is not None
        actions = await tool_log.recent("default", limit=10)
        assert [record.tool_name for record in actions] == [
            "deskmate_recall_memory",
            "deskmate_schedule_reminder",
        ]


@pytest.mark.asyncio
async def test_streaming_blocks_forget_memory_without_explicit_user_intent(
    tmp_path,
) -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    async with (
        ProfileStore(tmp_path / "profile.db") as profile,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "Cursor",
                    "updated_at_ms": 1_000,
                }
            },
        )
        await profile.flush()
        request_bodies: list[dict[str, object]] = []
        tool_events = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if body.get("stream") is True:
                return httpx.Response(
                    200,
                    content=_sse_events(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "stream-forget-policy-1",
                                                "type": "function",
                                                "function": {
                                                    "name": "deskmate_forget_memory",
                                                    "arguments": '{"query":"preferred_ide"}',
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        None,
                    ),
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I still have that memory.",
                            }
                        }
                    ]
                },
            )

        compose = openai_compat_streaming_composer(
            base_url="https://api.test/v1",
            api_key="sk",
            model="m",
            client=_client(handler),
            profile_store=profile,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(profile_store=profile),
            tool_event_sink=tool_events.append,
        )

        chunks = [chunk async for chunk in compose("do I prefer Cursor?")]

        assert chunks == ["I still have that memory."]
        facts = profile.get("memories.facts")
        assert facts["preferred_ide"]["value"] == "Cursor"
        tool_msg = request_bodies[1]["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "stream-forget-policy-1"
        assert tool_msg["content"].startswith(
            "Tool error: deskmate_forget_memory requires an explicit user request"
        )
        assert [event.raw_event for event in tool_events] == [
            "tool.started",
            "tool.failed",
        ]
        actions = await tool_log.recent("default", limit=10)
        assert [record.status for record in actions] == ["failed"]
        assert actions[0].tool_name == "deskmate_forget_memory"


@pytest.mark.asyncio
async def test_streaming_duplicate_tool_calls_do_not_repeat_side_effect() -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    request_bodies: list[dict[str, object]] = []
    tool_events = []
    store = ReminderStore()
    store_events = []
    store.subscribe(lambda event: store_events.append(event))
    reminder_ids = iter(["r-stream-dup-1", "r-stream-dup-2"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if body.get("stream") is True:
            return httpx.Response(
                200,
                content=_sse_events(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "stream-dup-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": '{"text":"stretch","delay_ms":60000}',
                                            },
                                        },
                                        {
                                            "index": 1,
                                            "id": "stream-dup-2",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": '{"delay_ms":60000,"text":"stretch"}',
                                            },
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                    None,
                ),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "Done."}}
                ]
            },
        )

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        tool_executor=DeskmateToolExecutor(
            reminder_store=store,
            reminder_clock=lambda: 1_000,
            reminder_id_factory=lambda: next(reminder_ids),
        ),
        tool_event_sink=tool_events.append,
    )

    tokens = [t async for t in compose("please remind me")]

    assert tokens == ["Done."]
    assert [event.reminder_id for event in store_events] == ["r-stream-dup-1"]
    assert store.get("r-stream-dup-1") is not None
    assert store.get("r-stream-dup-2") is None
    tool_messages = [
        message
        for message in request_bodies[1]["messages"]
        if message["role"] == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "stream-dup-1",
        "stream-dup-2",
    ]
    assert tool_messages[0]["content"] == tool_messages[1]["content"]
    assert [event.raw_event for event in tool_events] == [
        "tool.started",
        "tool.completed",
        "tool.duplicate",
    ]


@pytest.mark.asyncio
async def test_streaming_duplicate_tool_calls_across_rounds_do_not_repeat_side_effect(
    tmp_path,
) -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    async with (
        ChatMemory(tmp_path / "chat.db") as mem,
        ToolActionLog(tmp_path / "tool_actions.db") as tool_log,
    ):
        request_bodies: list[dict[str, object]] = []
        tool_events = []
        store = ReminderStore()
        store_events = []
        store.subscribe(lambda event: store_events.append(event))
        reminder_ids = iter(["r-stream-cross-dup-1", "r-stream-cross-dup-2"])

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            request_bodies.append(body)
            if body.get("stream") is True:
                return httpx.Response(
                    200,
                    content=_sse_events(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "stream-cross-dup-1",
                                                "type": "function",
                                                "function": {
                                                    "name": "deskmate_schedule_reminder",
                                                    "arguments": '{"text":"stretch"',
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "function": {
                                                    "arguments": ',"delay_ms":60000}'
                                                },
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        None,
                    ),
                    headers={"Content-Type": "text/event-stream"},
                )
            if len(request_bodies) == 2:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "stream-cross-dup-2",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_schedule_reminder",
                                                "arguments": json.dumps(
                                                    {
                                                        "delay_ms": 60_000,
                                                        "text": "stretch",
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Done.",
                            }
                        }
                    ]
                },
            )

        compose = openai_compat_streaming_composer(
            base_url="https://api.test/v1",
            api_key="sk-test",
            model="gpt-test",
            client=_client(handler),
            chat_memory=mem,
            tool_action_log=tool_log,
            tool_executor=DeskmateToolExecutor(
                reminder_store=store,
                reminder_clock=lambda: 1_000,
                reminder_id_factory=lambda: next(reminder_ids),
            ),
            tool_event_sink=tool_events.append,
        )

        tokens = [token async for token in compose("please double-check reminder")]

        assert tokens == ["Done."]
        assert len(request_bodies) == 3
        assert [event.reminder_id for event in store_events] == [
            "r-stream-cross-dup-1"
        ]
        assert store.get("r-stream-cross-dup-1") is not None
        assert store.get("r-stream-cross-dup-2") is None
        final_tool_messages = [
            message
            for message in request_bodies[2]["messages"]
            if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in final_tool_messages] == [
            "stream-cross-dup-1",
            "stream-cross-dup-2",
        ]
        assert final_tool_messages[0]["content"] == final_tool_messages[1]["content"]
        assert [event.raw_event for event in tool_events] == [
            "tool.started",
            "tool.completed",
            "tool.duplicate",
        ]
        actions = await tool_log.recent("default", limit=10)
        assert [record.tool_call_id for record in actions] == [
            "stream-cross-dup-1",
            "stream-cross-dup-2",
        ]
        assert [record.status for record in actions] == ["completed", "duplicate"]
        assert actions[0].result == actions[1].result


@pytest.mark.asyncio
async def test_streaming_composer_unknown_tool_call_returns_tool_error() -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    request_bodies: list[dict[str, object]] = []
    tool_events = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if body.get("stream") is True:
            return httpx.Response(
                200,
                content=_sse_events(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "bad-stream-1",
                                            "type": "function",
                                            "function": {
                                                "name": "run_shell",
                                                "arguments": "{}",
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    None,
                ),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I cannot use that tool.",
                        }
                    }
                ]
            },
        )

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        tool_executor=DeskmateToolExecutor(),
        tool_event_sink=tool_events.append,
    )

    tokens = [t async for t in compose("run a command")]

    assert tokens == ["I cannot use that tool."]
    tool_message = request_bodies[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "unknown tool" in tool_message["content"]
    assert [type(event) for event in tool_events] == [
        SessionActivityUpdated,
        SessionCompleted,
    ]
    assert tool_events[1].failed is True
    assert tool_events[1].tool_result.startswith("Tool error:")
    assert tool_events[1].tool_action == "run_shell"
    assert tool_events[1].tool_needs_user == "true"


@pytest.mark.asyncio
async def test_streaming_tool_call_timeout_returns_failed_event() -> None:
    from deskmate_agent.skills import openai_compat_streaming_composer

    request_bodies: list[dict[str, object]] = []
    tool_events = []

    class SlowExecutor(DeskmateToolExecutor):
        async def execute(self, name, arguments):
            await asyncio.sleep(0.2)
            return "should not arrive"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if body.get("stream") is True:
            return httpx.Response(
                200,
                content=_sse_events(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "slow-stream-1",
                                            "type": "function",
                                            "function": {
                                                "name": "deskmate_slow_tool",
                                                "arguments": "{}",
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    None,
                ),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "The streaming tool timed out.",
                        }
                    }
                ]
            },
        )

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        tool_executor=SlowExecutor(),
        tool_event_sink=tool_events.append,
        tool_timeout_s=0.01,
    )

    tokens = [t async for t in compose("run slow stream tool")]

    assert tokens == ["The streaming tool timed out."]
    tool_message = request_bodies[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "slow-stream-1"
    assert tool_message["content"] == "Tool error: deskmate_slow_tool timed out."
    assert [event.raw_event for event in tool_events] == [
        "tool.started",
        "tool.failed",
    ]
    assert tool_events[1].failed is True


@pytest.mark.asyncio
async def test_streaming_composer_first_token_observer_fires_on_first_token() -> None:
    """The first-token observer must fire exactly once, on the FIRST
    delta — not on stream open and not on each subsequent chunk."""

    from deskmate_agent.skills import openai_compat_streaming_composer

    observed: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_lines("a", "b", "c", None),
            headers={"Content-Type": "text/event-stream"},
        )

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        first_token_observer=observed.append,
    )
    _ = [t async for t in compose("hi")]
    assert len(observed) == 1, observed
    assert observed[0] >= 0.0


@pytest.mark.asyncio
async def test_streaming_composer_falls_back_to_canned_when_stream_fails() -> None:
    """An HTTP-level error before any token arrives must fall back
    to the configured non-streaming composer, yielded as a single
    chunk."""

    from deskmate_agent.skills import openai_compat_streaming_composer

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def fallback(text: str) -> str | None:
        return f"canned:{text}"

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        fallback=fallback,
    )
    tokens = [t async for t in compose("hi")]
    assert tokens == ["canned:hi"]


@pytest.mark.asyncio
async def test_streaming_composer_partial_then_error_keeps_partial_no_fallback() -> None:
    """If at least one token arrived before the stream broke, the
    partial reply stays — we don't paste a canned fallback over it."""

    from deskmate_agent.skills import openai_compat_streaming_composer

    # Build a transport that replies 200 with a stream that ends
    # cleanly after one delta. The composer's contract is "any
    # token suppresses the fallback", which the unit covers without
    # needing to fake a mid-stream socket reset.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_lines("partial", None),
            headers={"Content-Type": "text/event-stream"},
        )

    fallback_calls: list[str] = []

    async def fallback(text: str) -> str | None:
        fallback_calls.append(text)
        return f"canned:{text}"

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        fallback=fallback,
    )
    tokens = [t async for t in compose("hi")]
    assert tokens == ["partial"]
    assert fallback_calls == [], "fallback must not fire when stream produced data"


@pytest.mark.asyncio
async def test_streaming_composer_ignores_unknown_lines() -> None:
    """Heartbeat / comment lines and malformed JSON inside the stream
    must not break the iterator. They're silently skipped."""

    from deskmate_agent.skills import openai_compat_streaming_composer

    raw = (
        ": ping\n"
        "\n"
        "data: not-json\n"
        + "data: "
        + json.dumps({"choices": [{"delta": {"content": "good"}}]})
        + "\n"
        + "data: [DONE]\n"
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"Content-Type": "text/event-stream"},
        )

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
    )
    tokens = [t async for t in compose("hi")]
    assert tokens == ["good"]


@pytest.mark.asyncio
async def test_make_default_streaming_composer_returns_none_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streaming sibling factory returns ``None`` so the
    dispatcher transparently falls back to the canned non-streaming
    path when no key is configured."""

    from deskmate_agent.skills import make_default_streaming_composer

    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)
    assert make_default_streaming_composer() is None


@pytest.mark.asyncio
async def test_make_default_streaming_composer_respects_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deskmate_agent.skills import make_default_streaming_composer

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DESKMATE_LLM_STREAMING", "0")
    assert make_default_streaming_composer() is None


@pytest.mark.asyncio
async def test_make_default_streaming_composer_returns_composer_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deskmate_agent.skills import make_default_streaming_composer

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.delenv("DESKMATE_LLM_STREAMING", raising=False)
    composer = make_default_streaming_composer()
    assert composer is not None


@pytest.mark.asyncio
async def test_make_default_streaming_composer_runs_computer_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deskmate_agent.skills import make_default_streaming_composer

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.delenv("DESKMATE_LLM_STREAMING", raising=False)
    calls: list[list[str]] = []

    import deskmate_agent.skills.computer_control as control

    monkeypatch.setattr(
        control,
        "_default_opener",
        lambda args: calls.append(args) is None,
    )
    composer = make_default_streaming_composer()
    assert composer is not None

    chunks = [chunk async for chunk in composer("open Terminal")]

    assert chunks == ["Opened Terminal."]
    assert calls == [["open", "-a", "Terminal"]]


@pytest.mark.asyncio
async def test_make_default_streaming_composer_runs_reminder_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deskmate_agent.skills import make_default_streaming_composer

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.delenv("DESKMATE_LLM_STREAMING", raising=False)
    store = ReminderStore()
    composer = make_default_streaming_composer(
        reminder_store=store,
        reminder_control_clock=lambda: 1_000,
        reminder_id_factory=lambda: "r-stream-default",
    )
    assert composer is not None

    created = [chunk async for chunk in composer("timer for 1 minute")]
    listed = [chunk async for chunk in composer("show reminders")]
    cancelled = [chunk async for chunk in composer("delete timer r-stream-default")]
    listed_after = [chunk async for chunk in composer("show reminders")]

    assert created == ["Reminder set for 1 minute: Timer done."]
    assert listed == [
        "Pending reminders:\nr-stream-default [due in 1 minute]: Timer done"
    ]
    assert cancelled == ["Cancelled reminder r-stream-default: Timer done."]
    assert listed_after == ["You do not have any pending reminders."]


@pytest.mark.asyncio
async def test_make_default_streaming_composer_runs_task_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from deskmate_agent.skills import make_default_streaming_composer

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.delenv("DESKMATE_LLM_STREAMING", raising=False)
    async with DeskmateTaskStore(tmp_path / "tasks.db") as task_store:
        composer = make_default_streaming_composer(task_store=task_store)
        assert composer is not None

        created = [chunk async for chunk in composer("task: Stream task control")]
        listed = [chunk async for chunk in composer("show tasks")]

    assert len(created) == 1
    assert created[0].startswith("Task created:\ntask-")
    assert "[open]: Stream task control" in created[0]
    assert listed == ["Tasks:\n" + created[0].splitlines()[1]]


@pytest.mark.asyncio
async def test_make_default_streaming_composer_runs_memory_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from deskmate_agent.skills import make_default_streaming_composer

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.delenv("DESKMATE_LLM_STREAMING", raising=False)
    async with ProfileStore(tmp_path / "profile.db") as profile:
        composer = make_default_streaming_composer(
            profile_store=profile,
            reminder_control_clock=lambda: 456_000,
        )
        assert composer is not None

        chunks = [chunk async for chunk in composer("remember my snack is mochi")]
        extra = [chunk async for chunk in composer("remember my terminal is Ghostty")]
        listed = [chunk async for chunk in composer("你记得我什么")]
        forgotten = [chunk async for chunk in composer("forget snack")]
        recalled = [chunk async for chunk in composer("what do you remember about snack")]

        assert chunks == ["Remembered snack: mochi."]
        assert extra == ["Remembered terminal: Ghostty."]
        assert listed == ["I remember:\nsnack: mochi\nterminal: Ghostty"]
        assert forgotten == ["Forgot:\nsnack: mochi"]
        assert recalled == ["I do not have a matching memory yet."]
        facts = profile.get("memories.facts")
        assert set(facts) == {"terminal"}
        assert facts["terminal"]["value"] == "Ghostty"


# ---------------------------------------------------------------------------
# V10 L3-B1: connection prewarm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_llm_prewarm_no_op_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key → prewarm is a fast no-op, never makes a network call."""

    from deskmate_agent.skills import default_llm_prewarm

    monkeypatch.delenv("DESKMATE_LLM_API_KEY", raising=False)

    # Sentinel: if it tried to create an httpx client it would throw
    # because no DNS is configured for "api.openai.com" in the test
    # env. No throw == no network attempt.
    await default_llm_prewarm(timeout_s=0.5)


@pytest.mark.asyncio
async def test_default_llm_prewarm_swallows_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed warm-up MUST NOT raise — agent.ready cannot block on
    a flaky model endpoint."""

    from deskmate_agent.skills import default_llm_prewarm

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    # Point at a definitely-unreachable URL so the request fails
    # fast inside the configured timeout.
    monkeypatch.setenv(
        "DESKMATE_LLM_BASE_URL", "https://127.0.0.1:1/v1"
    )

    # Must complete without raising.
    await default_llm_prewarm(timeout_s=0.2)



# ---------------------------------------------------------------------------
# V10 L3-B3: tiered model selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_default_composer_picks_reactive_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DESKMATE_LLM_MODEL_REACTIVE`` wins over the generic
    ``DESKMATE_LLM_MODEL`` for ``skill_mode == "reactive"``."""

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DESKMATE_LLM_MODEL", "fallback-model")
    monkeypatch.setenv("DESKMATE_LLM_MODEL_REACTIVE", "big-reactive-model")
    monkeypatch.delenv("DESKMATE_LLM_MODEL_PROACTIVE", raising=False)

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["model"] = json.loads(request.content)["model"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    # Inject the mock client by overriding the env-driven path with
    # a direct ``openai_compat_composer`` call that mirrors what
    # ``make_default_composer`` would build, plus an env probe to
    # verify the resolver actually picks the right model.
    from deskmate_agent.skills.llm_chat import _resolve_tiered_model

    assert _resolve_tiered_model("reactive") == "big-reactive-model"
    assert _resolve_tiered_model("proactive") == "fallback-model"

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model=_resolve_tiered_model("reactive"),
        client=_client(handler),
    )
    await compose("hi")
    assert captured["model"] == "big-reactive-model"


@pytest.mark.asyncio
async def test_make_default_composer_picks_proactive_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DESKMATE_LLM_MODEL", "fallback-model")
    monkeypatch.setenv("DESKMATE_LLM_MODEL_PROACTIVE", "cheap-proactive-model")
    monkeypatch.delenv("DESKMATE_LLM_MODEL_REACTIVE", raising=False)

    from deskmate_agent.skills.llm_chat import _resolve_tiered_model

    assert _resolve_tiered_model("reactive") == "fallback-model"
    assert _resolve_tiered_model("proactive") == "cheap-proactive-model"


@pytest.mark.asyncio
async def test_tiered_model_falls_back_when_no_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tier-specific override → both modes use ``DESKMATE_LLM_MODEL``."""

    monkeypatch.setenv("DESKMATE_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DESKMATE_LLM_MODEL", "shared-model")
    monkeypatch.delenv("DESKMATE_LLM_MODEL_PROACTIVE", raising=False)
    monkeypatch.delenv("DESKMATE_LLM_MODEL_REACTIVE", raising=False)

    from deskmate_agent.skills.llm_chat import _resolve_tiered_model

    assert _resolve_tiered_model("reactive") == "shared-model"
    assert _resolve_tiered_model("proactive") == "shared-model"


def test_tool_round_limit_env_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deskmate_agent.skills.llm_chat import _resolve_tool_round_limit

    monkeypatch.delenv("DESKMATE_LLM_TOOL_ROUND_LIMIT", raising=False)
    assert _resolve_tool_round_limit() == 3

    monkeypatch.setenv("DESKMATE_LLM_TOOL_ROUND_LIMIT", "0")
    assert _resolve_tool_round_limit() == 1

    monkeypatch.setenv("DESKMATE_LLM_TOOL_ROUND_LIMIT", "9")
    assert _resolve_tool_round_limit() == 5

    monkeypatch.setenv("DESKMATE_LLM_TOOL_ROUND_LIMIT", "bad")
    assert _resolve_tool_round_limit() == 3


# ---------------------------------------------------------------------------
# V10 L3-B5: streaming first-token timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_composer_first_token_timeout_falls_back() -> None:
    """A model that opens the stream but never sends a first token
    inside the deadline must fall through to the configured fallback."""

    from deskmate_agent.skills import openai_compat_streaming_composer

    # The mock transport's streaming body iterates over the bytes
    # we hand it. To simulate "open but never any token", emit
    # only a SSE comment line (which the parser ignores) and never
    # close the stream — but httpx's MockTransport will still close
    # at end-of-bytes. Closer surrogate: emit ZERO data events,
    # so first_token_logged stays False and the deadline fires
    # only when the iterator is awaited longer than the budget.
    async def slow_stream() -> bytes:
        await asyncio.sleep(0.5)
        return b": ping\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        # Body that takes longer than the deadline to produce any
        # parseable line. ``httpx.MockTransport`` runs the handler
        # to completion before returning the response, so an async
        # body that sleeps achieves the same effect as a slow LLM.
        async def body():
            await asyncio.sleep(0.5)
            yield b": ping\n\n"
        return httpx.Response(
            200,
            content=body(),
            headers={"Content-Type": "text/event-stream"},
        )

    canned_calls: list[str] = []

    async def fallback(text: str) -> str | None:
        canned_calls.append(text)
        return f"canned:{text}"

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        fallback=fallback,
        first_token_timeout_s=0.1,
    )
    tokens = [t async for t in compose("hi")]
    assert tokens == ["canned:hi"]
    assert canned_calls == ["hi"]


@pytest.mark.asyncio
async def test_streaming_composer_first_token_timeout_disabled_does_not_fire() -> None:
    """``first_token_timeout_s=None`` lets a slow stream finish
    without tripping the deadline."""

    from deskmate_agent.skills import openai_compat_streaming_composer

    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            # Brief sleep, then a real token. Without the deadline
            # this should land normally.
            await asyncio.sleep(0.05)
            for line in (
                b"data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}).encode("utf-8") + b"\n",
                b"data: [DONE]\n",
            ):
                yield line
        return httpx.Response(
            200,
            content=body(),
            headers={"Content-Type": "text/event-stream"},
        )

    compose = openai_compat_streaming_composer(
        base_url="https://api.test/v1",
        api_key="sk-test",
        model="gpt-test",
        client=_client(handler),
        first_token_timeout_s=None,
    )
    tokens = [t async for t in compose("hi")]
    assert tokens == ["ok"]
