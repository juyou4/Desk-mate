"""Default bridge socket path (V10 / protocol.md §1)."""

from __future__ import annotations

from pathlib import Path


def default_socket_path() -> Path:
    """``~/Library/Application Support/Deskmate/ipc.sock`` on macOS."""
    return Path.home() / "Library" / "Application Support" / "Deskmate" / "ipc.sock"


__all__ = ["default_socket_path"]
