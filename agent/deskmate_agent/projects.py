"""Project registry — user-registered path ↔ display-name mapping
used by the coding-session tracker to enrich the island detail with
the current git branch (V10 Phase 15-ii).

Layout on disk (``$DESKMATE_DB_DIR/projects.json`` by default):

.. code-block:: json

    {
      "entries": [
        {
          "name": "deskmate",
          "path": "/Users/alice/projects/deskmate",
          "bundle_hints": ["com.microsoft.VSCode", "com.exafunction.windsurf"]
        }
      ]
    }

The registry is:

- **Read-only on the agent's hot path.** The CodingSessionTracker
  just asks "does any entry match this window?" per perception tick;
  writes happen only through the CLI.
- **Best-effort on I/O.** Corrupted JSON / missing file → empty
  registry instead of raising.
- **Honest about bundle hints.** When an entry registers a bundle
  id hint, the resolver only considers it for matching IDEs; that
  prevents ``my-project`` in a Safari window title from picking up
  the Windsurf branch.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .git_branch import current_branch
from .logging_setup import get_logger

_LOG = get_logger("deskmate_agent.projects")


@dataclass(frozen=True)
class ProjectEntry:
    name: str
    path: str
    bundle_hints: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "bundle_hints": list(self.bundle_hints),
        }


@dataclass(frozen=True)
class ResolvedProject:
    name: str
    path: Path
    branch: str | None


@dataclass
class ProjectRegistry:
    path: Path
    entries: list[ProjectEntry] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Populate :attr:`entries` from disk; silent on failure."""
        self.entries = []
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text()
        except OSError as exc:
            _LOG.warning("project_registry.read_failed", error=str(exc))
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOG.warning(
                "project_registry.decode_failed",
                error=str(exc),
                path=str(self.path),
            )
            return
        parsed: list[ProjectEntry] = []
        for item in data.get("entries", []) or []:
            try:
                parsed.append(
                    ProjectEntry(
                        name=str(item["name"]),
                        path=str(item["path"]),
                        bundle_hints=tuple(
                            str(h)
                            for h in (item.get("bundle_hints") or ())
                            if h
                        ),
                    )
                )
            except (KeyError, TypeError):
                continue
        self.entries = parsed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"entries": [e.as_dict() for e in self.entries]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add(
        self,
        name: str,
        path: Path | str,
        bundle_hints: Iterable[str] = (),
    ) -> ProjectEntry:
        """Upsert an entry keyed by ``name``. Returns the stored row."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("project name must be non-empty")
        resolved = Path(path).expanduser().resolve()
        entry = ProjectEntry(
            name=clean_name,
            path=str(resolved),
            bundle_hints=tuple(
                h.strip() for h in bundle_hints if h and h.strip()
            ),
        )
        self.entries = [e for e in self.entries if e.name != clean_name]
        self.entries.append(entry)
        self.save()
        return entry

    def remove(self, name: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.name != name]
        if len(self.entries) == before:
            return False
        self.save()
        return True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_all(self) -> list[ProjectEntry]:
        return list(self.entries)

    def resolve(
        self,
        *,
        bundle_id: str | None,
        window_title: str | None,
        branch_reader: Callable[[Path], str | None] = current_branch,
    ) -> ResolvedProject | None:
        """Find the best entry matching ``window_title`` and read its
        git branch. Returns ``None`` when nothing matches.

        Resolution policy:

        1. If any entries carry ``bundle_hints`` that include
           ``bundle_id``, they become the candidate set. Otherwise all
           entries are candidates.
        2. Longest-name substring match on ``window_title``
           (case-insensitive) wins — a window title ``"foo.py — my-app"``
           picks ``my-app`` over ``app`` when both are registered.
        3. The selected entry's :attr:`path` is passed to
           ``branch_reader`` (defaults to :func:`current_branch`) to
           compute the branch shown in the pill.
        """
        if not self.entries:
            return None
        candidates: Sequence[ProjectEntry] = self.entries
        if bundle_id:
            hinted = [e for e in candidates if bundle_id in e.bundle_hints]
            if hinted:
                candidates = hinted
        if not window_title:
            return None
        haystack = window_title.lower()
        best: ProjectEntry | None = None
        for entry in candidates:
            needle = entry.name.lower()
            if needle and needle in haystack and (
                best is None or len(entry.name) > len(best.name)
            ):
                best = entry
        if best is None:
            return None
        branch = None
        try:
            branch = branch_reader(Path(best.path))
        except Exception as exc:  # noqa: BLE001 — fail-soft
            _LOG.debug(
                "project_registry.branch_read_failed",
                error=str(exc),
                path=best.path,
            )
        return ResolvedProject(
            name=best.name, path=Path(best.path), branch=branch
        )


ProjectResolver = Callable[
    [str | None, str | None], ResolvedProject | None
]

__all__ = [
    "ProjectEntry",
    "ProjectRegistry",
    "ProjectResolver",
    "ResolvedProject",
]
