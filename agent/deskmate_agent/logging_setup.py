"""Structured logging with trace_id propagation.

V10 L3 Instrumentation: every user-triggered event gets a trace_id that must
survive the Swift → bridge → agent → skill → LLM → bridge → Swift path. On the
Python side we bind it to a ContextVar so async tasks inherit it automatically.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

_trace_id_var: ContextVar[str | None] = ContextVar("deskmate_trace_id", default=None)

_CONFIGURED = False


def bind_trace_id(trace_id: str | None) -> None:
    """Bind the current task's trace_id. Pass ``None`` to clear."""
    _trace_id_var.set(trace_id)


def get_trace_id() -> str | None:
    """Return the currently bound trace_id, or ``None``."""
    return _trace_id_var.get()


@contextmanager
def trace_scope(trace_id: str | None) -> Iterator[None]:
    """Scoped trace_id binding that restores the previous value on exit."""
    token = _trace_id_var.set(trace_id)
    try:
        yield
    finally:
        _trace_id_var.reset(token)


def _inject_trace_id(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    trace_id = _trace_id_var.get()
    if trace_id and "trace_id" not in event_dict:
        event_dict["trace_id"] = trace_id
    return event_dict


def configure_logging(level: int = logging.INFO, *, json_output: bool = True) -> None:
    """Install structlog with trace_id propagation. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger, configuring defaults on first call."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)


__all__ = [
    "bind_trace_id",
    "configure_logging",
    "get_logger",
    "get_trace_id",
    "trace_scope",
]
