"""Deterministic task/todo control for explicit user requests."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from ..dispatcher import ReplyComposer, StreamingReplyComposer
from ..logging_setup import get_logger
from ..memory import (
    DeskmateTaskRecord,
    DeskmateTaskStep,
    DeskmateTaskStore,
    ToolActionLog,
    ToolActionRecord,
    format_deskmate_task,
    format_deskmate_task_step,
    format_tool_action_summary,
    format_tool_lesson,
    format_tool_task_summary,
    now_ms,
)

_LOGGER = get_logger(__name__)

TaskCommandKind = Literal[
    "create",
    "list",
    "search",
    "update",
    "steps",
    "advance",
    "detail",
    "resume",
    "start",
    "pause",
]


@dataclass(frozen=True)
class TaskCommand:
    kind: TaskCommandKind
    title: str = ""
    query: str = ""
    task_id: str = ""
    status: str = ""
    notes: str = ""
    steps: tuple[dict[str, object], ...] = ()


_CREATE_PATTERNS = (
    re.compile(r"^(?:add|create|track)\s+(?:a\s+)?(?:task|todo)\s+(?P<title>.+)$", re.I),
    re.compile(r"^(?:todo|task):\s*(?P<title>.+)$", re.I),
    re.compile(r"^(?:添加|新增|创建|记录)\s*(?:任务|待办)\s*(?P<title>.+)$", re.I),
)
_LIST_PATTERNS = (
    re.compile(r"^(?:list|show)\s+(?:my\s+)?(?:tasks|todos)\??$", re.I),
    re.compile(r"^(?:what tasks do i have|what todos do i have)\??$", re.I),
    re.compile(r"^(?:列出|显示|查看)\s*(?:任务|待办)\??$", re.I),
)
_SEARCH_PATTERNS = (
    re.compile(r"^(?:search|find)\s+(?:tasks|todos)\s+(?:for\s+)?(?P<query>.+)$", re.I),
    re.compile(r"^(?:搜索|查找)\s*(?:任务|待办)\s*(?P<query>.+)$", re.I),
)
_DETAIL_PATTERNS = (
    re.compile(
        r"^(?:show|view|open|inspect)\s+(?:task|todo)\s+(?P<query>.+)\??$",
        re.I,
    ),
    re.compile(
        r"^(?:task|todo)\s+(?:detail|details|status)\s+(?P<query>.+)\??$",
        re.I,
    ),
    re.compile(
        r"^(?:what(?:'s| is)\s+next\s+for)\s+(?:task|todo)\s+(?P<query>.+)\??$",
        re.I,
    ),
    re.compile(r"^(?:查看|打开)\s*(?:任务|待办)\s*(?P<query>.+)$", re.I),
    re.compile(r"^(?:任务|待办)\s*(?P<query>.+?)\s*(?:详情|状态)$", re.I),
)
_UPDATE_PATTERNS = (
    re.compile(
        r"^(?:mark|set)\s+(?:task|todo)\s+(?P<task_id>[A-Za-z0-9_.:-]+)\s+"
        r"(?:as\s+)?(?P<status>open|in_progress|in progress|done|cancelled|canceled)$",
        re.I,
    ),
    re.compile(
        r"^(?:complete|finish|done)\s+(?:task|todo)\s+(?P<task_id>[A-Za-z0-9_.:-]+)$",
        re.I,
    ),
    re.compile(
        r"^(?:cancel|delete)\s+(?:task|todo)\s+(?P<task_id>[A-Za-z0-9_.:-]+)$",
        re.I,
    ),
    re.compile(
        r"^(?:完成|结束)\s*(?:任务|待办)\s*(?P<task_id>[A-Za-z0-9_.:-]+)$",
        re.I,
    ),
    re.compile(
        r"^(?:取消|删除)\s*(?:任务|待办)\s*(?P<task_id>[A-Za-z0-9_.:-]+)$",
        re.I,
    ),
)
_UPDATE_QUERY_PATTERNS = (
    re.compile(
        r"^(?:mark|set)\s+(?:task|todo)\s+(?P<query>.+?)\s+"
        r"(?:as\s+)?(?P<status>open|in_progress|in progress|done|cancelled|canceled)$",
        re.I,
    ),
    re.compile(
        r"^(?:complete|finish|done)\s+(?:task|todo)\s+(?P<query>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:cancel|delete)\s+(?:task|todo)\s+(?P<query>.+)$",
        re.I,
    ),
    re.compile(r"^(?:完成|结束)\s*(?:任务|待办)\s*(?P<query>.+)$", re.I),
    re.compile(r"^(?:取消|删除)\s*(?:任务|待办)\s*(?P<query>.+)$", re.I),
)
_STEPS_PATTERNS = (
    re.compile(
        r"^(?:plan|outline)\s+(?:task|todo)\s+(?P<query>.+?)\s*[:：]\s*(?P<steps>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:set|update)\s+(?:task|todo)\s+steps\s+(?P<query>.+?)\s*[:：]\s*(?P<steps>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:规划|计划|拆解)\s*(?:任务|待办)\s*(?P<query>.+?)\s*[:：]\s*(?P<steps>.+)$",
        re.I,
    ),
)
_ADVANCE_STEP_PATTERNS = (
    re.compile(
        r"^(?:next|advance|continue)\s+(?:step\s+)?(?:task|todo)\s+(?P<query>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:complete|finish|done)\s+(?:current\s+)?step\s+(?:for\s+)?(?:task|todo)\s+(?P<query>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:推进|继续|完成当前步骤)\s*(?:任务|待办)\s*(?P<query>.+)$",
        re.I,
    ),
)
_RESUME_CURRENT_PATTERNS = (
    re.compile(
        r"^(?:continue|resume|pick up|carry on|keep going)"
        r"(?:\s+(?:the\s+)?(?:(?:current|active|last|tracked)\s+)?"
        r"(?:task|todo|work))?\??$",
        re.I,
    ),
    re.compile(
        r"^(?:what(?:'s| is)\s+next|show\s+next)\s+(?:for\s+)?"
        r"(?:the\s+)?(?:current|active)\s+(?:task|todo)\??$",
        re.I,
    ),
    re.compile(r"^(?:继续|接着|恢复)(?:当前|刚才|这个)?(?:任务|待办|工作)?吧?$", re.I),
    re.compile(r"^(?:当前|刚才|这个)(?:任务|待办)(?:下一步|详情|状态)$", re.I),
)
_START_PATTERNS = (
    re.compile(r"^(?:start|resume|begin)\s+(?:task|todo)\s+(?P<query>.+)$", re.I),
    re.compile(r"^(?:work on|focus)\s+(?:task|todo)\s+(?P<query>.+)$", re.I),
    re.compile(r"^(?:开始|恢复)\s*(?:任务|待办)\s*(?P<query>.+)$", re.I),
)
_PAUSE_PATTERNS = (
    re.compile(r"^(?:pause|hold|stop)\s+(?:task|todo)\s+(?P<query>.+)$", re.I),
    re.compile(r"^(?:park|defer)\s+(?:task|todo)\s+(?P<query>.+)$", re.I),
    re.compile(r"^(?:暂停|搁置|稍后继续)\s*(?:任务|待办)\s*(?P<query>.+)$", re.I),
)


def task_control_composer(
    *,
    task_store: DeskmateTaskStore | None = None,
    tool_action_log: ToolActionLog | None = None,
    conversation_id: str = "default",
    fallback: ReplyComposer | None = None,
) -> ReplyComposer:
    async def compose(text: str) -> str | None:
        command = parse_task_command(text)
        if command is None:
            return await fallback(text) if fallback is not None else None
        return await run_task_command(
            command,
            task_store=task_store,
            tool_action_log=tool_action_log,
            conversation_id=conversation_id,
        )

    return compose


def task_control_streaming_composer(
    *,
    task_store: DeskmateTaskStore | None = None,
    tool_action_log: ToolActionLog | None = None,
    conversation_id: str = "default",
    fallback: StreamingReplyComposer | None = None,
) -> StreamingReplyComposer:
    async def compose(text: str) -> AsyncIterator[str]:
        command = parse_task_command(text)
        if command is not None:
            yield await run_task_command(
                command,
                task_store=task_store,
                tool_action_log=tool_action_log,
                conversation_id=conversation_id,
            )
            return
        if fallback is None:
            return
        async for chunk in fallback(text):
            yield chunk

    return compose


def parse_task_command(text: str) -> TaskCommand | None:
    stripped = " ".join(text.strip().split())
    if not stripped:
        return None
    for pattern in _RESUME_CURRENT_PATTERNS:
        if pattern.match(stripped):
            return TaskCommand("resume")
    for pattern in _PAUSE_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("query"))
            return TaskCommand("pause", query=query) if query else None
    for pattern in _START_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("query"))
            return TaskCommand("start", query=query) if query else None
    for pattern in _ADVANCE_STEP_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("query"))
            return TaskCommand("advance", query=query) if query else None
    for pattern in _STEPS_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("query"))
            steps = _parse_step_items(match.group("steps"))
            return TaskCommand("steps", query=query, steps=steps) if query and steps else None
    for pattern in _LIST_PATTERNS:
        if pattern.match(stripped):
            return TaskCommand("list")
    for pattern in _DETAIL_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("query"))
            return TaskCommand("detail", query=query) if query else None
    for pattern in _SEARCH_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("query"))
            return TaskCommand("search", query=query) if query else None
    for pattern in _UPDATE_PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        task_id = _clean_text(match.group("task_id"))
        status = _status_for_update(stripped, match.groupdict().get("status"))
        return TaskCommand("update", task_id=task_id, status=status) if task_id else None
    for pattern in _UPDATE_QUERY_PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        query = _clean_text(match.group("query"))
        status = _status_for_update(stripped, match.groupdict().get("status"))
        return TaskCommand("update", query=query, status=status) if query else None
    for pattern in _CREATE_PATTERNS:
        match = pattern.match(stripped)
        if match:
            title, notes = _split_title_notes(match.group("title"))
            return TaskCommand("create", title=title, notes=notes) if title else None
    return None


async def run_task_command(
    command: TaskCommand,
    *,
    task_store: DeskmateTaskStore | None,
    tool_action_log: ToolActionLog | None = None,
    conversation_id: str = "default",
) -> str:
    started_at_ms = now_ms()
    if task_store is None:
        result = "I can manage tasks once task memory is ready."
        await _audit_task_command(
            command,
            result=result,
            status="failed",
            started_at_ms=started_at_ms,
            tool_action_log=tool_action_log,
            conversation_id=conversation_id,
        )
        return result
    if command.kind == "create":
        task = await task_store.create(
            conversation_id=conversation_id,
            title=command.title,
            notes=command.notes,
        )
        result = "Task created:\n" + format_deskmate_task(task)
    elif command.kind == "list":
        tasks = await task_store.list(conversation_id, status="active", limit=10)
        result = await _format_task_list(
            tasks,
            task_store=task_store,
            conversation_id=conversation_id,
        )
    elif command.kind == "search":
        tasks = await task_store.search(
            conversation_id,
            query=command.query,
            status="all",
            limit=10,
        )
        result = await _format_task_list(
            tasks,
            task_store=task_store,
            conversation_id=conversation_id,
        )
    elif command.kind == "steps":
        result = await _replace_matching_task_steps(
            command.query,
            task_store=task_store,
            conversation_id=conversation_id,
            steps=list(command.steps),
        )
    elif command.kind == "advance":
        result = await _advance_matching_task_step(
            command.query,
            task_store=task_store,
            conversation_id=conversation_id,
        )
    elif command.kind == "detail":
        result = await _task_detail(
            command.query,
            task_store=task_store,
            conversation_id=conversation_id,
        )
    elif command.kind == "resume":
        result = await _resume_current_task(
            task_store=task_store,
            tool_action_log=tool_action_log,
            conversation_id=conversation_id,
        )
    elif command.kind == "start":
        result = await _start_matching_task(
            command.query,
            task_store=task_store,
            conversation_id=conversation_id,
        )
    elif command.kind == "pause":
        result = await _pause_matching_task(
            command.query,
            task_store=task_store,
            conversation_id=conversation_id,
        )
    else:
        target = command.task_id or command.query
        task = await _update_matching_task(
            target,
            conversation_id=conversation_id,
            task_store=task_store,
            status=command.status,  # type: ignore[arg-type]
        )
        if isinstance(task, str):
            result = task
        else:
            steps = await task_store.list_steps(
                task.task_id,
                conversation_id=conversation_id,
            )
            result = "Task updated:\n" + _format_task_with_steps(task, steps)
    await _audit_task_command(
        command,
        result=result,
        status=_task_command_audit_status(command, result),
        started_at_ms=started_at_ms,
        tool_action_log=tool_action_log,
        conversation_id=conversation_id,
    )
    return result


async def _audit_task_command(
    command: TaskCommand,
    *,
    result: str,
    status: Literal["completed", "failed"],
    started_at_ms: int,
    tool_action_log: ToolActionLog | None,
    conversation_id: str,
) -> None:
    if tool_action_log is None:
        return
    completed_at_ms = now_ms()
    action = _task_command_action_name(command)
    target = _task_command_target(command)
    arguments = {
        key: value
        for key, value in {
            "kind": command.kind,
            "task_id": command.task_id,
            "query": command.query or command.title or command.task_id,
            "title": command.title,
            "status": command.status,
            "notes": command.notes,
            "step_count": len(command.steps),
        }.items()
        if value not in ("", 0, ())
    }
    record = ToolActionRecord(
        conversation_id=conversation_id,
        tool_call_id=f"task-command:{uuid.uuid4().hex[:12]}",
        task_id=_task_command_record_task_id(command, result=result, status=status),
        tool_name="deskmate_task_command",
        arguments=arguments,
        result=result,
        status=status,
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        summary={
            "action": action,
            "target": target,
            "outcome": _compact_result(result),
            "needs_user": False,
        },
    )
    try:
        await tool_action_log.append(record)
    except Exception as exc:  # noqa: BLE001 — audit must never break task control
        _LOGGER.warning(
            "task_control.audit_failed",
            kind=command.kind,
            target=target,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _task_command_audit_status(
    command: TaskCommand,
    result: str,
) -> Literal["completed", "failed"]:
    if command.kind in {"list", "search"}:
        return "completed"
    failed_prefixes = (
        "I can manage tasks once task memory is ready.",
        "No matching task.",
        "Multiple matching tasks:",
        "Task already has no pending steps.",
        "Task has no steps.",
        "Task steps error:",
        "No active task.",
    )
    if result.startswith(failed_prefixes):
        return "failed"
    return "completed"


def _task_command_action_name(command: TaskCommand) -> str:
    if command.kind == "update" and command.status == "done":
        return "task.complete"
    if command.kind == "update" and command.status == "cancelled":
        return "task.cancel"
    return f"task.{command.kind}"


def _task_command_target(command: TaskCommand) -> str:
    return _compact_result(
        command.task_id
        or command.query
        or command.title
        or command.status
        or command.kind,
        limit=120,
    )


def _task_command_record_task_id(
    command: TaskCommand,
    *,
    result: str,
    status: Literal["completed", "failed"],
) -> str | None:
    if status == "failed":
        return None
    inferred = _task_id_from_single_task_result(result)
    if inferred is not None:
        return inferred
    return command.task_id or None


def _task_id_from_single_task_result(result: str) -> str | None:
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    if not lines:
        return None
    for prefix in (
        "Task created:",
        "Task updated:",
        "Task detail:",
        "Task started:",
        "Task paused:",
        "Task completed:",
        "Current task context:",
    ):
        if lines[0] == prefix and len(lines) > 1:
            return _leading_task_id(lines[1])
    match = re.match(r"^Task step advanced for (?P<task_id>\S+):$", lines[0])
    if match:
        return match.group("task_id")
    match = re.match(r"^Task steps updated for (?P<task_id>\S+):", lines[0])
    if match:
        return match.group("task_id")
    return None


def _leading_task_id(line: str) -> str | None:
    match = re.match(r"^(?P<task_id>\S+)\s+\[", line)
    return match.group("task_id") if match else None


def _compact_result(value: str, *, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


async def _format_task_list(
    tasks: list[DeskmateTaskRecord],
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
) -> str:
    if not tasks:
        return "No matching tasks."
    lines: list[str] = []
    for task in tasks:
        steps = await task_store.list_steps(
            task.task_id,
            conversation_id=conversation_id,
        )
        lines.append(_format_task_with_steps(task, steps))
    return "Tasks:\n" + "\n".join(lines)


async def _task_detail(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
) -> str:
    task = await _resolve_one_task(
        target,
        task_store=task_store,
        conversation_id=conversation_id,
        status="active",
    )
    if isinstance(task, str):
        return task
    steps = await task_store.list_steps(
        task.task_id,
        conversation_id=conversation_id,
    )
    return "Task detail:\n" + _format_task_with_steps(task, steps)


async def _resume_current_task(
    *,
    task_store: DeskmateTaskStore,
    tool_action_log: ToolActionLog | None,
    conversation_id: str,
) -> str:
    tasks = await task_store.list(
        conversation_id,
        status="active",
        limit=5,
    )
    if not tasks:
        return "No active task."
    task = _select_current_task(tasks)
    steps = await task_store.list_steps(
        task.task_id,
        conversation_id=conversation_id,
    )
    lines = [
        "Current task context:",
        _format_task_with_steps(task, steps),
    ]
    next_step = _current_or_pending_step(steps)
    if next_step is not None:
        lines.extend(["Next step:", format_deskmate_task_step(next_step)])
    if tool_action_log is not None:
        tool_tasks, tool_actions, tool_lessons = await _related_tool_history(
            task,
            steps,
            tool_action_log=tool_action_log,
            conversation_id=conversation_id,
        )
        lines.append("Related tool tasks:")
        lines.extend(f"  - {format_tool_task_summary(item)}" for item in tool_tasks)
        if not tool_tasks:
            lines.append("  - none")
        lines.append("Related tool actions:")
        lines.extend(f"  - {format_tool_action_summary(item)}" for item in tool_actions)
        if not tool_actions:
            lines.append("  - none")
        lines.append("Related tool lessons:")
        lines.extend(f"  - {format_tool_lesson(item)}" for item in tool_lessons)
        if not tool_lessons:
            lines.append("  - none")
    return "\n".join(lines)


async def _start_matching_task(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
) -> str:
    task = await _resolve_one_task(
        target,
        task_store=task_store,
        conversation_id=conversation_id,
        status="active",
    )
    if isinstance(task, str):
        return task
    steps = await task_store.list_steps(
        task.task_id,
        conversation_id=conversation_id,
    )
    updated_steps = steps
    if steps and not any(step.status == "in_progress" for step in steps):
        first_pending_index = _current_or_pending_step_index(steps)
        if first_pending_index is not None:
            replacement: list[dict[str, object]] = []
            for index, step in enumerate(steps):
                status = "in_progress" if index == first_pending_index else step.status
                replacement.append({"content": step.content, "status": status})
            updated_steps = await task_store.replace_steps(
                task.task_id,
                replacement,
                conversation_id=conversation_id,
            ) or []
    updated_task = await task_store.update(
        task.task_id,
        conversation_id=conversation_id,
        status="in_progress",
    )
    return "Task started:\n" + _format_task_with_steps(
        updated_task or task,
        updated_steps,
    )


async def _pause_matching_task(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
) -> str:
    task = await _resolve_one_active_task(
        target,
        task_store=task_store,
        conversation_id=conversation_id,
    )
    if isinstance(task, str):
        return task
    steps = await task_store.list_steps(
        task.task_id,
        conversation_id=conversation_id,
    )
    updated_steps = steps
    if any(step.status == "in_progress" for step in steps):
        replacement = [
            {
                "content": step.content,
                "status": "pending" if step.status == "in_progress" else step.status,
            }
            for step in steps
        ]
        updated_steps = await task_store.replace_steps(
            task.task_id,
            replacement,
            conversation_id=conversation_id,
        ) or []
    updated_task = await task_store.update(
        task.task_id,
        conversation_id=conversation_id,
        status="open",
    )
    return "Task paused:\n" + _format_task_with_steps(
        updated_task or task,
        updated_steps,
    )


async def _advance_matching_task_step(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
) -> str:
    task = await _resolve_one_active_task(
        target,
        task_store=task_store,
        conversation_id=conversation_id,
    )
    if isinstance(task, str):
        return task
    steps = await task_store.list_steps(
        task.task_id,
        conversation_id=conversation_id,
    )
    if not steps:
        return "Task has no steps."
    current_index = _current_or_pending_step_index(steps)
    if current_index is None:
        return "Task already has no pending steps."
    next_pending_index = _next_pending_step_index(steps, after=current_index)
    replacement: list[dict[str, object]] = []
    for index, step in enumerate(steps):
        status = step.status
        if index == current_index:
            status = "completed"
        elif next_pending_index is not None and index == next_pending_index:
            status = "in_progress"
        replacement.append({"content": step.content, "status": status})
    updated_steps = await task_store.replace_steps(
        task.task_id,
        replacement,
        conversation_id=conversation_id,
    )
    if updated_steps is None:
        return "No matching task."
    if next_pending_index is None:
        updated_task = await task_store.update(
            task.task_id,
            conversation_id=conversation_id,
            status="done",
        )
        return (
            "Task completed:\n"
            + _format_task_with_steps(updated_task or task, updated_steps)
        )
    return (
        f"Task step advanced for {task.task_id}:\n"
        + "\n".join(format_deskmate_task_step(step) for step in updated_steps)
    )


async def _replace_matching_task_steps(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
    steps: list[dict[str, object]],
) -> str:
    task = await _resolve_one_active_task(
        target,
        task_store=task_store,
        conversation_id=conversation_id,
    )
    if isinstance(task, str):
        return task
    try:
        updated = await task_store.replace_steps(
            task.task_id,
            steps,
            conversation_id=conversation_id,
        )
    except ValueError as exc:
        return f"Task steps error: {exc}"
    if updated is None:
        return "No matching task."
    if not updated:
        return f"Task steps updated for {task.task_id}: none."
    return (
        f"Task steps updated for {task.task_id}:\n"
        + "\n".join(format_deskmate_task_step(step) for step in updated)
    )


async def _update_matching_task(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
    status: str,
) -> DeskmateTaskRecord | str:
    task = await task_store.update(
        target,
        conversation_id=conversation_id,
        status=status,  # type: ignore[arg-type]
    )
    if task is not None:
        return task
    match = await _resolve_one_active_task(
        target,
        task_store=task_store,
        conversation_id=conversation_id,
    )
    if isinstance(match, str):
        return match
    return await task_store.update(
        match.task_id,
        conversation_id=conversation_id,
        status=status,  # type: ignore[arg-type]
    ) or "No matching task."


async def _resolve_one_active_task(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
) -> DeskmateTaskRecord | str:
    return await _resolve_one_task(
        target,
        task_store=task_store,
        conversation_id=conversation_id,
        status="active",
    )


async def _resolve_one_task(
    target: str,
    *,
    task_store: DeskmateTaskStore,
    conversation_id: str,
    status: str,
) -> DeskmateTaskRecord | str:
    exact = await task_store.get(target)
    if exact is not None and exact.conversation_id == conversation_id:
        return exact
    matches = await task_store.search(
        conversation_id,
        query=target,
        status=status,  # type: ignore[arg-type]
        limit=2,
    )
    if not matches:
        return "No matching task."
    if len(matches) > 1:
        return "Multiple matching tasks:\n" + "\n".join(
            format_deskmate_task(match) for match in matches
        )
    return matches[0]


def _format_task_with_steps(
    task: DeskmateTaskRecord,
    steps: list[DeskmateTaskStep],
) -> str:
    line = format_deskmate_task(task)
    if not steps:
        return line
    step_lines = "\n".join(
        f"  - {format_deskmate_task_step(step)}" for step in steps[:5]
    )
    return f"{line}\n  steps:\n{step_lines}"


def _select_current_task(tasks: list[DeskmateTaskRecord]) -> DeskmateTaskRecord:
    for task in tasks:
        if task.status == "in_progress":
            return task
    return tasks[0]


def _current_or_pending_step(steps: list[DeskmateTaskStep]) -> DeskmateTaskStep | None:
    index = _current_or_pending_step_index(steps)
    return steps[index] if index is not None else None


async def _related_tool_history(
    task: DeskmateTaskRecord,
    steps: list[DeskmateTaskStep],
    *,
    tool_action_log: ToolActionLog,
    conversation_id: str,
    limit: int = 3,
):
    terms = _task_resume_search_terms(task, steps)
    tool_tasks = []
    tool_actions = []
    tool_lessons = []
    seen_task_ids: set[str] = set()
    seen_action_ids: set[str] = set()
    seen_lesson_ids: set[str] = set()
    for item in await tool_action_log.recent(
        conversation_id,
        task_id=task.task_id,
        limit=limit,
    ):
        if item.tool_call_id in seen_action_ids:
            continue
        tool_actions.append(item)
        seen_action_ids.add(item.tool_call_id)
        if len(tool_actions) >= limit:
            break
    for item in await tool_action_log.recent_lessons(
        conversation_id,
        task_id=task.task_id,
        limit=limit,
    ):
        if item.lesson_key in seen_lesson_ids:
            continue
        tool_lessons.append(item)
        seen_lesson_ids.add(item.lesson_key)
        if len(tool_lessons) >= limit:
            break
    for term in terms:
        if len(tool_tasks) < limit:
            for item in await tool_action_log.search_tasks(
                conversation_id,
                query=term,
                limit=limit,
            ):
                if item.task_id in seen_task_ids:
                    continue
                tool_tasks.append(item)
                seen_task_ids.add(item.task_id)
                if len(tool_tasks) >= limit:
                    break
        if len(tool_actions) < limit:
            for item in await tool_action_log.search(
                conversation_id,
                query=term,
                limit=limit,
            ):
                key = item.tool_call_id
                if key in seen_action_ids:
                    continue
                tool_actions.append(item)
                seen_action_ids.add(key)
                if len(tool_actions) >= limit:
                    break
        if len(tool_lessons) < limit:
            for item in await tool_action_log.search_lessons(
                conversation_id,
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
    return tool_tasks[:limit], tool_actions[:limit], tool_lessons[:limit]


def _task_resume_search_terms(
    task: DeskmateTaskRecord,
    steps: list[DeskmateTaskStep],
) -> list[str]:
    step_terms: list[str] = []
    for step in steps[:8]:
        if step.active_form:
            step_terms.append(step.active_form)
        if step.content and step.content != step.active_form:
            step_terms.append(step.content)
    raw_terms = [
        task.title,
        task.notes,
        *step_terms,
        *(word for term in step_terms for word in term.split()),
        *task.title.split(),
        *task.notes.split(),
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        term = _clean_text(raw)
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


def _current_or_pending_step_index(steps: list[DeskmateTaskStep]) -> int | None:
    for index, step in enumerate(steps):
        if step.status == "in_progress":
            return index
    for index, step in enumerate(steps):
        if step.status == "pending":
            return index
    return None


def _next_pending_step_index(
    steps: list[DeskmateTaskStep],
    *,
    after: int,
) -> int | None:
    for index, step in enumerate(steps):
        if index > after and step.status == "pending":
            return index
    return None


def _status_for_update(text: str, status: str | None) -> str:
    if status:
        normalized = status.lower().replace(" ", "_")
        return "cancelled" if normalized == "canceled" else normalized
    lowered = text.lower()
    if lowered.startswith(("cancel", "delete")) or lowered.startswith(("取消", "删除")):
        return "cancelled"
    return "done"


def _split_title_notes(text: str) -> tuple[str, str]:
    title = _clean_text(text)
    notes = ""
    for separator in (" -- notes: ", " -- note: ", " notes: ", " note: ", " -- "):
        if separator in title.lower():
            idx = title.lower().find(separator)
            notes = _clean_text(title[idx + len(separator):])
            title = _clean_text(title[:idx])
            break
    return title, notes


def _parse_step_items(text: str) -> tuple[dict[str, object], ...]:
    raw_items = [
        item
        for item in re.split(r"\s*(?:[;；|]|\n)\s*", text.strip())
        if item.strip()
    ]
    steps: list[dict[str, object]] = []
    for item in raw_items:
        status, content = _split_step_status(item)
        if not content:
            continue
        steps.append({"content": content, "status": status})
    return tuple(steps)


def _split_step_status(item: str) -> tuple[str, str]:
    raw = item.strip()
    match = re.match(
        r"^(?P<status>pending|todo|open|in_progress|in progress|doing|current|completed|done)\s*[:：-]\s*(?P<content>.+)$",
        raw,
        re.I,
    )
    if not match:
        return "pending", _clean_text(raw)
    status = match.group("status").lower().replace(" ", "_")
    mapped = {
        "todo": "pending",
        "open": "pending",
        "doing": "in_progress",
        "current": "in_progress",
        "done": "completed",
    }.get(status, status)
    return mapped, _clean_text(match.group("content"))


def _clean_text(value: str) -> str:
    return " ".join(value.strip().strip("。.!?").split())


__all__ = [
    "TaskCommand",
    "parse_task_command",
    "run_task_command",
    "task_control_composer",
    "task_control_streaming_composer",
]
