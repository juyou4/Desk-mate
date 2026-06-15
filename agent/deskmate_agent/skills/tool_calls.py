"""OpenAI-compatible tool-call execution for Deskmate.

V1 intentionally exposes only high-level, allowlisted actions. Computer
control routes through the existing natural-language parser and approval
store, so sensitive actions remain gated exactly like direct user commands.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..approvals import ApprovalStore
from ..memory import (
    ChatMemory,
    DeskmateTaskStore,
    MemorySuggestion,
    ProfileStore,
    TaskSuggestion,
    ToolActionLog,
    create_memory_suggestion_approval,
    create_task_suggestion_approval,
    format_deskmate_task,
    format_deskmate_task_step,
    format_tool_action_summary,
    format_tool_lesson,
    format_tool_task_summary,
)
from ..reminders import Reminder, ReminderStatus, ReminderStore
from .computer_control import PendingComputerActionStore, computer_control_composer
from .reminder_control import ReminderRequest, schedule_reminder_request
from .system_tools import (
    CalendarEventRequest,
    CommandRunner,
    WeatherFetcher,
    create_calendar_event,
    get_weather,
    list_system_tools,
)

Clock = Callable[[], int]
IdFactory = Callable[[], str]

DESKMATE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "deskmate_schedule_reminder",
            "description": "Schedule a local reminder after a relative delay.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "What to remind the user about.",
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": "Relative delay in milliseconds.",
                        "minimum": 1000,
                    },
                },
                "required": ["text", "delay_ms"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_list_reminders",
            "description": (
                "List local Deskmate reminders without creating, firing, or "
                "cancelling anything. Defaults to pending reminders."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "fired", "all"],
                        "description": "Which reminder status to list. Defaults to pending.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of reminders to return.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_cancel_reminder",
            "description": (
                "Cancel a local Deskmate reminder by reminder_id. Use only "
                "when the user explicitly asks to cancel a reminder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "string",
                        "description": (
                            "The reminder id returned by deskmate_list_reminders "
                            "or a previously scheduled reminder result."
                        ),
                    }
                },
                "required": ["reminder_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_computer_action",
            "description": (
                "Execute a safe local computer action using Deskmate's existing "
                "natural-language command parser. Sensitive actions request "
                "approval instead of running immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "A concise command such as 'open Terminal', "
                            "'search for pytest docs', or 'set volume to 40'."
                        ),
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_create_calendar_event",
            "description": (
                "Create a macOS Calendar event. Use when the user explicitly "
                "asks Deskmate to add, schedule, or put an event on the calendar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short event title.",
                    },
                    "start_at": {
                        "type": "string",
                        "description": "Event start datetime, ISO-like local time preferred.",
                    },
                    "end_at": {
                        "type": "string",
                        "description": "Optional event end datetime.",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Optional duration in minutes when end_at is absent.",
                        "minimum": 1,
                        "maximum": 1440,
                    },
                    "calendar": {
                        "type": "string",
                        "description": "Calendar name. Defaults to Calendar.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional event notes.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Optional event location.",
                    },
                },
                "required": ["title", "start_at"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_get_weather",
            "description": (
                "Read a compact weather report for a location using a "
                "CLI-friendly weather endpoint. Use this when the user asks "
                "what the weather is, not when they only ask to open the app."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City/location. Leave empty for the service default location.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_list_system_tools",
            "description": (
                "List Deskmate's high-level local system tools. This is a "
                "read-only MCP-style discovery helper."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_remember_fact",
            "description": (
                "Persist a durable user preference or fact that should help "
                "future Deskmate conversations. Use only when the user "
                "explicitly asks you to remember something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Short stable key, lowercase with underscores, "
                            "such as preferred_ide or coffee_order."
                        ),
                    },
                    "value": {
                        "type": "string",
                        "description": "The fact or preference to remember.",
                    },
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_suggest_memory",
            "description": (
                "Propose a durable memory candidate from ordinary conversation. "
                "This does not write memory directly; it creates a user approval "
                "that must be allowed before the fact is stored."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Short stable key, lowercase with underscores, "
                            "such as preferred_ide or coffee_order."
                        ),
                    },
                    "value": {
                        "type": "string",
                        "description": "The fact or preference to suggest.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short reason this may be useful later.",
                    },
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_recall_memory",
            "description": "Search durable Deskmate memories by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of memories to return.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_list_memories",
            "description": (
                "List current durable Deskmate memories without requiring a "
                "keyword. Use when the user asks what you remember about them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of memories to return.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_forget_memory",
            "description": (
                "Delete durable Deskmate memories by key or keyword. Use only "
                "when the user explicitly asks to forget or remove a stored "
                "fact/preference."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Memory key or keyword to forget.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_search_chat_memory",
            "description": (
                "Search this Deskmate conversation's persisted chat transcript "
                "for older user/assistant/tool messages by keyword. Use when "
                "the user asks what was discussed earlier or when useful "
                "context may be outside the recent rolling window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matching messages.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_recent_tool_actions",
            "description": (
                "Read recent Deskmate-owned tool-call results from the local "
                "action log for this conversation. This is read-only and does "
                "not execute a new computer action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional keyword to search tool name, arguments, or result.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Optional exact tool name filter.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional exact tool task id filter returned by deskmate_recent_tool_tasks.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["completed", "failed", "duplicate"],
                        "description": "Optional status filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tool actions to return.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_create_task",
            "description": (
                "Create a persistent Deskmate task/todo item for user-visible "
                "work tracking. Use when the user asks to remember a task, "
                "track work, or add something to a todo list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short task title.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional implementation notes or context.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "in_progress", "done", "cancelled"],
                        "description": "Initial task status. Defaults to open.",
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_suggest_task",
            "description": (
                "Propose a persistent Deskmate task/todo from ordinary "
                "conversation. This does not write the task directly; it "
                "creates a user approval that must be allowed first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short task title.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional task context.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "in_progress"],
                        "description": "Suggested active status. Defaults to open.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short reason this task may be useful to track.",
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_list_tasks",
            "description": (
                "List persistent Deskmate tasks for this conversation. "
                "Defaults to active open/in_progress tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "active",
                            "open",
                            "in_progress",
                            "done",
                            "cancelled",
                            "all",
                        ],
                        "description": "Which task status to list. Defaults to active.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tasks to return.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_search_tasks",
            "description": "Search persistent Deskmate tasks by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search task id, title, or notes.",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "active",
                            "open",
                            "in_progress",
                            "done",
                            "cancelled",
                            "all",
                        ],
                        "description": "Optional task status filter. Defaults to all.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tasks to return.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_update_task",
            "description": (
                "Update a persistent Deskmate task title, notes, or status by "
                "task_id. Use when the user asks to mark progress, complete, "
                "cancel, or revise a tracked task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task id returned by deskmate_list_tasks or deskmate_create_task.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional replacement title.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional replacement notes.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "in_progress", "done", "cancelled"],
                        "description": "Optional replacement task status.",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_update_task_steps",
            "description": (
                "Replace the checklist steps for one persistent Deskmate task. "
                "Use a complete ordered list, with at most one in_progress step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task id returned by deskmate_list_tasks or deskmate_create_task.",
                    },
                    "steps": {
                        "type": "array",
                        "description": "Complete ordered task checklist, maximum 20 items.",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Stable step description.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Step state.",
                                },
                                "active_form": {
                                    "type": "string",
                                    "description": "Short present-tense text for the current in_progress step.",
                                },
                            },
                            "required": ["content", "status"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["task_id", "steps"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_list_task_steps",
            "description": (
                "Read the checklist steps for one persistent Deskmate task. "
                "This is read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task id returned by deskmate_list_tasks or deskmate_create_task.",
                    }
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_recent_tool_tasks",
            "description": (
                "Read recent Deskmate-owned multi-step tool task lifecycles "
                "from the local action log for this conversation. This is "
                "read-only and does not execute a new computer action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional keyword to search task id, user request, or task summary.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["running", "completed", "failed"],
                        "description": "Optional task status filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tool tasks to return.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_task_context",
            "description": (
                "Read a persistent Deskmate task together with related "
                "tool-task lifecycles, tool-call results, and tool lessons. "
                "Use this to resume work on a tracked task. This is read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Optional exact task id from deskmate_list_tasks.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional keyword to find a task when task_id is unknown.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum related rows per section.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_tool_task_details",
            "description": (
                "Read one persisted Deskmate tool task lifecycle plus its "
                "tool-call action details by task id. This is read-only and "
                "does not execute a new computer action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Exact task id returned by deskmate_recent_tool_tasks.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of task actions to return.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deskmate_recent_tool_lessons",
            "description": (
                "Read durable lessons learned from previous Deskmate-owned "
                "tool calls in this conversation. Use before repeating a "
                "similar local action to avoid repeated failures or reuse "
                "known successful patterns. This is read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional keyword to search tool, target, outcome, or lesson text.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of tool lessons to return.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class DeskmateToolExecutor:
    """Executes the small V1 tool surface used by the LLM composer."""

    reminder_store: ReminderStore | None = None
    reminder_clock: Clock | None = None
    reminder_id_factory: IdFactory | None = None
    approval_store: ApprovalStore | None = None
    pending_computer_actions: PendingComputerActionStore | None = None
    computer_control_clock: Clock | None = None
    profile_store: ProfileStore | None = None
    chat_memory: ChatMemory | None = None
    task_store: DeskmateTaskStore | None = None
    tool_action_log: ToolActionLog | None = None
    calendar_runner: CommandRunner | None = None
    weather_fetcher: WeatherFetcher | None = None
    conversation_id: str = "default"

    async def execute(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        try:
            args = _parse_arguments(arguments)
        except ValueError as exc:
            return f"Tool error: {exc}"

        if name == "deskmate_schedule_reminder":
            return self._schedule_reminder(args)
        if name == "deskmate_list_reminders":
            return self._list_reminders(args)
        if name == "deskmate_cancel_reminder":
            return self._cancel_reminder(args)
        if name == "deskmate_computer_action":
            return await self._computer_action(args)
        if name == "deskmate_create_calendar_event":
            return self._create_calendar_event(args)
        if name == "deskmate_get_weather":
            return await self._get_weather(args)
        if name == "deskmate_list_system_tools":
            return list_system_tools()
        if name == "deskmate_remember_fact":
            return await self._remember_fact(args)
        if name == "deskmate_suggest_memory":
            return self._suggest_memory(args)
        if name == "deskmate_recall_memory":
            return self._recall_memory(args)
        if name == "deskmate_list_memories":
            return self._list_memories(args)
        if name == "deskmate_forget_memory":
            return await self._forget_memory(args)
        if name == "deskmate_search_chat_memory":
            return await self._search_chat_memory(args)
        if name == "deskmate_create_task":
            return await self._create_task(args)
        if name == "deskmate_suggest_task":
            return self._suggest_task(args)
        if name == "deskmate_list_tasks":
            return await self._list_tasks(args)
        if name == "deskmate_search_tasks":
            return await self._search_tasks(args)
        if name == "deskmate_update_task":
            return await self._update_task(args)
        if name == "deskmate_update_task_steps":
            return await self._update_task_steps(args)
        if name == "deskmate_list_task_steps":
            return await self._list_task_steps(args)
        if name == "deskmate_recent_tool_actions":
            return await self._recent_tool_actions(args)
        if name == "deskmate_recent_tool_tasks":
            return await self._recent_tool_tasks(args)
        if name == "deskmate_task_context":
            return await self._task_context(args)
        if name == "deskmate_tool_task_details":
            return await self._tool_task_details(args)
        if name == "deskmate_recent_tool_lessons":
            return await self._recent_tool_lessons(args)
        return f"Tool error: unknown tool '{name}'."

    def _schedule_reminder(self, args: dict[str, Any]) -> str:
        if self.reminder_store is None:
            return "Tool error: reminder store is not ready."
        text = str(args.get("text") or "").strip()
        delay_ms = args.get("delay_ms")
        if not text:
            return "Tool error: reminder text is required."
        if not isinstance(delay_ms, int) or delay_ms < 1000:
            return "Tool error: delay_ms must be an integer >= 1000."

        now_ms = (self.reminder_clock or _default_clock)()
        reminder_id = (
            self.reminder_id_factory or _default_reminder_id
        )()
        request = ReminderRequest(
            text=text,
            delay_ms=delay_ms,
            display_delay=_display_delay(delay_ms),
        )
        reminder = schedule_reminder_request(
            request,
            now_ms=now_ms,
            reminder_id=reminder_id,
        )
        self.reminder_store.add(reminder)
        return f"Reminder scheduled for {request.display_delay}: {text}."

    def _list_reminders(self, args: dict[str, Any]) -> str:
        if self.reminder_store is None:
            return "Tool error: reminder store is not ready."
        limit_raw = args.get("limit", 10)
        limit = limit_raw if isinstance(limit_raw, int) else 10
        limit = max(1, min(limit, 20))
        status_raw = str(args.get("status") or "pending").strip().lower()
        if status_raw == "pending":
            reminders = self.reminder_store.list(status=ReminderStatus.PENDING)
        elif status_raw == "fired":
            reminders = self.reminder_store.list(status=ReminderStatus.FIRED)
        elif status_raw == "all":
            reminders = self.reminder_store.list()
        else:
            return "Tool error: status must be pending, fired, or all."
        if not reminders:
            return "No matching reminders."
        now_ms = (self.reminder_clock or _default_clock)()
        lines = [
            _format_reminder(reminder, now_ms=now_ms)
            for reminder in reminders[:limit]
        ]
        return "Reminders:\n" + "\n".join(lines)

    def _cancel_reminder(self, args: dict[str, Any]) -> str:
        if self.reminder_store is None:
            return "Tool error: reminder store is not ready."
        reminder_id = str(args.get("reminder_id") or "").strip()
        if not reminder_id:
            return "Tool error: reminder_id is required."
        existing = self.reminder_store.get(reminder_id)
        if existing is None:
            return "No matching reminder."
        now_ms = (self.reminder_clock or _default_clock)()
        cancelled = self.reminder_store.cancel(reminder_id, now_ms)
        if cancelled is None:
            return f"Reminder is already resolved: {existing.text}."
        return f"Cancelled reminder {cancelled.reminder_id}: {cancelled.text}."

    async def _computer_action(self, args: dict[str, Any]) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            return "Tool error: command is required."
        composer = computer_control_composer(
            approval_store=self.approval_store,
            pending_actions=self.pending_computer_actions,
            clock=self.computer_control_clock,
        )
        result = await composer(command)
        if result is None:
            return "Tool error: command was not recognized or allowed."
        return result

    def _create_calendar_event(self, args: dict[str, Any]) -> str:
        title = str(args.get("title") or "").strip()
        start_at = str(args.get("start_at") or "").strip()
        end_at_raw = args.get("end_at")
        end_at = str(end_at_raw).strip() if end_at_raw is not None else None
        duration_raw = args.get("duration_minutes")
        duration_minutes = duration_raw if isinstance(duration_raw, int) else None
        if duration_minutes is not None:
            duration_minutes = max(1, min(duration_minutes, 1440))
        request = CalendarEventRequest(
            title=title,
            start_at=start_at,
            end_at=end_at,
            duration_minutes=duration_minutes,
            calendar=str(args.get("calendar") or "Calendar").strip() or "Calendar",
            notes=str(args.get("notes") or "").strip(),
            location=str(args.get("location") or "").strip(),
        )
        result = create_calendar_event(request, runner=self.calendar_runner)
        return result.message

    async def _get_weather(self, args: dict[str, Any]) -> str:
        location = str(args.get("location") or "").strip()
        return await get_weather(location=location, fetcher=self.weather_fetcher)

    async def _remember_fact(self, args: dict[str, Any]) -> str:
        if self.profile_store is None:
            return "Tool error: profile memory is not ready."
        key = _normalize_memory_key(str(args.get("key") or ""))
        value = str(args.get("value") or "").strip()
        if not key:
            return "Tool error: memory key is required."
        if not value:
            return "Tool error: memory value is required."

        facts = _memory_facts(self.profile_store)
        facts[key] = {
            "key": key,
            "value": value,
            "updated_at_ms": (self.reminder_clock or _default_clock)(),
        }
        self.profile_store.set("memories.facts", facts)
        await self.profile_store.flush()
        return f"Remembered {key}: {value}."

    def _suggest_memory(self, args: dict[str, Any]) -> str:
        if self.approval_store is None:
            return "Tool error: approval store is not ready."
        key = _normalize_memory_key(str(args.get("key") or ""))
        value = str(args.get("value") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not key:
            return "Tool error: memory key is required."
        if not value:
            return "Tool error: memory value is required."

        now_ms = (self.reminder_clock or _default_clock)()
        approval = create_memory_suggestion_approval(
            MemorySuggestion(key=key, value=value, reason=reason, source="llm"),
            approval_store=self.approval_store,
            profile_store=self.profile_store,
            now_ms=now_ms,
            session_id=f"deskmate-tools-{self.conversation_id}",
        )
        return f"Memory suggestion pending approval: {approval.approval_id}."

    def _recall_memory(self, args: dict[str, Any]) -> str:
        if self.profile_store is None:
            return "Tool error: profile memory is not ready."
        query = str(args.get("query") or "").strip().lower()
        limit_raw = args.get("limit", 5)
        limit = limit_raw if isinstance(limit_raw, int) else 5
        limit = max(1, min(limit, 10))
        if not query:
            return "Tool error: query is required."

        matches: list[dict[str, Any]] = []
        for fact in _memory_facts(self.profile_store).values():
            key = str(fact.get("key") or "")
            value = str(fact.get("value") or "")
            haystack = f"{key} {value}".lower()
            if query in haystack:
                matches.append(fact)
        matches.sort(key=lambda item: int(item.get("updated_at_ms") or 0), reverse=True)
        if not matches:
            return "No matching memories."
        lines = [
            f"{fact.get('key')}: {fact.get('value')}"
            for fact in matches[:limit]
        ]
        return "Memories:\n" + "\n".join(lines)

    def _list_memories(self, args: dict[str, Any]) -> str:
        if self.profile_store is None:
            return "Tool error: profile memory is not ready."
        limit_raw = args.get("limit", 10)
        limit = limit_raw if isinstance(limit_raw, int) else 10
        limit = max(1, min(limit, 20))
        facts = list(_memory_facts(self.profile_store).values())
        facts.sort(key=lambda item: int(item.get("updated_at_ms") or 0), reverse=True)
        if not facts:
            return "No durable memories."
        lines = [
            f"{fact.get('key')}: {fact.get('value')}"
            for fact in facts[:limit]
        ]
        return "Durable memories:\n" + "\n".join(lines)

    async def _forget_memory(self, args: dict[str, Any]) -> str:
        if self.profile_store is None:
            return "Tool error: profile memory is not ready."
        query = str(args.get("query") or "").strip()
        if not query:
            return "Tool error: query is required."

        facts = _memory_facts(self.profile_store)
        if not facts:
            return "No matching memories."

        normalized = _normalize_memory_key(query)
        keys_to_delete: list[str] = []
        if normalized in facts:
            keys_to_delete = [normalized]
        else:
            needle = query.lower()
            for key, fact in facts.items():
                value = str(fact.get("value") or "")
                if needle in f"{key} {value}".lower():
                    keys_to_delete.append(key)
        if not keys_to_delete:
            return "No matching memories."

        removed: list[str] = []
        for key in keys_to_delete:
            fact = facts.pop(key, None)
            if fact is not None:
                removed.append(f"{fact.get('key', key)}: {fact.get('value', '')}")
        self.profile_store.set("memories.facts", facts)
        await self.profile_store.flush()
        return "Forgot memories:\n" + "\n".join(removed)

    async def _search_chat_memory(self, args: dict[str, Any]) -> str:
        if self.chat_memory is None:
            return "Tool error: chat memory is not ready."
        query = str(args.get("query") or "").strip()
        limit_raw = args.get("limit", 5)
        limit = limit_raw if isinstance(limit_raw, int) else 5
        limit = max(1, min(limit, 10))
        if not query:
            return "Tool error: query is required."

        matches = await self.chat_memory.search(
            self.conversation_id,
            query=query,
            limit=limit,
        )
        if not matches:
            return "No matching chat messages."
        return "Chat memory matches:\n" + "\n".join(
            _format_chat_match(message) for message in matches
        )

    async def _create_task(self, args: dict[str, Any]) -> str:
        if self.task_store is None:
            return "Tool error: task store is not ready."
        title = str(args.get("title") or "").strip()
        notes = str(args.get("notes") or "").strip()
        status = str(args.get("status") or "open").strip().lower()
        if not title:
            return "Tool error: title is required."
        if status not in {"open", "in_progress", "done", "cancelled"}:
            return "Tool error: status must be open, in_progress, done, or cancelled."
        try:
            task = await self.task_store.create(
                conversation_id=self.conversation_id,
                title=title,
                notes=notes,
                status=status,  # type: ignore[arg-type]
            )
        except ValueError as exc:
            return f"Tool error: {exc}"
        return "Task created:\n" + _format_task(task)

    def _suggest_task(self, args: dict[str, Any]) -> str:
        if self.approval_store is None:
            return "Tool error: approval store is not ready."
        title = str(args.get("title") or "").strip()
        notes = str(args.get("notes") or "").strip()
        status = str(args.get("status") or "open").strip().lower()
        reason = str(args.get("reason") or "").strip()
        if not title:
            return "Tool error: title is required."
        if status not in {"open", "in_progress"}:
            return "Tool error: status must be open or in_progress."
        try:
            approval = create_task_suggestion_approval(
                TaskSuggestion(
                    title=title,
                    notes=notes,
                    status=status,  # type: ignore[arg-type]
                    reason=reason,
                    source="llm",
                ),
                approval_store=self.approval_store,
                now_ms=(self.reminder_clock or _default_clock)(),
                conversation_id=self.conversation_id,
                session_id=f"deskmate-tools-{self.conversation_id}",
            )
        except ValueError as exc:
            return f"Tool error: {exc}"
        return f"Task suggestion pending approval: {approval.approval_id}."

    async def _list_tasks(self, args: dict[str, Any]) -> str:
        if self.task_store is None:
            return "Tool error: task store is not ready."
        limit = _limit(args.get("limit"), default=10, maximum=20)
        status = str(args.get("status") or "active").strip().lower()
        if status not in {"active", "open", "in_progress", "done", "cancelled", "all"}:
            return "Tool error: status must be active, open, in_progress, done, cancelled, or all."
        try:
            tasks = await self.task_store.list(
                self.conversation_id,
                status=status,  # type: ignore[arg-type]
                limit=limit,
            )
        except ValueError as exc:
            return f"Tool error: {exc}"
        if not tasks:
            return "No matching tasks."
        return "Tasks:\n" + "\n".join(_format_task(task) for task in tasks)

    async def _search_tasks(self, args: dict[str, Any]) -> str:
        if self.task_store is None:
            return "Tool error: task store is not ready."
        query = str(args.get("query") or "").strip()
        if not query:
            return "Tool error: query is required."
        limit = _limit(args.get("limit"), default=10, maximum=20)
        status = str(args.get("status") or "all").strip().lower()
        if status not in {"active", "open", "in_progress", "done", "cancelled", "all"}:
            return "Tool error: status must be active, open, in_progress, done, cancelled, or all."
        try:
            tasks = await self.task_store.search(
                self.conversation_id,
                query=query,
                status=status,  # type: ignore[arg-type]
                limit=limit,
            )
        except ValueError as exc:
            return f"Tool error: {exc}"
        if not tasks:
            return "No matching tasks."
        return "Tasks:\n" + "\n".join(_format_task(task) for task in tasks)

    async def _update_task(self, args: dict[str, Any]) -> str:
        if self.task_store is None:
            return "Tool error: task store is not ready."
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return "Tool error: task_id is required."
        title = args.get("title")
        notes = args.get("notes")
        status_raw = args.get("status")
        status = str(status_raw).strip().lower() if status_raw is not None else None
        if status is not None and status not in {"open", "in_progress", "done", "cancelled"}:
            return "Tool error: status must be open, in_progress, done, or cancelled."
        if title is None and notes is None and status is None:
            return "Tool error: provide title, notes, or status."
        try:
            task = await self.task_store.update(
                task_id,
                conversation_id=self.conversation_id,
                title=str(title) if title is not None else None,
                notes=str(notes) if notes is not None else None,
                status=status,  # type: ignore[arg-type]
            )
        except ValueError as exc:
            return f"Tool error: {exc}"
        if task is None:
            return "No matching task."
        return "Task updated:\n" + _format_task(task)

    async def _update_task_steps(self, args: dict[str, Any]) -> str:
        if self.task_store is None:
            return "Tool error: task store is not ready."
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return "Tool error: task_id is required."
        raw_steps = args.get("steps")
        if not isinstance(raw_steps, list):
            return "Tool error: steps must be an array."
        for item in raw_steps:
            if not isinstance(item, dict):
                return "Tool error: each step must be an object."
        try:
            steps = await self.task_store.replace_steps(
                task_id,
                raw_steps,
                conversation_id=self.conversation_id,
            )
        except ValueError as exc:
            return f"Tool error: {exc}"
        if steps is None:
            return "No matching task."
        if not steps:
            return f"Task steps updated for {task_id}: none."
        return (
            f"Task steps updated for {task_id}:\n"
            + "\n".join(_format_task_step(step) for step in steps)
        )

    async def _list_task_steps(self, args: dict[str, Any]) -> str:
        if self.task_store is None:
            return "Tool error: task store is not ready."
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return "Tool error: task_id is required."
        task = await self.task_store.get(task_id)
        if task is None or task.conversation_id != self.conversation_id:
            return "No matching task."
        steps = await self.task_store.list_steps(
            task_id,
            conversation_id=self.conversation_id,
        )
        if not steps:
            return f"Task steps for {task_id}: none."
        return (
            f"Task steps for {task_id}:\n"
            + "\n".join(_format_task_step(step) for step in steps)
        )

    async def _recent_tool_actions(self, args: dict[str, Any]) -> str:
        if self.tool_action_log is None:
            return "Tool error: tool action log is not ready."
        limit_raw = args.get("limit", 5)
        limit = limit_raw if isinstance(limit_raw, int) else 5
        limit = max(1, min(limit, 10))
        query = str(args.get("query") or "").strip()
        tool_name = str(args.get("tool_name") or "").strip() or None
        task_id = str(args.get("task_id") or "").strip() or None
        status_raw = str(args.get("status") or "").strip()
        status = status_raw if status_raw in {"completed", "failed", "duplicate"} else None
        if status_raw and status is None:
            return "Tool error: status must be completed, failed, or duplicate."

        if query:
            records = await self.tool_action_log.search(
                self.conversation_id,
                query=query,
                limit=limit,
            )
            if tool_name is not None:
                records = [record for record in records if record.tool_name == tool_name]
            if task_id is not None:
                records = [record for record in records if record.task_id == task_id]
            if status is not None:
                records = [record for record in records if record.status == status]
        else:
            records = await self.tool_action_log.recent(
                self.conversation_id,
                tool_name=tool_name,
                task_id=task_id,
                status=status,
                limit=limit,
            )
        if not records:
            return "No matching tool actions."
        return "Recent tool actions:\n" + "\n".join(
            _format_tool_action(record) for record in records[:limit]
        )

    async def _recent_tool_tasks(self, args: dict[str, Any]) -> str:
        if self.tool_action_log is None:
            return "Tool error: tool action log is not ready."
        limit_raw = args.get("limit", 5)
        limit = limit_raw if isinstance(limit_raw, int) else 5
        limit = max(1, min(limit, 10))
        query = str(args.get("query") or "").strip()
        status_raw = str(args.get("status") or "").strip()
        if status_raw and status_raw not in {"running", "completed", "failed"}:
            return "Tool error: status must be running, completed, or failed."

        if query:
            tasks = await self.tool_action_log.search_tasks(
                self.conversation_id,
                query=query,
                limit=limit,
            )
        else:
            tasks = await self.tool_action_log.recent_tasks(
                self.conversation_id,
                limit=limit,
            )
        if status_raw:
            tasks = [task for task in tasks if task.status == status_raw]
        if not tasks:
            return "No matching tool tasks."
        return "Recent tool tasks:\n" + "\n".join(
            _format_tool_task(task) for task in tasks[:limit]
        )

    async def _task_context(self, args: dict[str, Any]) -> str:
        if self.task_store is None:
            return "Tool error: task store is not ready."
        if self.tool_action_log is None:
            return "Tool error: tool action log is not ready."
        limit = _limit(args.get("limit"), default=5, maximum=10)
        task_id = str(args.get("task_id") or "").strip()
        query = str(args.get("query") or "").strip()
        if not task_id and not query:
            return "Tool error: task_id or query is required."

        task = None
        if task_id:
            task = await self.task_store.get(task_id)
            if task is not None and task.conversation_id != self.conversation_id:
                task = None
        if task is None and query:
            matches = await self.task_store.search(
                self.conversation_id,
                query=query,
                status="all",
                limit=1,
            )
            task = matches[0] if matches else None
        if task is None:
            return "No matching task."

        steps = await self.task_store.list_steps(
            task.task_id,
            conversation_id=self.conversation_id,
        )
        search_terms = _task_context_terms(task.title, task.notes, steps)
        tool_tasks = []
        tool_actions = []
        tool_lessons = []
        seen_tool_task_ids: set[str] = set()
        seen_tool_action_ids: set[str] = set()
        seen_lesson_ids: set[str] = set()
        for item in await self.tool_action_log.recent(
            self.conversation_id,
            task_id=task.task_id,
            limit=limit,
        ):
            if item.tool_call_id in seen_tool_action_ids:
                continue
            tool_actions.append(item)
            seen_tool_action_ids.add(item.tool_call_id)
            if len(tool_actions) >= limit:
                break
        for item in await self.tool_action_log.recent_lessons(
            self.conversation_id,
            task_id=task.task_id,
            limit=limit,
        ):
            if item.lesson_key in seen_lesson_ids:
                continue
            tool_lessons.append(item)
            seen_lesson_ids.add(item.lesson_key)
            if len(tool_lessons) >= limit:
                break
        for term in search_terms:
            if len(tool_tasks) < limit:
                for item in await self.tool_action_log.search_tasks(
                    self.conversation_id,
                    query=term,
                    limit=limit,
                ):
                    if item.task_id in seen_tool_task_ids:
                        continue
                    tool_tasks.append(item)
                    seen_tool_task_ids.add(item.task_id)
                    if len(tool_tasks) >= limit:
                        break
            if len(tool_actions) < limit:
                for item in await self.tool_action_log.search(
                    self.conversation_id,
                    query=term,
                    limit=limit,
                ):
                    if item.tool_call_id in seen_tool_action_ids:
                        continue
                    tool_actions.append(item)
                    seen_tool_action_ids.add(item.tool_call_id)
                    if len(tool_actions) >= limit:
                        break
            if len(tool_lessons) < limit:
                for item in await self.tool_action_log.search_lessons(
                    self.conversation_id,
                    query=term,
                    limit=limit,
                ):
                    if item.lesson_key in seen_lesson_ids:
                        continue
                    tool_lessons.append(item)
                    seen_lesson_ids.add(item.lesson_key)
                    if len(tool_lessons) >= limit:
                        break
            if (
                len(tool_tasks) >= limit
                and len(tool_actions) >= limit
                and len(tool_lessons) >= limit
            ):
                break

        lines = ["Task context:", _format_task(task)]
        lines.append("Task steps:")
        lines.extend(_format_task_step(item) for item in steps)
        if not steps:
            lines.append("none")
        lines.append("Related tool tasks:")
        lines.extend(
            _format_tool_task(item) for item in tool_tasks[:limit]
        )
        if not tool_tasks:
            lines.append("none")
        lines.append("Related tool actions:")
        lines.extend(
            _format_tool_action(item) for item in tool_actions[:limit]
        )
        if not tool_actions:
            lines.append("none")
        lines.append("Related tool lessons:")
        lines.extend(
            _format_tool_lesson(item) for item in tool_lessons[:limit]
        )
        if not tool_lessons:
            lines.append("none")
        return "\n".join(lines)

    async def _tool_task_details(self, args: dict[str, Any]) -> str:
        if self.tool_action_log is None:
            return "Tool error: tool action log is not ready."
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return "Tool error: task_id is required."
        limit_raw = args.get("limit", 10)
        limit = limit_raw if isinstance(limit_raw, int) else 10
        limit = max(1, min(limit, 20))

        task = await self.tool_action_log.get_task(task_id)
        if task is None or task.conversation_id != self.conversation_id:
            return "No matching tool task."
        actions = await self.tool_action_log.recent(
            self.conversation_id,
            task_id=task_id,
            limit=limit,
        )
        if not actions:
            return f"Tool task details:\n{_format_tool_task(task)}\nActions: none"
        return (
            f"Tool task details:\n{_format_tool_task(task)}\nActions:\n"
            + "\n".join(f"- {_format_tool_action(action)}" for action in actions)
        )

    async def _recent_tool_lessons(self, args: dict[str, Any]) -> str:
        if self.tool_action_log is None:
            return "Tool error: tool action log is not ready."
        limit = _limit(args.get("limit"), default=5, maximum=10)
        query = str(args.get("query") or "").strip()
        if query:
            lessons = await self.tool_action_log.search_lessons(
                self.conversation_id,
                query=query,
                limit=limit,
            )
        else:
            lessons = await self.tool_action_log.recent_lessons(
                self.conversation_id,
                limit=limit,
            )
        if not lessons:
            return "No matching tool lessons."
        return "Tool lessons:\n" + "\n".join(
            _format_tool_lesson(lesson) for lesson in lessons[:limit]
        )


def _parse_arguments(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("arguments must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


def _display_delay(delay_ms: int) -> str:
    seconds = delay_ms // 1000
    if delay_ms % 3_600_000 == 0:
        hours = delay_ms // 3_600_000
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if delay_ms % 60_000 == 0:
        minutes = delay_ms // 60_000
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _default_clock() -> int:
    import time

    return int(time.time() * 1000)


def _default_reminder_id() -> str:
    return "tool-reminder-" + uuid.uuid4().hex[:12]


def _memory_facts(profile_store: ProfileStore) -> dict[str, dict[str, Any]]:
    raw = profile_store.get("memories.facts", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key)] = dict(value)
    return out


def _normalize_memory_key(value: str) -> str:
    import re

    key = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    key = re.sub(r"_+", "_", key).strip("_")
    return key[:64]


def _format_chat_match(message: Any) -> str:
    role = str(getattr(message, "role", "") or "message")
    content = str(getattr(message, "content", "") or "").strip()
    content = " ".join(content.split())
    if len(content) > 240:
        content = content[:237].rstrip() + "..."
    return f"{role}: {content}"


def _format_reminder(reminder: Reminder, *, now_ms: int) -> str:
    delta_ms = reminder.due_at_ms - now_ms
    if reminder.status is ReminderStatus.PENDING:
        when = (
            f"due in {_display_delay(delta_ms)}"
            if delta_ms >= 0
            else f"overdue by {_display_delay(abs(delta_ms))}"
        )
    elif reminder.status is ReminderStatus.FIRED:
        when = "fired"
    elif reminder.status is ReminderStatus.CANCELLED:
        when = "cancelled"
    else:
        when = "dismissed"
    return f"{reminder.reminder_id} [{reminder.status.value}, {when}]: {reminder.text}"


def _format_tool_action(record: Any) -> str:
    return format_tool_action_summary(record)


def _format_tool_task(record: Any) -> str:
    return format_tool_task_summary(record)


def _format_tool_lesson(record: Any) -> str:
    return format_tool_lesson(record)


def _format_task(record: Any) -> str:
    return format_deskmate_task(record)


def _format_task_step(record: Any) -> str:
    return format_deskmate_task_step(record)


def _task_context_terms(title: str, notes: str, steps: list[Any] | None = None) -> list[str]:
    step_terms: list[str] = []
    for step in (steps or [])[:8]:
        active_form = str(getattr(step, "active_form", "") or "")
        content = str(getattr(step, "content", "") or "")
        if active_form:
            step_terms.append(active_form)
        if content and content != active_form:
            step_terms.append(content)
    raw_terms = [
        title,
        notes,
        *step_terms,
        *(word for term in step_terms for word in term.split()),
        *title.split(),
        *notes.split(),
    ]
    seen: set[str] = set()
    terms: list[str] = []
    for raw in raw_terms:
        term = " ".join(str(raw or "").strip().split())
        if len(term) < 3:
            continue
        lowered = term.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(term[:120])
        if len(terms) >= 12:
            break
    return terms


def _limit(value: Any, *, default: int, maximum: int) -> int:
    raw = value if isinstance(value, int) else default
    return max(1, min(raw, maximum))


__all__ = ["DESKMATE_TOOLS", "DeskmateToolExecutor"]
