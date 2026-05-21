"""Small command-line helper (V10 Phase 14-i / 14-iii).

Two families today:

- ``build-*`` — drop a JSON line into ``~/.deskmate/build-status.json``
  that the running agent picks up to drive the island build pill.
- ``hook ingest`` — normalize external agent hook JSON into the file queue
  consumed by the resident Python agent.
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
    deskmate today            # human readable summary
    deskmate today --json     # machine readable

``DESKMATE_BUILD_STATUS_PATH`` overrides the build-status target path
if you need to redirect (e.g. tests). ``DESKMATE_DB_DIR`` overrides
the agent's SQLite directory the ``today`` subcommand queries.
"""

from __future__ import annotations

import argparse
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
    elif args.command == "tail-status":
        return _run_tail_status(args)
    elif args.command == "project":
        return _run_project(args)
    elif args.command == "hook":
        return _run_hook(args)
    else:  # pragma: no cover — argparse enforces this
        return 2
    _write(payload)
    # Quiet by default — Makefile integrations don't want extra noise.
    return 0


def _run_hook(args: argparse.Namespace) -> int:
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
