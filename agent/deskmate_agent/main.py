"""Entry point: ``python -m deskmate_agent`` (V10 Phase 1d).

Responsibilities:

- Install ``uvloop`` when available (V10 L3-B8).
- Configure structured logging.
- Resolve default paths on macOS.
- Drive :class:`App.setup` / :meth:`App.serve_forever`.

Keep this module *dependency-light* so it doubles as a manual smoke script
for the agent on machines without the Swift shell running.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from .app import App, AppConfig
from .bridge import default_socket_path
from .logging_setup import configure_logging, get_logger

_LOG = get_logger("deskmate_agent.main")


def install_uvloop_if_available() -> bool:
    """Install uvloop if it's available (macOS only, best-effort)."""
    try:
        import uvloop  # type: ignore[import-not-found]
    except ImportError:
        return False
    uvloop.install()
    return True


def default_db_dir() -> Path:
    override = os.environ.get("DESKMATE_DB_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "Deskmate"


def _resolved_socket_path() -> Path:
    override = os.environ.get("DESKMATE_SOCKET_PATH")
    if override:
        return Path(override).expanduser()
    return default_socket_path()


def _codex_app_server_enabled() -> bool:
    raw = os.environ.get("DESKMATE_CODEX_APP_SERVER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


async def run() -> None:
    install_uvloop_if_available()
    configure_logging()
    config = AppConfig(
        socket_path=_resolved_socket_path(),
        db_dir=default_db_dir(),
        codex_app_server_enabled=_codex_app_server_enabled(),
    )
    app = App(config)
    runtime = await app.setup()
    _LOG.info(
        "deskmate_agent.started",
        socket=str(config.socket_path),
        db_dir=str(config.db_dir),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Signal handlers aren't available on non-Unix event loops.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    serve_task = asyncio.create_task(app.serve_forever())
    _ = runtime  # keep reference to prevent lint "unused" warnings
    await stop.wait()
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await serve_task
    await app.teardown()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
