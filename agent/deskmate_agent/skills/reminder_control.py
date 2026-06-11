"""Natural-language reminder creation for the reactive chain.

This skill only accepts explicit relative times, then writes to the
existing :class:`ReminderStore`. The scheduler and UI path stay unchanged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

from ..dispatcher import ReplyComposer, StreamingReplyComposer
from ..protocol.state import Priority
from ..reminders import Reminder, ReminderStatus, ReminderStore

Clock = Callable[[], int]
IdFactory = Callable[[], str]
ReminderCommandKind = Literal["list_reminders", "cancel_reminder"]

_MS_PER_SECOND = 1_000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


@dataclass(frozen=True)
class ReminderRequest:
    text: str
    delay_ms: int
    display_delay: str


@dataclass(frozen=True)
class ReminderCommand:
    kind: ReminderCommandKind
    reminder_id: str = ""


_REMIND_IN_PATTERNS = (
    re.compile(
        r"^(?:remind me to|remind me about|remind me)\s+"
        r"(?P<text>.+?)\s+in\s+(?P<amount>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)$",
        re.I,
    ),
    re.compile(
        r"^(?:set a reminder to|set reminder to)\s+"
        r"(?P<text>.+?)\s+in\s+(?P<amount>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)$",
        re.I,
    ),
    re.compile(
        r"^(?P<amount>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)\s+"
        r"later\s+remind me\s+(?:to\s+)?(?P<text>.+)$",
        re.I,
    ),
)
_TIMER_PATTERNS = (
    re.compile(
        r"^(?:timer|set timer|start timer)\s+(?:for\s+)?"
        r"(?P<amount>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)"
        r"(?:\s+(?P<text>.+))?$",
        re.I,
    ),
    re.compile(
        r"^(?:帮我)?(?:设置|设|开|启动)?(?:一个|个)?\s*"
        r"(?P<amount>\d+)\s*(?P<unit>秒|分钟|分|小时|个小时)\s*"
        r"(?:倒计时|计时器)$",
        re.I,
    ),
)
_ZH_REMINDER_PATTERNS = (
    re.compile(
        r"^(?P<amount>\d+)\s*(?P<unit>秒|分钟|分|小时|个小时)后"
        r"(?:提醒我|叫我)(?P<text>.+)$",
        re.I,
    ),
    re.compile(
        r"^(?:提醒我|叫我)(?P<text>.+?)"
        r"(?P<amount>\d+)\s*(?P<unit>秒|分钟|分|小时|个小时)后$",
        re.I,
    ),
)
_LIST_REMINDERS_PATTERNS = (
    re.compile(
        r"^(?:what reminders do i have|list reminders|show reminders|"
        r"show my reminders|list my reminders)\??$",
        re.I,
    ),
    re.compile(r"^(?:有什么提醒|列出提醒|显示提醒|我的提醒)\??$", re.I),
)
_CANCEL_REMINDER_PATTERNS = (
    re.compile(
        r"^(?:cancel|delete|remove)\s+(?:reminder|timer)\s+"
        r"(?P<reminder_id>[A-Za-z0-9_.:-]+)$",
        re.I,
    ),
    re.compile(
        r"^(?:取消|删除|删掉)\s*(?:提醒|计时器)\s*"
        r"(?P<reminder_id>[A-Za-z0-9_.:-]+)$",
        re.I,
    ),
)


def reminder_control_composer(
    *,
    reminder_store: ReminderStore | None = None,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
    fallback: ReplyComposer | None = None,
) -> ReplyComposer:
    """Return a composer that schedules reminders when recognized."""
    effective_clock = clock or _default_clock
    effective_id_factory = id_factory or _default_id

    async def compose(text: str) -> str | None:
        command = parse_reminder_command(text)
        if command is not None:
            return _run_reminder_command(
                command,
                reminder_store=reminder_store,
                now_ms=effective_clock(),
            )
        request = parse_reminder_request(text)
        if request is None:
            return await fallback(text) if fallback is not None else None
        if reminder_store is None:
            return "I can make reminders once the reminder store is ready."
        reminder = schedule_reminder_request(
            request,
            now_ms=effective_clock(),
            reminder_id=effective_id_factory(),
        )
        reminder_store.add(reminder)
        return f"Reminder set for {request.display_delay}: {request.text}."

    return compose


def reminder_control_streaming_composer(
    *,
    reminder_store: ReminderStore | None = None,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
    fallback: StreamingReplyComposer | None = None,
) -> StreamingReplyComposer:
    """Streaming variant used ahead of LLM streaming."""
    effective_clock = clock or _default_clock
    effective_id_factory = id_factory or _default_id

    async def compose(text: str) -> AsyncIterator[str]:
        command = parse_reminder_command(text)
        if command is not None:
            yield _run_reminder_command(
                command,
                reminder_store=reminder_store,
                now_ms=effective_clock(),
            )
            return
        request = parse_reminder_request(text)
        if request is not None:
            if reminder_store is None:
                yield "I can make reminders once the reminder store is ready."
                return
            reminder_store.add(
                schedule_reminder_request(
                    request,
                    now_ms=effective_clock(),
                    reminder_id=effective_id_factory(),
                )
            )
            yield f"Reminder set for {request.display_delay}: {request.text}."
            return
        if fallback is None:
            return
        async for chunk in fallback(text):
            yield chunk

    return compose


def parse_reminder_command(text: str) -> ReminderCommand | None:
    stripped = " ".join(text.strip().split())
    if not stripped:
        return None
    for pattern in _LIST_REMINDERS_PATTERNS:
        if pattern.match(stripped):
            return ReminderCommand("list_reminders")
    for pattern in _CANCEL_REMINDER_PATTERNS:
        match = pattern.match(stripped)
        if match is not None:
            reminder_id = _clean_text(match.group("reminder_id"))
            return (
                ReminderCommand("cancel_reminder", reminder_id)
                if reminder_id
                else None
            )
    return None


def parse_reminder_request(text: str) -> ReminderRequest | None:
    stripped = " ".join(text.strip().split())
    if not stripped:
        return None

    for pattern in (*_REMIND_IN_PATTERNS, *_TIMER_PATTERNS, *_ZH_REMINDER_PATTERNS):
        match = pattern.match(stripped)
        if match is None:
            continue
        delay_ms = _delay_ms(match.group("amount"), match.group("unit"))
        if delay_ms is None:
            return None
        reminder_text = _clean_text(match.groupdict().get("text") or "Timer done")
        if not reminder_text:
            return None
        return ReminderRequest(
            text=reminder_text,
            delay_ms=delay_ms,
            display_delay=_display_delay(delay_ms),
        )
    return None


def _run_reminder_command(
    command: ReminderCommand,
    *,
    reminder_store: ReminderStore | None,
    now_ms: int,
) -> str:
    if reminder_store is None:
        return "I can manage reminders once the reminder store is ready."
    if command.kind == "list_reminders":
        reminders = reminder_store.list(status=ReminderStatus.PENDING)
        if not reminders:
            return "You do not have any pending reminders."
        lines = [
            _format_reminder(reminder, now_ms=now_ms)
            for reminder in reminders[:10]
        ]
        return "Pending reminders:\n" + "\n".join(lines)
    reminder = reminder_store.get(command.reminder_id)
    if reminder is None:
        return "I do not have a matching reminder."
    cancelled = reminder_store.cancel(command.reminder_id, now_ms)
    if cancelled is None:
        return f"Reminder is already resolved: {reminder.text}."
    return f"Cancelled reminder {cancelled.reminder_id}: {cancelled.text}."


def schedule_reminder_request(
    request: ReminderRequest,
    *,
    now_ms: int,
    reminder_id: str,
) -> Reminder:
    """Build a reminder from structured tool-call input.

    The caller still owns inserting the reminder so tests can inspect the
    object before mutation when needed.
    """
    return _build_reminder(
        request,
        now_ms=now_ms,
        reminder_id=reminder_id,
    )


def _build_reminder(
    request: ReminderRequest,
    *,
    now_ms: int,
    reminder_id: str,
) -> Reminder:
    return Reminder(
        reminder_id=reminder_id,
        text=request.text,
        due_at_ms=now_ms + request.delay_ms,
        created_at_ms=now_ms,
        priority=Priority.P1,
        extras={
            "source": "reminder_control",
            "delay_ms": request.delay_ms,
            "display_delay": request.display_delay,
        },
    )


def _delay_ms(amount_raw: str, unit_raw: str) -> int | None:
    amount = int(amount_raw)
    if amount <= 0:
        return None
    unit = unit_raw.lower()
    if unit in {"second", "seconds", "sec", "secs", "秒"}:
        return amount * _MS_PER_SECOND
    if unit in {"minute", "minutes", "min", "mins", "分钟", "分"}:
        return amount * _MS_PER_MINUTE
    if unit in {"hour", "hours", "hr", "hrs", "小时", "个小时"}:
        return amount * _MS_PER_HOUR
    return None


def _display_delay(delay_ms: int) -> str:
    if delay_ms % _MS_PER_HOUR == 0:
        hours = delay_ms // _MS_PER_HOUR
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if delay_ms % _MS_PER_MINUTE == 0:
        minutes = delay_ms // _MS_PER_MINUTE
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    seconds = delay_ms // _MS_PER_SECOND
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _format_reminder(reminder: Reminder, *, now_ms: int) -> str:
    delta_ms = reminder.due_at_ms - now_ms
    when = (
        f"due in {_display_delay(delta_ms)}"
        if delta_ms >= 0
        else f"overdue by {_display_delay(abs(delta_ms))}"
    )
    return f"{reminder.reminder_id} [{when}]: {reminder.text}"


def _clean_text(value: str) -> str:
    return value.strip().strip("\"'“”‘’").strip()


def _default_clock() -> int:
    import time

    return int(time.time() * 1000)


def _default_id() -> str:
    return "nl-reminder-" + uuid.uuid4().hex[:12]


__all__ = [
    "ReminderCommand",
    "ReminderRequest",
    "parse_reminder_command",
    "parse_reminder_request",
    "reminder_control_composer",
    "reminder_control_streaming_composer",
    "schedule_reminder_request",
]
