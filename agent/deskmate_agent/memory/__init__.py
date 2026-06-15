"""Three-tier memory (V10 L3-B6 / L3-B7).

- :class:`ShortTermMemory` — in-memory deque, last N messages of a session.
- :class:`SessionMemory` — mid-term aiosqlite store of session summaries
  with WAL journaling + ``synchronous=NORMAL`` so writes don't block the
  event loop.
- :class:`ProfileStore` — long-term key-value profile, loaded once on open
  and flushed *lazily* (delayed commit) so per-turn updates never hit disk.
"""

from __future__ import annotations

from .chat import ChatMemory, ChatSummary
from .coding_session_store import CodingSession, CodingSessionStore
from .profile import ProfileStore
from .session import SessionMemory, SessionSummary, now_ms
from .short import ShortTermMemory
from .suggestions import (
    MEMORY_SUGGESTION_KIND,
    MemorySuggestion,
    create_memory_suggestion_approval,
    extract_memory_suggestion,
    memory_facts,
    memory_suggestion_composer,
    memory_suggestion_streaming_composer,
    normalize_memory_key,
    resolve_memory_suggestion,
    suggest_memory_from_text,
)
from .task_suggestions import (
    TASK_SUGGESTION_KIND,
    TaskSuggestion,
    create_task_suggestion_approval,
    resolve_task_suggestion,
)
from .tasks import (
    DeskmateTaskRecord,
    DeskmateTaskStatus,
    DeskmateTaskStep,
    DeskmateTaskStepStatus,
    DeskmateTaskStore,
    format_deskmate_task,
    format_deskmate_task_step,
)
from .tool_actions import (
    ToolActionLog,
    ToolActionRecord,
    ToolActionStatus,
    ToolLessonRecord,
    ToolTaskRecord,
    ToolTaskStatus,
    format_tool_action_summary,
    format_tool_lesson,
    format_tool_task_summary,
    sanitize_tool_arguments,
    summarize_tool_action,
    tool_action_summary,
)
from .types import Message

__all__ = [
    "ChatMemory",
    "ChatSummary",
    "CodingSession",
    "CodingSessionStore",
    "DeskmateTaskRecord",
    "DeskmateTaskStep",
    "DeskmateTaskStepStatus",
    "DeskmateTaskStatus",
    "DeskmateTaskStore",
    "MEMORY_SUGGESTION_KIND",
    "Message",
    "MemorySuggestion",
    "ProfileStore",
    "SessionMemory",
    "SessionSummary",
    "ShortTermMemory",
    "ToolActionLog",
    "ToolActionRecord",
    "ToolActionStatus",
    "ToolLessonRecord",
    "ToolTaskRecord",
    "ToolTaskStatus",
    "TASK_SUGGESTION_KIND",
    "TaskSuggestion",
    "create_memory_suggestion_approval",
    "create_task_suggestion_approval",
    "extract_memory_suggestion",
    "format_tool_action_summary",
    "format_deskmate_task",
    "format_deskmate_task_step",
    "format_tool_lesson",
    "format_tool_task_summary",
    "memory_facts",
    "memory_suggestion_composer",
    "memory_suggestion_streaming_composer",
    "now_ms",
    "normalize_memory_key",
    "resolve_memory_suggestion",
    "resolve_task_suggestion",
    "sanitize_tool_arguments",
    "summarize_tool_action",
    "suggest_memory_from_text",
    "tool_action_summary",
]
