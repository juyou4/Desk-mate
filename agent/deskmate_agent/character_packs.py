"""Character pack discovery + activation (V10 Phase 8).

Phase 7 shipped the pure :class:`AvatarRenderer` + :class:`AvatarView`
duo but wired the active *style* through a process env var, because
no loader existed to read an actual :class:`CharacterPackManifest`
off disk. Phase 8 closes that gap:

- :func:`load_manifest` reads a single ``manifest.json`` and
  validates it via the already-shipped Pydantic model.
- :func:`discover_packs` walks a packs root (``~/.deskmate/packs`` by
  default) for ``<pack_id>/manifest.json`` entries, collecting the
  successful ones and quietly dropping unreadable / invalid packs.
- :class:`CharacterPackRegistry` holds the resulting catalog and
  answers ``select_active_pack(preferred_id)`` against a sensible
  fallback order.

The loader is intentionally isolated from Swift / UI concerns — it
produces a plain :class:`CharacterPackManifest`. Downstream code
(:class:`AppRuntime`, the Swift bridge) decides *what* to do with
the active pack.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import get_logger
from .protocol.character_pack import (
    DEFAULT_REQUIRED_STATES,
    SPEC_VERSION,
    CharacterPackManifest,
)

_LOGGER = get_logger(__name__)

#: Default packs root on macOS. Honours ``$XDG_DATA_HOME`` on Linux
#: for devs who run the agent under XDG-respecting environments.
DEFAULT_PACKS_DIR_ENV = "DESKMATE_PACKS_DIR"

#: Env var that overrides which pack id is picked as active. When
#: unset the registry falls back to the built-in default id, then to
#: the first registered pack.
ACTIVE_PACK_ENV = "DESKMATE_CHARACTER_PACK"

#: The id the primary built-in pack advertises; kept as a module
#: constant so tests and production pick from the same source.
BUILTIN_PACK_ID = "deskmate_native"

#: Lightweight legacy fallback kept for older installs and low-resource
#: environments.
LEGACY_PIXEL_PACK_ID = "pixel_default"


class CharacterPackError(ValueError):
    """Raised when a manifest fails structural validation.

    Used only by the explicit :func:`load_manifest` path —
    :func:`discover_packs` swallows these into log lines so one bad
    pack can't take down the whole agent boot.
    """


# ---------------------------------------------------------------------------
# Single-manifest loader
# ---------------------------------------------------------------------------


def load_manifest(path: str | os.PathLike[str]) -> CharacterPackManifest:
    """Read + validate one ``manifest.json``.

    Raises :class:`CharacterPackError` with a short, human-oriented
    message on any failure. Call sites that can tolerate a bad pack
    should use :func:`discover_packs` (which logs + drops instead).
    """
    p = Path(path)
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CharacterPackError(
            f"{p}: cannot read manifest ({exc})"
        ) from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CharacterPackError(
            f"{p}: manifest is not valid JSON ({exc.msg} "
            f"at line {exc.lineno}:{exc.colno})"
        ) from exc
    if not isinstance(raw, dict):
        raise CharacterPackError(
            f"{p}: manifest root must be an object, got "
            f"{type(raw).__name__}"
        )

    try:
        manifest = CharacterPackManifest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError etc.
        raise CharacterPackError(
            f"{p}: manifest failed schema validation: {exc}"
        ) from exc

    # A mismatching ``spec_version`` is a hard stop even though
    # Pydantic let it through — we don't know what new rules a future
    # version might impose. Older packs that never wrote the field
    # end up at SPEC_VERSION by default.
    if manifest.spec_version != SPEC_VERSION:
        raise CharacterPackError(
            f"{p}: unsupported spec_version {manifest.spec_version} "
            f"(expected {SPEC_VERSION})"
        )

    missing = manifest.missing_required_states()
    if missing:
        raise CharacterPackError(
            f"{p}: pack {manifest.id!r} is missing required states "
            f"{missing!r}"
        )

    return manifest


# ---------------------------------------------------------------------------
# Directory discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackDiscoveryResult:
    """Outcome of walking a packs root.

    Kept separate from the registry so callers can log skipped packs
    without reaching into the registry's internals. ``skipped`` is a
    dict of ``pack_path → human-readable reason`` — useful for the
    menu bar's "packs" status pane (future phase).
    """

    packs: tuple[CharacterPackManifest, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)


def discover_packs(root: str | os.PathLike[str]) -> PackDiscoveryResult:
    """Load every ``<root>/<pack_id>/manifest.json`` under ``root``.

    Non-existent roots, unreadable packs, and invalid manifests are
    all treated as soft failures: the function always returns a
    :class:`PackDiscoveryResult`, never raises.
    """
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        return PackDiscoveryResult()

    packs: list[CharacterPackManifest] = []
    skipped: dict[str, str] = {}
    # Sorted so the ordering — and therefore the "first registered
    # wins" fallback — is deterministic across filesystems.
    for child in sorted(root_path.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            skipped[str(child)] = "missing manifest.json"
            continue
        try:
            packs.append(load_manifest(manifest_path))
        except CharacterPackError as exc:
            _LOGGER.warning(
                "character_pack.skipped",
                path=str(manifest_path),
                reason=str(exc),
            )
            skipped[str(manifest_path)] = str(exc)

    # De-duplicate by id: later entries win (consistent with dict
    # semantics in the registry). A warning is emitted so pack
    # authors notice collisions during development.
    by_id: dict[str, CharacterPackManifest] = {}
    for pack in packs:
        if pack.id in by_id:
            _LOGGER.warning(
                "character_pack.duplicate_id",
                pack_id=pack.id,
            )
        by_id[pack.id] = pack
    return PackDiscoveryResult(
        packs=tuple(by_id.values()),
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Registry + active pack selection
# ---------------------------------------------------------------------------


class CharacterPackRegistry:
    """In-memory catalog of loaded :class:`CharacterPackManifest`s.

    Separate from the global :class:`deskmate_agent.skills.SkillRegistry`
    because character packs and skills ship on different lifecycles
    (packs are bulky resources shipped as folders; skills are
    metadata records).
    """

    def __init__(
        self, packs: Iterable[CharacterPackManifest] = (),
    ) -> None:
        self._packs: dict[str, CharacterPackManifest] = {}
        self._order: list[str] = []
        for pack in packs:
            self.register(pack)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(self, manifest: CharacterPackManifest) -> None:
        """Insert (or replace) a manifest. Insertion order is kept so
        :meth:`select_active_pack` falls back to "first added" when
        nothing else matches."""
        if manifest.id not in self._packs:
            self._order.append(manifest.id)
        self._packs[manifest.id] = manifest

    def unregister(self, pack_id: str) -> None:
        self._packs.pop(pack_id, None)
        if pack_id in self._order:
            self._order.remove(pack_id)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def all(self) -> list[CharacterPackManifest]:
        return [self._packs[p] for p in self._order]

    def ids(self) -> list[str]:
        return list(self._order)

    def get(self, pack_id: str) -> CharacterPackManifest | None:
        return self._packs.get(pack_id)

    def __len__(self) -> int:
        return len(self._packs)

    def __contains__(self, pack_id: object) -> bool:
        return pack_id in self._packs

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------

    def select_active_pack(
        self,
        preferred_id: str | None = None,
        *,
        fallback_order: Sequence[str] = (BUILTIN_PACK_ID, LEGACY_PIXEL_PACK_ID),
    ) -> CharacterPackManifest | None:
        """Pick the active pack by priority.

        Resolution order:

        1. ``preferred_id`` if it resolves.
        2. Each id in ``fallback_order`` in turn (defaults to the
           built-in pixel pack).
        3. The first registered pack (insertion order).
        4. ``None`` when the registry is empty.
        """
        if preferred_id:
            match = self._packs.get(preferred_id)
            if match is not None:
                return match
            _LOGGER.info(
                "character_pack.preferred_missing",
                preferred_id=preferred_id,
            )
        for candidate in fallback_order:
            match = self._packs.get(candidate)
            if match is not None:
                return match
        if self._order:
            return self._packs[self._order[0]]
        return None


# ---------------------------------------------------------------------------
# Convenience wiring
# ---------------------------------------------------------------------------


def default_packs_dir() -> Path:
    """Return the packs root, honouring :data:`DEFAULT_PACKS_DIR_ENV`.

    On macOS the default lives under ``~/.deskmate/packs`` — the same
    folder family as the other Deskmate state (``~/.deskmate/db/``,
    ``~/.deskmate/build-status.json``).
    """
    env = os.environ.get(DEFAULT_PACKS_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".deskmate" / "packs"


def build_default_registry(
    extra_roots: Sequence[str | os.PathLike[str]] = (),
) -> CharacterPackRegistry:
    """Return a registry populated from the default root plus any
    ``extra_roots`` (e.g. a bundled ``assets/packs`` directory the
    app ships as its last-resort fallback).

    Every root is scanned with :func:`discover_packs`; later roots
    take precedence on id collisions. All I/O is best-effort — a
    missing or permission-denied root is logged + skipped.
    """
    roots: list[Path] = [default_packs_dir(), *[Path(r) for r in extra_roots]]
    registry = CharacterPackRegistry()
    for root in roots:
        result = discover_packs(root)
        for pack in result.packs:
            registry.register(pack)
    return registry


def resolve_active_pack(
    registry: CharacterPackRegistry,
    *,
    preferred_id: str | None = None,
) -> CharacterPackManifest | None:
    """Resolve the active pack the app should use right now.

    Priority:

    1. ``preferred_id`` argument (tests / explicit config).
    2. ``DESKMATE_CHARACTER_PACK`` env var.
    3. :class:`CharacterPackRegistry.select_active_pack` defaults.
    """
    env = os.environ.get(ACTIVE_PACK_ENV, "").strip() or None
    return registry.select_active_pack(preferred_id or env)


__all__ = [
    "ACTIVE_PACK_ENV",
    "BUILTIN_PACK_ID",
    "build_default_registry",
    "CharacterPackError",
    "CharacterPackRegistry",
    "DEFAULT_PACKS_DIR_ENV",
    "default_packs_dir",
    "discover_packs",
    "load_manifest",
    "LEGACY_PIXEL_PACK_ID",
    "PackDiscoveryResult",
    "resolve_active_pack",
    "DEFAULT_REQUIRED_STATES",
]
