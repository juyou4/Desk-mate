"""Pure-Python git branch lookup (V10 Phase 14-ii).

Avoids shelling out to ``git`` so the ``deskmate`` CLI stays fast
(sub-ms), dependency-free, and usable in environments where git may
not be on ``PATH`` (minimal containers, locked-down corporate
workstations, …).

Parses ``.git/HEAD`` directly per the gitformat-packfile spec. Also
handles the two wrappers git uses for non-standard layouts:

- Worktrees: the repo's ``.git`` is a file that points at
  ``<main>/worktrees/<name>`` via a ``gitdir: …`` line.
- Submodules: similar ``gitdir: …`` pointer to the superproject's
  ``.git/modules/<name>``.
"""

from __future__ import annotations

from pathlib import Path


def current_branch(start: Path | None = None) -> str | None:
    """Return the branch name for ``start`` (default cwd), ``None``
    if the path isn't inside a git repo.

    Detached HEADs return the first 7 chars of the commit sha so the
    user still gets a meaningful label in the island pill.
    """
    here = Path(start) if start is not None else Path.cwd()
    try:
        here = here.resolve()
    except OSError:
        return None

    for candidate in [here, *here.parents]:
        head_path = _locate_head(candidate)
        if head_path is None:
            continue
        return _parse_head(head_path)
    return None


def _locate_head(candidate: Path) -> Path | None:
    git_entry = candidate / ".git"
    if git_entry.is_dir():
        head = git_entry / "HEAD"
        return head if head.exists() else None
    if git_entry.is_file():
        # Worktree / submodule indirection: file with
        # ``gitdir: /path/to/real/gitdir`` pointer.
        try:
            pointer = git_entry.read_text().strip()
        except OSError:
            return None
        if not pointer.startswith("gitdir: "):
            return None
        gitdir = Path(pointer[len("gitdir: "):].strip())
        if not gitdir.is_absolute():
            gitdir = (candidate / gitdir).resolve()
        head = gitdir / "HEAD"
        return head if head.exists() else None
    return None


def _parse_head(head: Path) -> str | None:
    try:
        content = head.read_text().strip()
    except OSError:
        return None
    if content.startswith("ref: refs/heads/"):
        branch = content[len("ref: refs/heads/"):].strip()
        return branch or None
    # Detached HEAD — return short sha.
    sha = content.split()[0] if content else ""
    return sha[:7] if sha else None


__all__ = ["current_branch"]
