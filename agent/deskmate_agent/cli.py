"""Small command-line helper (V10 Phase 14-i / 14-iii).

Two families today:

- ``build-*`` — drop a JSON line into ``~/.deskmate/build-status.json``
  that the running agent picks up to drive the island build pill.
- ``hook ingest`` — normalize external agent hook JSON into the file queue
  consumed by the resident Python agent.
- ``island module register`` — enqueue an external island module spec for the
  resident agent to forward as a typed ``register_module`` intent.
- ``today`` — read the coding-session SQLite directly (no agent
  round-trip) and print a daily coding summary.

Usage examples (all of these are safe to copy into a Makefile /
``package.json`` / CI step)::

    deskmate build-start "cargo test"
    deskmate build-progress "cargo test" 0.42
    deskmate build-done "cargo test"
    deskmate build-failed "cargo test" --message "42 failed"
    deskmate build-dismiss
    echo '{"session_id":"s1","event":"session.started"}' | deskmate hook ingest --source codex
    deskmate island module register kiro.spec --kind live_activity --title KIRO --activity-prefix kiro-spec-
    deskmate today            # human readable summary
    deskmate today --json     # machine readable

``DESKMATE_BUILD_STATUS_PATH`` overrides the build-status target path
if you need to redirect (e.g. tests). ``DESKMATE_DB_DIR`` overrides
the agent's SQLite directory the ``today`` subcommand queries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .git_branch import current_branch

_DEFAULT_PATH = Path.home() / ".deskmate" / "build-status.json"


def _default_db_dir() -> Path:
    override = os.environ.get("DESKMATE_DB_DIR")
    if override:
        return Path(override).expanduser()
    # Match :func:`deskmate_agent.main.default_db_dir`.
    return Path.home() / "Library" / "Application Support" / "Deskmate"


def _local_tz_offset_s() -> int:
    # Mirror :func:`deskmate_agent.app._local_tz_offset_s`.
    if time.daylight and time.localtime().tm_isdst > 0:
        return -time.altzone
    return -time.timezone


def _local_midnight_ms(now_ms: int, tz_offset_s: int) -> int:
    day_ms = 24 * 60 * 60 * 1000
    offset_ms = tz_offset_s * 1000
    local_ms = now_ms + offset_ms
    local_midnight = local_ms - (local_ms % day_ms)
    return local_midnight - offset_ms


def _format_duration_ms(ms: int) -> str:
    seconds = ms // 1000
    if seconds < 1:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}h {rem}m" if rem else f"{hours}h"


def _target_path() -> Path:
    override = os.environ.get("DESKMATE_BUILD_STATUS_PATH")
    return Path(override) if override else _DEFAULT_PATH


def _write(payload: dict) -> None:
    path = _target_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic rename so the watcher never reads a half-written file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deskmate",
        description="Deskmate command helper — currently covers the "
        "build-status island pill.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # Phase 14-ii: every build-* subcommand accepts ``--no-branch``
    # to opt out of the automatic git-branch annotation.
    def _add_branch_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--no-branch",
            dest="include_branch",
            action="store_false",
            default=True,
            help="skip the automatic git-branch annotation",
        )
        sp.add_argument(
            "--branch",
            default=None,
            help="override the detected branch name",
        )

    start = sub.add_parser("build-start", help="flag a build / test as started")
    start.add_argument("task", help="free-form task label")
    _add_branch_flags(start)

    progress = sub.add_parser(
        "build-progress", help="report fractional progress 0..1"
    )
    progress.add_argument("task")
    progress.add_argument("progress", type=float)
    progress.add_argument("--message", default=None)
    _add_branch_flags(progress)

    done = sub.add_parser("build-done", help="flag a successful completion")
    done.add_argument("task")
    done.add_argument("--message", default=None)
    _add_branch_flags(done)

    failed = sub.add_parser("build-failed", help="flag a failure")
    failed.add_argument("task")
    failed.add_argument("--message", default=None)
    _add_branch_flags(failed)

    sub.add_parser(
        "build-dismiss",
        help="clear the build pill immediately regardless of state",
    )

    today = sub.add_parser(
        "today",
        help="print today's coding-session summary (reads SQLite directly)",
    )
    today.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a JSON object suitable for piping into other tools",
    )
    today.add_argument(
        "--db-dir",
        default=None,
        help="override the SQLite directory (defaults to $DESKMATE_DB_DIR "
        "or the macOS Application Support path).",
    )

    memory = sub.add_parser(
        "memory",
        help="diagnose persisted chat/profile/task memory and tool-call ledger",
    )
    memory_sub = memory.add_subparsers(dest="memory_cmd", required=True)
    memory_summary = memory_sub.add_parser(
        "summary",
        help="print a read-only summary of persisted Deskmate memory",
    )
    memory_summary.add_argument(
        "--conversation-id",
        default="default",
        help="conversation id to inspect (default: default)",
    )
    memory_summary.add_argument(
        "--db-dir",
        default=None,
        help="override the SQLite directory (defaults to $DESKMATE_DB_DIR "
        "or the macOS Application Support path).",
    )
    memory_summary.add_argument(
        "--limit",
        type=int,
        default=5,
        help="number of recent tool tasks to show (default 5)",
    )
    memory_summary.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a JSON object suitable for diagnostics",
    )
    memory_task = memory_sub.add_parser(
        "tool-task",
        help="print one persisted tool task and its action details",
    )
    memory_task.add_argument("task_id")
    memory_task.add_argument(
        "--conversation-id",
        default="default",
        help="conversation id to inspect (default: default)",
    )
    memory_task.add_argument(
        "--db-dir",
        default=None,
        help="override the SQLite directory (defaults to $DESKMATE_DB_DIR "
        "or the macOS Application Support path).",
    )
    memory_task.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum number of task actions to show (default 20)",
    )
    memory_task.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a JSON object suitable for diagnostics",
    )
    memory_user_task = memory_sub.add_parser(
        "task",
        help="print one persistent Deskmate task by task id",
    )
    memory_user_task.add_argument("task_id")
    memory_user_task.add_argument(
        "--conversation-id",
        default="default",
        help="conversation id to inspect (default: default)",
    )
    memory_user_task.add_argument(
        "--db-dir",
        default=None,
        help="override the SQLite directory (defaults to $DESKMATE_DB_DIR "
        "or the macOS Application Support path).",
    )
    memory_user_task.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a JSON object suitable for diagnostics",
    )
    memory_task_context = memory_sub.add_parser(
        "task-context",
        help="print one Deskmate task plus related tool tasks/actions/lessons",
    )
    memory_task_context.add_argument(
        "task_id",
        help="persistent Deskmate task id to inspect",
    )
    memory_task_context.add_argument(
        "--conversation-id",
        default="default",
        help="conversation id to inspect (default: default)",
    )
    memory_task_context.add_argument(
        "--db-dir",
        default=None,
        help="override the SQLite directory (defaults to $DESKMATE_DB_DIR "
        "or the macOS Application Support path).",
    )
    memory_task_context.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum number of related rows per section (default 10)",
    )
    memory_task_context.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a JSON object suitable for diagnostics",
    )

    task = sub.add_parser(
        "task",
        help="manage persistent Deskmate tasks/todos",
    )
    task_sub = task.add_subparsers(dest="task_cmd", required=True)

    def _add_task_store_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--conversation-id",
            default="default",
            help="conversation id to manage (default: default)",
        )
        sp.add_argument(
            "--db-dir",
            default=None,
            help="override the SQLite directory (defaults to $DESKMATE_DB_DIR "
            "or the macOS Application Support path).",
        )
        sp.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="emit a JSON object suitable for scripts",
        )

    task_add = task_sub.add_parser("add", help="create a persistent task")
    task_add.add_argument("title", help="short task title")
    task_add.add_argument("--notes", default="", help="optional task notes")
    task_add.add_argument(
        "--status",
        default="open",
        choices=["open", "in_progress", "done", "cancelled"],
        help="initial task status (default: open)",
    )
    _add_task_store_flags(task_add)

    task_list = task_sub.add_parser("list", help="list persistent tasks")
    task_list.add_argument(
        "--status",
        default="active",
        choices=["active", "open", "in_progress", "done", "cancelled", "all"],
        help="which tasks to list (default: active)",
    )
    task_list.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum number of tasks to show (default 10)",
    )
    _add_task_store_flags(task_list)

    task_search = task_sub.add_parser("search", help="search persistent tasks")
    task_search.add_argument("query")
    task_search.add_argument(
        "--status",
        default="all",
        choices=["active", "open", "in_progress", "done", "cancelled", "all"],
        help="optional status filter (default: all)",
    )
    task_search.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum number of tasks to show (default 10)",
    )
    _add_task_store_flags(task_search)

    task_update = task_sub.add_parser("update", help="update a persistent task")
    task_update.add_argument("task_id")
    task_update.add_argument("--title", default=None, help="replacement title")
    task_update.add_argument("--notes", default=None, help="replacement notes")
    task_update.add_argument(
        "--status",
        default=None,
        choices=["open", "in_progress", "done", "cancelled"],
        help="replacement status",
    )
    _add_task_store_flags(task_update)

    task_done = task_sub.add_parser("done", help="mark a task done")
    task_done.add_argument("task_id")
    _add_task_store_flags(task_done)

    task_cancel = task_sub.add_parser("cancel", help="mark a task cancelled")
    task_cancel.add_argument("task_id")
    _add_task_store_flags(task_cancel)

    project = sub.add_parser(
        "project",
        help="manage the project registry used by coding-session tracking",
    )
    project_sub = project.add_subparsers(dest="project_cmd", required=True)

    p_add = project_sub.add_parser(
        "add",
        help="register a project (defaults to the current working directory)",
    )
    p_add.add_argument("path", nargs="?", default=".")
    p_add.add_argument(
        "--name",
        default=None,
        help="display name (defaults to the directory's basename)",
    )
    p_add.add_argument(
        "--bundle-hint",
        action="append",
        default=[],
        metavar="BUNDLE_ID",
        help="restrict matching to IDEs with this bundle id "
        "(repeatable)",
    )

    p_list = project_sub.add_parser(
        "list", help="show every registered project"
    )
    p_list.add_argument(
        "--json", dest="as_json", action="store_true"
    )

    p_remove = project_sub.add_parser(
        "remove", help="unregister a project by name"
    )
    p_remove.add_argument("name")

    runtime = sub.add_parser(
        "runtime",
        help="diagnose passively detected IDE / agent runtimes",
    )
    runtime_sub = runtime.add_subparsers(dest="runtime_cmd", required=True)
    scan = runtime_sub.add_parser(
        "scan",
        help="run one read-only process scan and print detected runtimes",
    )
    scan.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable runtime statuses",
    )
    scan.add_argument(
        "--ps-file",
        default=None,
        help="read ps output from a file instead of running /bin/ps "
        "(mainly useful for diagnostics/tests)",
    )

    tail = sub.add_parser(
        "tail-status",
        help="stream the running agent's intent log (JSON lines) "
        "to stdout — like ``tail -f``",
    )
    tail.add_argument(
        "--path",
        default=None,
        help="override the log path (defaults to $DESKMATE_INTENT_LOG_PATH "
        "or the macOS Application Support path).",
    )
    tail.add_argument(
        "-n",
        "--lines",
        type=int,
        default=10,
        help="number of existing lines to replay before tailing (default 10)",
    )
    tail.add_argument(
        "--poll-ms",
        type=int,
        default=200,
        help="poll interval in milliseconds (default 200)",
    )
    tail.add_argument(
        "--once",
        action="store_true",
        help="don't loop — print the existing buffer and exit",
    )

    hook = sub.add_parser(
        "hook",
        help="ingest and manage external agent hooks",
    )
    hook_sub = hook.add_subparsers(dest="hook_cmd", required=True)
    ingest = hook_sub.add_parser(
        "ingest",
        help="read one JSON object from stdin and enqueue a normalized HookEvent",
    )
    ingest.add_argument(
        "--source",
        required=True,
        help="hook source name, e.g. codex",
    )
    ingest.add_argument(
        "--queue-dir",
        default=None,
        help="override queue dir (defaults to $DESKMATE_HOOK_EVENTS_DIR or ~/.deskmate/hook-events)",
    )

    def _add_hook_management_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--source",
            required=True,
            choices=["codex", "claude", "cursor"],
            help="agent config to manage",
        )
        sp.add_argument(
            "--config",
            default=None,
            help="override config path; useful for tests or nonstandard installs",
        )
        sp.add_argument(
            "--command",
            dest="hook_command",
            default=None,
            help="override installed hook command "
            "(default: deskmate hook ingest --source <source>)",
        )

    install = hook_sub.add_parser(
        "install",
        help="opt-in install Deskmate-managed hooks for an agent",
    )
    _add_hook_management_flags(install)

    status = hook_sub.add_parser(
        "status",
        help="show whether Deskmate-managed hooks are installed",
    )
    _add_hook_management_flags(status)
    status.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable status",
    )

    uninstall = hook_sub.add_parser(
        "uninstall",
        help="remove Deskmate-managed hooks without touching user hooks",
    )
    _add_hook_management_flags(uninstall)

    doctor = hook_sub.add_parser(
        "doctor",
        help="diagnose hook helper, agent queue, bridge socket, and installed hooks",
    )
    doctor.add_argument(
        "--source",
        action="append",
        choices=["codex", "claude", "cursor"],
        default=None,
        help="source to check (repeatable; default checks codex, claude, cursor)",
    )
    doctor.add_argument(
        "--queue-dir",
        default=None,
        help="override hook queue dir",
    )
    doctor.add_argument(
        "--socket",
        default=None,
        help="override agent bridge socket path",
    )
    doctor.add_argument(
        "--intent-log",
        default=None,
        help="override intent log path",
    )
    doctor.add_argument(
        "--repair-helper",
        action="store_true",
        help="install or refresh the stable deskmate-hook helper",
    )
    doctor.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable diagnostics",
    )

    island = sub.add_parser(
        "island",
        help="manage external island integrations",
    )
    island_sub = island.add_subparsers(dest="island_cmd", required=True)
    module = island_sub.add_parser(
        "module",
        help="manage externally registered island modules",
    )
    module_sub = module.add_subparsers(dest="module_cmd", required=True)
    register = module_sub.add_parser(
        "register",
        help="enqueue an island module registration for the resident agent",
    )
    register.add_argument("id", help="stable module id, e.g. kiro.spec")
    register.add_argument(
        "--kind",
        required=True,
        help="surface kind, e.g. live_activity or notification_card",
    )
    register.add_argument("--title", required=True, help="compact module title")
    register.add_argument(
        "--priority",
        type=int,
        default=50,
        help="claim priority when multiple modules match (default 50)",
    )
    register.add_argument(
        "--activity-prefix",
        default=None,
        help="only claim live activities whose activity_id starts with this prefix",
    )
    register.add_argument(
        "--subtitle",
        default=None,
        help="optional template; supports {detail}, {activity}, {session}",
    )
    register.add_argument(
        "--image",
        default=None,
        help="optional SF Symbol name, e.g. k.circle",
    )
    register.add_argument(
        "--queue-dir",
        default=None,
        help="override queue dir (defaults to $DESKMATE_MODULE_REGISTRATIONS_DIR or ~/.deskmate/module-registrations)",
    )
    register.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the enqueued spec as JSON",
    )
    return p


def _attach_branch(payload: dict, args: argparse.Namespace) -> None:
    """Attach a ``branch`` field when the user wants it."""
    if not getattr(args, "include_branch", False):
        return
    branch = args.branch if args.branch else current_branch(Path.cwd())
    if branch:
        payload["branch"] = branch


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "build-start":
        payload: dict = {"state": "started", "task": args.task}
        _attach_branch(payload, args)
    elif args.command == "build-progress":
        payload = {
            "state": "progress",
            "task": args.task,
            "progress": args.progress,
        }
        if args.message:
            payload["message"] = args.message
        _attach_branch(payload, args)
    elif args.command == "build-done":
        payload = {"state": "done", "task": args.task}
        if args.message:
            payload["message"] = args.message
        _attach_branch(payload, args)
    elif args.command == "build-failed":
        payload = {"state": "failed", "task": args.task}
        if args.message:
            payload["message"] = args.message
        _attach_branch(payload, args)
    elif args.command == "build-dismiss":
        payload = {"state": "dismiss"}
    elif args.command == "today":
        return _run_today(args)
    elif args.command == "memory":
        return _run_memory(args)
    elif args.command == "task":
        return _run_task(args)
    elif args.command == "tail-status":
        return _run_tail_status(args)
    elif args.command == "project":
        return _run_project(args)
    elif args.command == "runtime":
        return _run_runtime(args)
    elif args.command == "hook":
        return _run_hook(args)
    elif args.command == "island":
        return _run_island(args)
    else:  # pragma: no cover — argparse enforces this
        return 2
    _write(payload)
    # Quiet by default — Makefile integrations don't want extra noise.
    return 0


def _run_task(args: argparse.Namespace) -> int:
    db_dir = Path(args.db_dir).expanduser() if args.db_dir else _default_db_dir()
    try:
        payload = asyncio.run(_run_task_async(args, db_dir=db_dir))
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if getattr(args, "as_json", False):
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        _write_task_command_payload(payload)
    return 0 if bool(payload.get("ok")) else 1


async def _run_task_async(
    args: argparse.Namespace,
    *,
    db_dir: Path,
) -> dict[str, object]:
    from .memory import DeskmateTaskStore

    db_dir.mkdir(parents=True, exist_ok=True)
    conversation_id = str(args.conversation_id or "default")
    async with DeskmateTaskStore(db_dir / "tasks.db") as store:
        if args.task_cmd == "add":
            task = await store.create(
                conversation_id=conversation_id,
                title=str(args.title),
                notes=str(args.notes or ""),
                status=str(args.status or "open"),  # type: ignore[arg-type]
            )
            return {
                "ok": True,
                "action": "add",
                "db_path": str(db_dir / "tasks.db"),
                "conversation_id": conversation_id,
                "task": _task_record_payload(task),
            }
        if args.task_cmd == "list":
            tasks = await store.list(
                conversation_id,
                status=str(args.status or "active"),  # type: ignore[arg-type]
                limit=max(1, min(int(args.limit), 50)),
            )
            return {
                "ok": True,
                "action": "list",
                "db_path": str(db_dir / "tasks.db"),
                "conversation_id": conversation_id,
                "tasks": [_task_record_payload(task) for task in tasks],
            }
        if args.task_cmd == "search":
            tasks = await store.search(
                conversation_id,
                query=str(args.query),
                status=str(args.status or "all"),  # type: ignore[arg-type]
                limit=max(1, min(int(args.limit), 50)),
            )
            return {
                "ok": True,
                "action": "search",
                "db_path": str(db_dir / "tasks.db"),
                "conversation_id": conversation_id,
                "query": str(args.query),
                "tasks": [_task_record_payload(task) for task in tasks],
            }
        if args.task_cmd in {"update", "done", "cancel"}:
            status = getattr(args, "status", None)
            if args.task_cmd == "done":
                status = "done"
            elif args.task_cmd == "cancel":
                status = "cancelled"
            title = getattr(args, "title", None)
            notes = getattr(args, "notes", None)
            if args.task_cmd == "update" and title is None and notes is None and status is None:
                raise ValueError("provide --title, --notes, or --status")
            task = await store.update(
                str(args.task_id),
                conversation_id=conversation_id,
                title=title,
                notes=notes,
                status=status,  # type: ignore[arg-type]
            )
            return {
                "ok": task is not None,
                "action": args.task_cmd,
                "db_path": str(db_dir / "tasks.db"),
                "conversation_id": conversation_id,
                "task_id": str(args.task_id),
                "task": _task_record_payload(task) if task is not None else None,
            }
    raise ValueError(f"unknown task command: {args.task_cmd}")


def _task_record_payload(record) -> dict[str, object]:
    return {
        "task_id": record.task_id,
        "conversation_id": record.conversation_id,
        "title": record.title,
        "status": record.status,
        "notes": record.notes,
        "created_at_ms": record.created_at_ms,
        "updated_at_ms": record.updated_at_ms,
        "completed_at_ms": record.completed_at_ms,
    }


def _write_task_command_payload(payload: dict[str, object]) -> None:
    action = str(payload.get("action") or "")
    if not payload.get("ok"):
        task_id = payload.get("task_id")
        if task_id:
            sys.stderr.write(
                f"No task {task_id} for {payload.get('conversation_id', 'default')}\n"
            )
        else:
            sys.stderr.write("Task command failed.\n")
        return
    if action in {"add", "update", "done", "cancel"}:
        task = payload.get("task")
        assert isinstance(task, dict)
        sys.stdout.write(_format_task_payload(task) + "\n")
        return
    tasks = payload.get("tasks")
    assert isinstance(tasks, list)
    if not tasks:
        sys.stdout.write("No matching tasks.\n")
        return
    for task in tasks:
        if isinstance(task, dict):
            sys.stdout.write(_format_task_payload(task) + "\n")


def _format_task_payload(task: dict[str, object]) -> str:
    notes = f" - {task['notes']}" if task.get("notes") else ""
    return f"{task['task_id']} [{task['status']}]: {task['title']}{notes}"


def _run_island(args: argparse.Namespace) -> int:
    if args.island_cmd != "module" or args.module_cmd != "register":
        return 2
    from .module_registrations import write_module_registration
    from .protocol import IslandModuleSpec

    try:
        spec = IslandModuleSpec(
            id=str(args.id),
            kind=str(args.kind),
            title=str(args.title),
            priority=int(args.priority),
            activity_prefix=args.activity_prefix,
            subtitle=args.subtitle,
            image=args.image,
        )
    except ValueError as exc:
        sys.stderr.write(f"error: invalid module spec: {exc}\n")
        return 2
    queue_dir = Path(args.queue_dir).expanduser() if args.queue_dir else None
    path = write_module_registration(spec, queue_dir=queue_dir)
    if getattr(args, "as_json", False):
        payload = spec.model_dump(mode="json", exclude_none=True)
        payload["queue_path"] = str(path)
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0


def _run_runtime(args: argparse.Namespace) -> int:
    if args.runtime_cmd != "scan":
        return 2
    from .agent_runtime import scan_runtime_statuses

    if args.ps_file:
        path = Path(args.ps_file).expanduser()
        ps_provider = path.read_bytes
    else:
        ps_provider = None
    try:
        statuses = scan_runtime_statuses(
            ps_provider=ps_provider,
        ) if ps_provider is not None else scan_runtime_statuses()
    except OSError as exc:
        sys.stderr.write(f"error: could not read ps data: {exc}\n")
        return 1

    if getattr(args, "as_json", False):
        sys.stdout.write(
            json.dumps(
                [status.model_dump(mode="json") for status in statuses],
                indent=2,
            )
            + "\n"
        )
        return 0

    if not statuses:
        sys.stdout.write("No IDE or agent runtimes detected.\n")
        return 0
    for status in statuses:
        bits = [
            status.source.value,
            status.kind.value,
            f"pid={status.process_id}" if status.process_id is not None else "",
            f"phase={status.phase.value}",
        ]
        terminal = _runtime_terminal_label(status.raw)
        if terminal:
            bits.append(terminal)
        if status.cwd:
            bits.append(f"cwd={status.cwd}")
        sys.stdout.write(f"{status.display_name}: " + " ".join(b for b in bits if b) + "\n")
    return 0


def _runtime_terminal_label(raw: dict) -> str:
    terminal_app = raw.get("terminal_app")
    terminal_tty = raw.get("terminal_tty") or raw.get("tty")
    if terminal_app and terminal_tty:
        return f"terminal={terminal_app}@{terminal_tty}"
    if terminal_app:
        return f"terminal={terminal_app}"
    if terminal_tty:
        return f"tty={terminal_tty}"
    return ""


def _run_hook(args: argparse.Namespace) -> int:
    if args.hook_cmd == "doctor":
        return _run_hook_doctor(args)
    if args.hook_cmd in {"install", "status", "uninstall"}:
        return _run_hook_management(args)
    if args.hook_cmd != "ingest":
        return 2
    from .hooks import normalize_hook_event, write_hook_event

    raw_text = sys.stdin.read()
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: invalid JSON on stdin: {exc}\n")
        return 2
    if not isinstance(raw, dict):
        sys.stderr.write("error: hook payload must be a JSON object\n")
        return 2
    event = normalize_hook_event(raw, source=str(args.source))
    queue_dir = Path(args.queue_dir).expanduser() if args.queue_dir else None
    write_hook_event(event, queue_dir=queue_dir)
    return 0


def _run_hook_management(args: argparse.Namespace) -> int:
    from .hook_installers import (
        install_hooks,
        normalize_install_source,
        status_hooks,
        uninstall_hooks,
    )

    try:
        source = normalize_install_source(str(args.source))
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    config_path = Path(args.config).expanduser() if args.config else None
    command = str(args.hook_command) if args.hook_command else None

    try:
        if args.hook_cmd == "install":
            result = install_hooks(source, config_path=config_path, hook_command=command)
        elif args.hook_cmd == "uninstall":
            result = uninstall_hooks(source, config_path=config_path, hook_command=command)
        elif args.hook_cmd == "status":
            status = status_hooks(source, config_path=config_path, hook_command=command)
            if getattr(args, "as_json", False):
                sys.stdout.write(
                    json.dumps(
                        {
                            "source": status.source.value,
                            "config_path": str(status.config_path),
                            "installed": status.installed,
                            "managed_count": status.managed_count,
                            "message": status.message,
                        },
                        indent=2,
                    )
                    + "\n"
                )
            else:
                installed = "installed" if status.installed else "not installed"
                sys.stdout.write(
                    f"{status.source.value}: {installed} "
                    f"({status.managed_count} managed hooks) at {status.config_path}\n"
                )
            return 0 if status.installed else 1
        else:
            return 2
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: {exc}\n")
        return 1

    sys.stdout.write(
        f"{result.source.value}: {result.message} "
        f"({result.managed_count} managed hooks) at {result.config_path}\n"
    )
    return 0


def _run_hook_doctor(args: argparse.Namespace) -> int:
    from .bridge import default_socket_path
    from .hook_installers import (
        ensure_hook_helper,
        hook_helper_status,
        normalize_install_source,
        status_hooks,
    )
    from .hooks import default_hook_events_dir

    helper = ensure_hook_helper() if args.repair_helper else hook_helper_status()
    queue_dir = Path(args.queue_dir).expanduser() if args.queue_dir else default_hook_events_dir()
    socket_path = Path(args.socket).expanduser() if args.socket else default_socket_path()
    intent_log = (
        Path(args.intent_log).expanduser()
        if args.intent_log
        else _default_intent_log_path()
    )
    sources = args.source or ["codex", "claude", "cursor"]

    hook_statuses = []
    for source_name in sources:
        source = normalize_install_source(str(source_name))
        status = status_hooks(source)
        hook_statuses.append(
            {
                "source": status.source.value,
                "config_path": str(status.config_path),
                "installed": status.installed,
                "managed_count": status.managed_count,
                "message": status.message,
            }
        )

    queue_status = _doctor_queue_status(queue_dir)
    intent_status = _doctor_file_status(intent_log)
    payload = {
        "helper": {
            "path": str(helper.path),
            "exists": helper.exists,
            "executable": helper.executable,
            "message": helper.message,
        },
        "queue": queue_status,
        "bridge_socket": {
            "path": str(socket_path),
            "exists": socket_path.exists(),
            "message": "agent socket present" if socket_path.exists() else "agent socket missing",
        },
        "intent_log": intent_status,
        "hooks": hook_statuses,
    }
    ok = (
        helper.exists
        and helper.executable
        and bool(queue_status["writable"])
        and any(item["installed"] for item in hook_statuses)
    )
    payload["ok"] = ok

    if getattr(args, "as_json", False):
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stdout.write(f"helper: {helper.message} at {helper.path}\n")
        sys.stdout.write(
            f"queue: {queue_status['message']} at {queue_status['path']}\n"
        )
        sys.stdout.write(
            f"bridge: {payload['bridge_socket']['message']} at {socket_path}\n"
        )
        sys.stdout.write(
            f"intent log: {intent_status['message']} at {intent_status['path']}\n"
        )
        for item in hook_statuses:
            installed = "installed" if item["installed"] else "not installed"
            sys.stdout.write(
                f"{item['source']}: {installed} "
                f"({item['managed_count']} managed hooks) at {item['config_path']} "
                f"- {item['message']}\n"
            )
    return 0 if ok else 1


def _doctor_queue_status(path: Path) -> dict[str, object]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".deskmate-doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
        message = "writable"
    except OSError as exc:
        writable = False
        message = f"not writable: {exc}"
    return {
        "path": str(path),
        "exists": path.exists(),
        "writable": writable,
        "pending_events": _count_hook_event_files(path),
        "message": message,
    }


def _count_hook_event_files(path: Path) -> int:
    try:
        return len([p for p in path.glob("*.json") if not p.name.endswith(".tmp")])
    except OSError:
        return 0


def _doctor_file_status(path: Path) -> dict[str, object]:
    exists = path.exists()
    size = _safe_size(path)
    try:
        mtime_ms = int(path.stat().st_mtime * 1000) if exists else None
    except OSError:
        mtime_ms = None
    message = "present" if exists else "missing"
    return {
        "path": str(path),
        "exists": exists,
        "size_bytes": size,
        "mtime_ms": mtime_ms,
        "message": message,
    }


def _run_today(args: argparse.Namespace) -> int:
    """Print today's coding rollup."""
    db_dir = (
        Path(args.db_dir).expanduser() if args.db_dir else _default_db_dir()
    )
    db_path = db_dir / "sessions.db"
    now_ms = int(time.time() * 1000)
    midnight_ms = _local_midnight_ms(now_ms, _local_tz_offset_s())

    total_ms, by_ide = _query_today(db_path, midnight_ms)

    if args.as_json:
        sys.stdout.write(
            json.dumps(
                {
                    "total_ms": total_ms,
                    "by_ide": by_ide,
                    "db_path": str(db_path),
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    if total_ms == 0:
        sys.stdout.write("Today: nothing logged yet.\n")
        return 0
    sys.stdout.write(f"Today: {_format_duration_ms(total_ms)}\n")
    for ide, ms in by_ide.items():
        sys.stdout.write(f"  {ide:<18}  {_format_duration_ms(ms)}\n")
    return 0


def _run_memory(args: argparse.Namespace) -> int:
    db_dir = (
        Path(args.db_dir).expanduser() if args.db_dir else _default_db_dir()
    )
    conversation_id = str(args.conversation_id or "default")
    if args.memory_cmd == "summary":
        payload = _query_memory_summary(
            db_dir,
            conversation_id=conversation_id,
            limit=max(1, min(int(args.limit), 25)),
        )
        if args.as_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            return 0
        _write_memory_summary(payload)
        return 0
    if args.memory_cmd == "tool-task":
        payload = _query_tool_task_details(
            db_dir,
            conversation_id=conversation_id,
            task_id=str(args.task_id),
            limit=max(1, min(int(args.limit), 50)),
        )
        if args.as_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            return 0 if payload["found"] else 1
        _write_tool_task_details(payload)
        return 0 if payload["found"] else 1
    if args.memory_cmd == "task":
        payload = _query_deskmate_task(
            db_dir,
            conversation_id=conversation_id,
            task_id=str(args.task_id),
        )
        if args.as_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            return 0 if payload["found"] else 1
        _write_deskmate_task(payload)
        return 0 if payload["found"] else 1
    if args.memory_cmd == "task-context":
        payload = _query_deskmate_task_context(
            db_dir,
            conversation_id=conversation_id,
            task_id=str(args.task_id),
            limit=max(1, min(int(args.limit), 50)),
        )
        if args.as_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            return 0 if payload["found"] else 1
        _write_deskmate_task_context(payload)
        return 0 if payload["found"] else 1
    return 2


def _query_memory_summary(
    db_dir: Path,
    *,
    conversation_id: str,
    limit: int,
) -> dict[str, object]:
    chat_db = db_dir / "chat.db"
    profile_db = db_dir / "profile.db"
    tool_db = db_dir / "tool_actions.db"
    task_db = db_dir / "tasks.db"
    chat_summary = _query_chat_summary(chat_db, conversation_id)
    facts = _query_profile_facts(profile_db)
    task_summary = _query_task_summary(
        task_db,
        conversation_id=conversation_id,
        limit=limit,
    )
    tool_counts, recent_tasks = _query_tool_ledger_summary(
        tool_db,
        conversation_id=conversation_id,
        limit=limit,
    )
    return {
        "db_dir": str(db_dir),
        "conversation_id": conversation_id,
        "chat": chat_summary,
        "profile": {
            "facts_count": len(facts),
            "facts": facts,
        },
        "tasks": task_summary,
        "tool_ledger": {
            **tool_counts,
            "recent_tasks": recent_tasks,
        },
    }


def _query_chat_summary(db_path: Path, conversation_id: str) -> dict[str, object]:
    out: dict[str, object] = {
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "message_count": 0,
        "summary": "",
        "summary_message_count": 0,
        "updated_at_ms": None,
    }
    if not db_path.exists():
        return out
    conn = _connect_readonly(db_path)
    if conn is None:
        return out
    try:
        count_row = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        out["message_count"] = int(count_row[0]) if count_row else 0
        row = conn.execute(
            """
            SELECT summary, message_count, updated_at_ms
            FROM chat_summaries
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if row:
            out["summary"] = str(row[0])
            out["summary_message_count"] = int(row[1])
            out["updated_at_ms"] = int(row[2])
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()
    return out


def _query_profile_facts(db_path: Path) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    conn = _connect_readonly(db_path)
    if conn is None:
        return []
    try:
        row = conn.execute(
            "SELECT value_json FROM profile WHERE key = ?",
            ("memories.facts",),
        ).fetchone()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()
    if not row:
        return []
    try:
        raw = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict):
        return []
    facts: list[dict[str, object]] = []
    for key, value in sorted(raw.items()):
        if not isinstance(value, dict):
            continue
        facts.append(
            {
                "key": str(value.get("key") or key),
                "value": str(value.get("value") or ""),
                "updated_at_ms": value.get("updated_at_ms"),
                "approval_id": str(value.get("approval_id") or ""),
            }
        )
    return facts


def _query_task_summary(
    db_path: Path,
    *,
    conversation_id: str,
    limit: int,
) -> dict[str, object]:
    out: dict[str, object] = {
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "task_count": 0,
        "active_task_count": 0,
        "done_task_count": 0,
        "cancelled_task_count": 0,
        "recent_tasks": [],
    }
    if not db_path.exists():
        return out
    conn = _connect_readonly(db_path)
    if conn is None:
        return out
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN status IN ('open', 'in_progress') THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END)
            FROM deskmate_tasks
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if row:
            out["task_count"] = int(row[0] or 0)
            out["active_task_count"] = int(row[1] or 0)
            out["done_task_count"] = int(row[2] or 0)
            out["cancelled_task_count"] = int(row[3] or 0)
        rows = conn.execute(
            """
            SELECT task_id, title, status, notes, updated_at_ms, completed_at_ms
            FROM deskmate_tasks
            WHERE conversation_id = ?
            ORDER BY
                CASE status
                    WHEN 'in_progress' THEN 0
                    WHEN 'open' THEN 1
                    WHEN 'done' THEN 2
                    ELSE 3
                END,
                updated_at_ms DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
    except sqlite3.DatabaseError:
        return out
    finally:
        conn.close()
    out["recent_tasks"] = [
        {
            "task_id": str(row[0]),
            "title": str(row[1]),
            "status": str(row[2]),
            "notes": str(row[3]),
            "updated_at_ms": int(row[4]),
            "completed_at_ms": int(row[5]) if row[5] is not None else None,
        }
        for row in rows
    ]
    return out


def _query_deskmate_task(
    db_dir: Path,
    *,
    conversation_id: str,
    task_id: str,
) -> dict[str, object]:
    db_path = db_dir / "tasks.db"
    payload: dict[str, object] = {
        "db_path": str(db_path),
        "conversation_id": conversation_id,
        "task_id": task_id,
        "found": False,
        "task": None,
    }
    if not db_path.exists():
        return payload
    conn = _connect_readonly(db_path)
    if conn is None:
        return payload
    try:
        row = conn.execute(
            """
            SELECT task_id, conversation_id, title, status, notes,
                   created_at_ms, updated_at_ms, completed_at_ms
            FROM deskmate_tasks
            WHERE task_id = ? AND conversation_id = ?
            """,
            (task_id, conversation_id),
        ).fetchone()
    except sqlite3.DatabaseError:
        return payload
    finally:
        conn.close()
    if not row:
        return payload
    payload["found"] = True
    payload["task"] = {
        "task_id": str(row[0]),
        "conversation_id": str(row[1]),
        "title": str(row[2]),
        "status": str(row[3]),
        "notes": str(row[4]),
        "created_at_ms": int(row[5]),
        "updated_at_ms": int(row[6]),
        "completed_at_ms": int(row[7]) if row[7] is not None else None,
    }
    return payload


def _query_tool_ledger_summary(
    db_path: Path,
    *,
    conversation_id: str,
    limit: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    counts: dict[str, object] = {
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "action_count": 0,
        "task_count": 0,
        "running_task_count": 0,
        "failed_task_count": 0,
    }
    if not db_path.exists():
        return counts, []
    conn = _connect_readonly(db_path)
    if conn is None:
        return counts, []
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM tool_actions WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        counts["action_count"] = int(row[0]) if row else 0
        task_rows = conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
            FROM tool_tasks
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if task_rows:
            counts["task_count"] = int(task_rows[0] or 0)
            counts["running_task_count"] = int(task_rows[1] or 0)
            counts["failed_task_count"] = int(task_rows[2] or 0)
        rows = conn.execute(
            """
            SELECT task_id, status, summary, action_count, failed_count,
                   duplicate_count, updated_at_ms
            FROM tool_tasks
            WHERE conversation_id = ?
            ORDER BY updated_at_ms DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
    except sqlite3.DatabaseError:
        return counts, []
    finally:
        conn.close()
    tasks = [
        {
            "task_id": str(row[0]),
            "status": str(row[1]),
            "summary": str(row[2]),
            "action_count": int(row[3]),
            "failed_count": int(row[4]),
            "duplicate_count": int(row[5]),
            "updated_at_ms": int(row[6]),
        }
        for row in rows
    ]
    return counts, tasks


def _query_tool_task_details(
    db_dir: Path,
    *,
    conversation_id: str,
    task_id: str,
    limit: int,
) -> dict[str, object]:
    db_path = db_dir / "tool_actions.db"
    payload: dict[str, object] = {
        "db_path": str(db_path),
        "conversation_id": conversation_id,
        "task_id": task_id,
        "found": False,
        "task": None,
        "actions": [],
    }
    if not db_path.exists():
        return payload
    conn = _connect_readonly(db_path)
    if conn is None:
        return payload
    try:
        task_row = conn.execute(
            """
            SELECT task_id, conversation_id, user_text, status, summary,
                   action_count, failed_count, duplicate_count,
                   started_at_ms, updated_at_ms, completed_at_ms
            FROM tool_tasks
            WHERE task_id = ? AND conversation_id = ?
            """,
            (task_id, conversation_id),
        ).fetchone()
        if not task_row:
            return payload
        action_rows = conn.execute(
            """
            SELECT tool_call_id, tool_name, status, summary_json, result,
                   started_at_ms, completed_at_ms
            FROM tool_actions
            WHERE task_id = ? AND conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (task_id, conversation_id, limit),
        ).fetchall()
    except sqlite3.DatabaseError:
        return payload
    finally:
        conn.close()
    payload["found"] = True
    payload["task"] = {
        "task_id": str(task_row[0]),
        "conversation_id": str(task_row[1]),
        "user_text": str(task_row[2]),
        "status": str(task_row[3]),
        "summary": str(task_row[4]),
        "action_count": int(task_row[5]),
        "failed_count": int(task_row[6]),
        "duplicate_count": int(task_row[7]),
        "started_at_ms": int(task_row[8]),
        "updated_at_ms": int(task_row[9]),
        "completed_at_ms": int(task_row[10]) if task_row[10] is not None else None,
    }
    payload["actions"] = [
        {
            "tool_call_id": str(row[0]),
            "tool_name": str(row[1]),
            "status": str(row[2]),
            "summary": _json_loads_dict(row[3]),
            "result": str(row[4]),
            "started_at_ms": int(row[5]),
            "completed_at_ms": int(row[6]),
        }
        for row in reversed(action_rows)
    ]
    return payload


def _query_deskmate_task_context(
    db_dir: Path,
    *,
    conversation_id: str,
    task_id: str,
    limit: int,
) -> dict[str, object]:
    base = _query_deskmate_task(
        db_dir,
        conversation_id=conversation_id,
        task_id=task_id,
    )
    payload: dict[str, object] = {
        **base,
        "tool_db_path": str(db_dir / "tool_actions.db"),
        "task_steps": [],
        "related_tool_tasks": [],
        "related_tool_actions": [],
        "related_tool_lessons": [],
    }
    task = base.get("task")
    if not base.get("found") or not isinstance(task, dict):
        return payload
    task_steps = _query_deskmate_task_steps(
        db_dir,
        conversation_id=conversation_id,
        task_id=task_id,
    )
    payload["task_steps"] = task_steps
    tool_db = db_dir / "tool_actions.db"
    if not tool_db.exists():
        return payload
    conn = _connect_readonly(tool_db)
    if conn is None:
        return payload
    terms = _task_context_terms(
        str(task.get("title") or ""),
        str(task.get("notes") or ""),
        task_steps,
    )
    try:
        tool_tasks: list[dict[str, object]] = []
        tool_actions: list[dict[str, object]] = []
        tool_lessons: list[dict[str, object]] = []
        seen_tool_tasks: set[str] = set()
        seen_actions: set[str] = set()
        seen_lessons: set[str] = set()
        rows = conn.execute(
            """
            SELECT tool_call_id, task_id, tool_name, status,
                   summary_json, result, completed_at_ms
            FROM tool_actions
            WHERE conversation_id = ? AND task_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, task_id, limit),
        ).fetchall()
        for row in reversed(rows):
            key = str(row[0])
            if key in seen_actions:
                continue
            seen_actions.add(key)
            tool_actions.append(
                {
                    "tool_call_id": key,
                    "task_id": str(row[1]) if row[1] is not None else "",
                    "tool_name": str(row[2]),
                    "status": str(row[3]),
                    "summary": _json_loads_dict(row[4]),
                    "result": str(row[5]),
                    "completed_at_ms": int(row[6]),
                }
            )
        if _table_exists(conn, "tool_lessons"):
            rows = conn.execute(
                """
                SELECT lesson_key, tool_name, target, outcome, status,
                       needs_user, lesson, task_id, updated_at_ms, seen_count
                FROM tool_lessons
                WHERE conversation_id = ? AND task_id = ?
                ORDER BY updated_at_ms DESC
                LIMIT ?
                """,
                (conversation_id, task_id, limit),
            ).fetchall()
            for row in reversed(rows):
                key = str(row[0])
                if key in seen_lessons:
                    continue
                seen_lessons.add(key)
                tool_lessons.append(
                    {
                        "lesson_key": key,
                        "tool_name": str(row[1]),
                        "target": str(row[2]),
                        "outcome": str(row[3]),
                        "status": str(row[4]),
                        "needs_user": bool(row[5]),
                        "lesson": str(row[6]),
                        "task_id": str(row[7]) if row[7] is not None else "",
                        "updated_at_ms": int(row[8]),
                        "seen_count": int(row[9]),
                    }
                )
        for term in terms:
            like = f"%{_escape_like(term)}%"
            if len(tool_tasks) < limit:
                rows = conn.execute(
                    """
                    SELECT task_id, status, summary, action_count,
                           failed_count, duplicate_count, updated_at_ms
                    FROM tool_tasks
                    WHERE conversation_id = ?
                      AND (
                        task_id LIKE ? ESCAPE '\\'
                        OR user_text LIKE ? ESCAPE '\\'
                        OR summary LIKE ? ESCAPE '\\'
                      )
                    ORDER BY updated_at_ms DESC
                    LIMIT ?
                    """,
                    (conversation_id, like, like, like, limit),
                ).fetchall()
                for row in rows:
                    key = str(row[0])
                    if key in seen_tool_tasks:
                        continue
                    seen_tool_tasks.add(key)
                    tool_tasks.append(
                        {
                            "task_id": key,
                            "status": str(row[1]),
                            "summary": str(row[2]),
                            "action_count": int(row[3]),
                            "failed_count": int(row[4]),
                            "duplicate_count": int(row[5]),
                            "updated_at_ms": int(row[6]),
                        }
                    )
                    if len(tool_tasks) >= limit:
                        break
            if len(tool_actions) < limit:
                rows = conn.execute(
                    """
                    SELECT tool_call_id, task_id, tool_name, status,
                           summary_json, result, completed_at_ms
                    FROM tool_actions
                    WHERE conversation_id = ?
                      AND (
                        tool_name LIKE ? ESCAPE '\\'
                        OR task_id LIKE ? ESCAPE '\\'
                        OR result LIKE ? ESCAPE '\\'
                        OR arguments_json LIKE ? ESCAPE '\\'
                        OR summary_json LIKE ? ESCAPE '\\'
                      )
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (conversation_id, like, like, like, like, like, limit),
                ).fetchall()
                for row in rows:
                    key = str(row[0])
                    if key in seen_actions:
                        continue
                    seen_actions.add(key)
                    tool_actions.append(
                        {
                            "tool_call_id": key,
                            "task_id": str(row[1]) if row[1] is not None else "",
                            "tool_name": str(row[2]),
                            "status": str(row[3]),
                            "summary": _json_loads_dict(row[4]),
                            "result": str(row[5]),
                            "completed_at_ms": int(row[6]),
                        }
                    )
                    if len(tool_actions) >= limit:
                        break
            if len(tool_lessons) < limit and _table_exists(conn, "tool_lessons"):
                rows = conn.execute(
                    """
                    SELECT lesson_key, tool_name, target, outcome, status,
                           needs_user, lesson, task_id, updated_at_ms, seen_count
                    FROM tool_lessons
                    WHERE conversation_id = ?
                      AND (
                        tool_name LIKE ? ESCAPE '\\'
                        OR target LIKE ? ESCAPE '\\'
                        OR outcome LIKE ? ESCAPE '\\'
                        OR lesson LIKE ? ESCAPE '\\'
                      )
                    ORDER BY updated_at_ms DESC
                    LIMIT ?
                    """,
                    (conversation_id, like, like, like, like, limit),
                ).fetchall()
                for row in rows:
                    key = str(row[0])
                    if key in seen_lessons:
                        continue
                    seen_lessons.add(key)
                    tool_lessons.append(
                        {
                            "lesson_key": key,
                            "tool_name": str(row[1]),
                            "target": str(row[2]),
                            "outcome": str(row[3]),
                            "status": str(row[4]),
                            "needs_user": bool(row[5]),
                            "lesson": str(row[6]),
                            "task_id": str(row[7]) if row[7] is not None else "",
                            "updated_at_ms": int(row[8]),
                            "seen_count": int(row[9]),
                        }
                    )
                    if len(tool_lessons) >= limit:
                        break
            if (
                len(tool_tasks) >= limit
                and len(tool_actions) >= limit
                and len(tool_lessons) >= limit
            ):
                break
    except sqlite3.DatabaseError:
        return payload
    finally:
        conn.close()
    payload["related_tool_tasks"] = tool_tasks
    payload["related_tool_actions"] = tool_actions
    payload["related_tool_lessons"] = tool_lessons
    return payload


def _query_deskmate_task_steps(
    db_dir: Path,
    *,
    conversation_id: str,
    task_id: str,
) -> list[dict[str, object]]:
    db_path = db_dir / "tasks.db"
    if not db_path.exists():
        return []
    conn = _connect_readonly(db_path)
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "deskmate_task_steps"):
            return []
        rows = conn.execute(
            """
            SELECT step_id, position, content, status, active_form,
                   created_at_ms, updated_at_ms, completed_at_ms
            FROM deskmate_task_steps
            WHERE task_id = ? AND conversation_id = ?
            ORDER BY position ASC
            """,
            (task_id, conversation_id),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()
    return [
        {
            "step_id": str(row[0]),
            "position": int(row[1]),
            "content": str(row[2]),
            "status": str(row[3]),
            "active_form": str(row[4]),
            "created_at_ms": int(row[5]),
            "updated_at_ms": int(row[6]),
            "completed_at_ms": int(row[7]) if row[7] is not None else None,
        }
        for row in rows
    ]


def _write_memory_summary(payload: dict[str, object]) -> None:
    chat = payload["chat"]
    profile = payload["profile"]
    tasks = payload["tasks"]
    ledger = payload["tool_ledger"]
    assert isinstance(chat, dict)
    assert isinstance(profile, dict)
    assert isinstance(tasks, dict)
    assert isinstance(ledger, dict)
    sys.stdout.write(f"Memory diagnostics for {payload['conversation_id']}\n")
    sys.stdout.write(
        f"chat: {chat['message_count']} messages"
        + (
            f", summary covers {chat['summary_message_count']}"
            if chat.get("summary")
            else ", no summary"
        )
        + "\n"
    )
    sys.stdout.write(f"profile facts: {profile['facts_count']}\n")
    facts = profile.get("facts")
    if isinstance(facts, list):
        for fact in facts[:10]:
            if isinstance(fact, dict):
                sys.stdout.write(f"  {fact['key']}: {fact['value']}\n")
    sys.stdout.write(
        f"tasks: {tasks['task_count']} total, "
        f"{tasks['active_task_count']} active, "
        f"{tasks['done_task_count']} done, "
        f"{tasks['cancelled_task_count']} cancelled\n"
    )
    recent_user_tasks = tasks.get("recent_tasks")
    if isinstance(recent_user_tasks, list) and recent_user_tasks:
        sys.stdout.write("recent tasks:\n")
        for task in recent_user_tasks:
            if isinstance(task, dict):
                notes = f" - {task['notes']}" if task.get("notes") else ""
                sys.stdout.write(
                    f"  {task['task_id']} [{task['status']}]: {task['title']}{notes}\n"
                )
    sys.stdout.write(
        f"tool ledger: {ledger['task_count']} tasks, "
        f"{ledger['action_count']} actions, "
        f"{ledger['running_task_count']} running, "
        f"{ledger['failed_task_count']} failed\n"
    )
    recent = ledger.get("recent_tasks")
    if isinstance(recent, list) and recent:
        sys.stdout.write("recent tool tasks:\n")
        for task in recent:
            if isinstance(task, dict):
                sys.stdout.write(
                    f"  {task['task_id']} [{task['status']}]: {task['summary']}\n"
                )


def _write_deskmate_task(payload: dict[str, object]) -> None:
    if not payload.get("found"):
        sys.stderr.write(f"No task {payload['task_id']} for {payload['conversation_id']}\n")
        return
    task = payload.get("task")
    assert isinstance(task, dict)
    notes = f"\nnotes: {task['notes']}" if task.get("notes") else ""
    sys.stdout.write(
        f"{task['task_id']} [{task['status']}]: {task['title']}{notes}\n"
    )
    sys.stdout.write(
        f"created_at_ms={task['created_at_ms']} "
        f"updated_at_ms={task['updated_at_ms']} "
        f"completed_at_ms={task['completed_at_ms']}\n"
    )


def _write_tool_task_details(payload: dict[str, object]) -> None:
    if not payload.get("found"):
        sys.stderr.write(f"No tool task {payload['task_id']} for {payload['conversation_id']}\n")
        return
    task = payload.get("task")
    actions = payload.get("actions")
    assert isinstance(task, dict)
    sys.stdout.write(
        f"{task['task_id']} [{task['status']}]: {task['summary']}\n"
    )
    sys.stdout.write(
        f"actions={task['action_count']} failed={task['failed_count']} "
        f"duplicate={task['duplicate_count']}\n"
    )
    if not isinstance(actions, list) or not actions:
        sys.stdout.write("actions: none\n")
        return
    sys.stdout.write("actions:\n")
    for action in actions:
        if not isinstance(action, dict):
            continue
        summary = action.get("summary")
        target = ""
        outcome = action.get("result", "")
        if isinstance(summary, dict):
            target = str(summary.get("target") or "")
            outcome = str(summary.get("outcome") or outcome)
        target_part = f" target={target}" if target else ""
        sys.stdout.write(
            f"  {action['tool_name']} [{action['status']}]{target_part}: {outcome}\n"
        )


def _write_deskmate_task_context(payload: dict[str, object]) -> None:
    if not payload.get("found"):
        sys.stderr.write(f"No task {payload['task_id']} for {payload['conversation_id']}\n")
        return
    _write_deskmate_task(payload)
    task_steps = payload.get("task_steps")
    tool_tasks = payload.get("related_tool_tasks")
    tool_actions = payload.get("related_tool_actions")
    tool_lessons = payload.get("related_tool_lessons")
    sys.stdout.write("task steps:\n")
    if isinstance(task_steps, list) and task_steps:
        for step in task_steps:
            if isinstance(step, dict):
                active = (
                    f" -> {step['active_form']}"
                    if step.get("active_form")
                    else ""
                )
                sys.stdout.write(
                    f"  {step['position']}. [{step['status']}] "
                    f"{step['content']}{active}\n"
                )
    else:
        sys.stdout.write("  none\n")
    sys.stdout.write("related tool tasks:\n")
    if isinstance(tool_tasks, list) and tool_tasks:
        for task in tool_tasks:
            if isinstance(task, dict):
                sys.stdout.write(
                    f"  {task['task_id']} [{task['status']}]: {task['summary']}\n"
                )
    else:
        sys.stdout.write("  none\n")
    sys.stdout.write("related tool actions:\n")
    if isinstance(tool_actions, list) and tool_actions:
        for action in tool_actions:
            if not isinstance(action, dict):
                continue
            summary = action.get("summary")
            target = ""
            outcome = action.get("result", "")
            if isinstance(summary, dict):
                target = str(summary.get("target") or "")
                outcome = str(summary.get("outcome") or outcome)
            target_part = f" target={target}" if target else ""
            sys.stdout.write(
                f"  {action['tool_name']} [{action['status']}]{target_part}: {outcome}\n"
            )
    else:
        sys.stdout.write("  none\n")
    sys.stdout.write("related tool lessons:\n")
    if isinstance(tool_lessons, list) and tool_lessons:
        for lesson in tool_lessons:
            if isinstance(lesson, dict):
                seen = (
                    f" seen={lesson['seen_count']}"
                    if int(lesson.get("seen_count") or 0) > 1
                    else ""
                )
                sys.stdout.write(
                    f"  {lesson['tool_name']} [{lesson['status']}] "
                    f"target={lesson['target']}: {lesson['outcome']}{seen}\n"
                )
    else:
        sys.stdout.write("  none\n")


def _connect_readonly(db_path: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None


def _json_loads_dict(value: object) -> dict[str, object]:
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None


def _task_context_terms(
    title: str,
    notes: str,
    steps: list[dict[str, object]] | None = None,
) -> list[str]:
    step_terms: list[str] = []
    for step in (steps or [])[:8]:
        active_form = str(step.get("active_form") or "")
        content = str(step.get("content") or "")
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


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _projects_json_path() -> Path:
    override = os.environ.get("DESKMATE_PROJECTS_PATH")
    if override:
        return Path(override).expanduser()
    return _default_db_dir() / "projects.json"


def _run_project(args: argparse.Namespace) -> int:
    """Dispatch the ``deskmate project *`` subcommands."""
    from .projects import ProjectRegistry

    registry = ProjectRegistry(path=_projects_json_path())
    registry.load()

    cmd = args.project_cmd
    if cmd == "add":
        path = Path(args.path).expanduser().resolve()
        name = (args.name or path.name).strip()
        if not name:
            sys.stderr.write("error: --name must be non-empty\n")
            return 2
        hints = tuple(h for h in args.bundle_hint if h)
        entry = registry.add(name, path, bundle_hints=hints)
        sys.stdout.write(
            f"Registered {entry.name} → {entry.path}"
            + (
                f" (hints: {', '.join(entry.bundle_hints)})"
                if entry.bundle_hints
                else ""
            )
            + "\n"
        )
        return 0

    if cmd == "list":
        entries = registry.list_all()
        if args.as_json:
            sys.stdout.write(
                json.dumps(
                    [e.as_dict() for e in entries], indent=2
                )
                + "\n"
            )
            return 0
        if not entries:
            sys.stdout.write("No projects registered.\n")
            return 0
        for e in entries:
            hint_suffix = (
                f"  [{', '.join(e.bundle_hints)}]" if e.bundle_hints else ""
            )
            sys.stdout.write(f"  {e.name:<16}  {e.path}{hint_suffix}\n")
        return 0

    if cmd == "remove":
        removed = registry.remove(args.name)
        if removed:
            sys.stdout.write(f"Removed {args.name}\n")
            return 0
        sys.stderr.write(f"No project named {args.name}\n")
        return 1

    # argparse enforces project_cmd is one of the above
    return 2  # pragma: no cover


def _default_intent_log_path() -> Path:
    override = os.environ.get("DESKMATE_INTENT_LOG_PATH")
    if override:
        return Path(override).expanduser()
    return _default_db_dir() / "intents.jsonl"


def _run_tail_status(args: argparse.Namespace) -> int:
    """Stream the running agent's intent log to stdout."""
    path = (
        Path(args.path).expanduser() if args.path else _default_intent_log_path()
    )
    # Seed with the last ``--lines`` lines so the user sees recent
    # context even if the agent's been running a while.
    offset = _write_seed_lines(path, lines=max(0, args.lines))

    if args.once:
        return 0

    poll_s = max(0.01, args.poll_ms / 1000)
    try:
        while True:
            offset = _drain_new_lines(path, offset)
            time.sleep(poll_s)
    except KeyboardInterrupt:
        return 130


def _write_seed_lines(path: Path, *, lines: int) -> int:
    """Print the tail of ``path`` and return the resulting offset."""
    if not path.exists() or lines <= 0:
        return _safe_size(path)
    try:
        with path.open("rb") as fh:
            # Cheap tail: read the last ~64 KB, split on newline,
            # take the last ``lines`` entries. Avoids seeking back
            # through multi-MB logs just to show a few trailing rows.
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            read_bytes = min(size, 64 * 1024)
            fh.seek(size - read_bytes)
            chunk = fh.read()
            offset = fh.tell()
    except OSError:
        return _safe_size(path)
    text = chunk.decode("utf-8", errors="replace")
    tail = text.splitlines()[-lines:]
    for line in tail:
        if line.strip():
            sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return offset


def _drain_new_lines(path: Path, offset: int) -> int:
    size = _safe_size(path)
    if size < offset:
        # File was rotated / truncated — rewind.
        offset = 0
    if size <= offset:
        return offset
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
            new_offset = fh.tell()
    except FileNotFoundError:
        return 0
    except OSError:
        return offset
    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.strip():
            sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return new_offset


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


def _query_today(
    db_path: Path, midnight_ms: int
) -> tuple[int, dict[str, int]]:
    """Synchronous read from the coding-session log.

    Returns ``(total_ms, {ide: ms, ...})`` — an empty pair if the
    database hasn't been created yet (fresh install, agent never
    ran) so the CLI degrades politely.
    """
    if not db_path.exists():
        return 0, {}
    # Open read-only so the CLI can run concurrently with the agent
    # without any lock contention risk.
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return 0, {}
    try:
        total_row = conn.execute(
            "SELECT COALESCE(SUM(duration_ms), 0) FROM coding_sessions "
            "WHERE ended_at_ms >= ?",
            (midnight_ms,),
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        breakdown_rows = conn.execute(
            "SELECT ide, SUM(duration_ms) FROM coding_sessions "
            "WHERE ended_at_ms >= ? GROUP BY ide ORDER BY SUM(duration_ms) DESC",
            (midnight_ms,),
        ).fetchall()
        by_ide = {str(r[0]): int(r[1]) for r in breakdown_rows}
    except sqlite3.DatabaseError:
        return 0, {}
    finally:
        conn.close()
    return total, by_ide


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
