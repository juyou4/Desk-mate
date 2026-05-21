"""Coding-session island skill (V10 Phase 13-i / 13-ii).

Watches perception ticks for a frontmost IDE and drives
``PRESENT_ISLAND`` / ``UPDATE_ISLAND`` / ``DISMISS_ISLAND`` intents so
the notch overlay reflects the user's real coding activity instead of
just connection-level status.

Wire contract (matches Swift's ``decodePresentIsland``):

- ``surface``: ``IslandSurfaceKind`` raw string (``"live_activity"`` here)
- ``activity_id``: stable key (``"coding-<AppName>"``) used by Swift
  for UPDATE / DISMISS matching
- ``priority``: :class:`Priority` raw string
- ``detail``: optional secondary label (window title when available)

Design notes:

- **Pure recognition by bundle id.** The tracker ships a default map
  of known macOS IDE bundle ids → display names. The map is
  user-extensible (pass ``apps=`` to the constructor) so side-loading
  a new editor is a one-line change.
- **Stateful transitions.** The tracker only emits on state *change*
  — foregrounding the same IDE with the same detail is a no-op. This
  keeps the wire quiet; the perception sampler already deduplicates,
  but the tracker double-checks because nothing else on the Python
  side de-dups per activity id.
- **UPDATE_ISLAND for detail churn.** When the IDE stays the same but
  the window title changes, the tracker emits ``UPDATE_ISLAND`` with
  a fresh ``detail`` — Swift's reducer patches the detail slot in
  place without re-animating the pill.
- **Dismiss on leave.** The moment a non-IDE app takes focus, the
  tracker dismisses. A debounce/idle-grace can land in a later phase
  if the flicker turns out to be annoying in practice.
- **No proactive chain interaction.** The tracker runs as a
  :data:`dispatcher.PerceptionObserver` — it's a pure side effect
  that sits alongside the reactive / proactive chains rather than
  inside either.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..context import PerceptionSnapshot
from ..dispatcher import IntentSink, PerceptionObserver
from ..projects import ResolvedProject
from ..protocol.intents import CompanionIntent, IntentKind

# Phase 15-ii: plugged by the app. Takes the bundle id + raw AX window
# title and returns a resolved project (with git branch) or ``None``.
ProjectResolver = Callable[[str | None, str | None], ResolvedProject | None]

# Phase 15-i: invoked whenever a coding session terminates
# (DISMISS_ISLAND is emitted). The tracker hands over what it
# knows so the caller can persist / aggregate without needing
# access to the tracker's internals.
SessionEndCallback = Callable[[str, int, int], Awaitable[None]]
"""``(ide_name, started_at_ms, ended_at_ms) -> None``."""

# Known macOS IDE / editor bundle ids. The display name is what ends
# up on the wire (as ``activity_id`` suffix) and in the notch pill.
_DEFAULT_IDE_APPS: dict[str, str] = {
    "com.apple.dt.Xcode": "Xcode",
    "com.microsoft.VSCode": "VSCode",
    "com.microsoft.VSCodeInsiders": "VSCode Insiders",
    "com.visualstudio.code.oss": "VSCode OSS",
    # Cursor ships under a ToDesktop wrapper id.
    "com.todesktop.230313mzl4w4u92": "Cursor",
    "com.exafunction.windsurf": "Windsurf",
    "dev.zed.Zed": "Zed",
    "dev.zed.Zed-Preview": "Zed Preview",
    # JetBrains family
    "com.jetbrains.intellij": "IntelliJ",
    "com.jetbrains.intellij.ce": "IntelliJ CE",
    "com.jetbrains.pycharm": "PyCharm",
    "com.jetbrains.pycharm.ce": "PyCharm CE",
    "com.jetbrains.goland": "GoLand",
    "com.jetbrains.rider": "Rider",
    "com.jetbrains.rubymine": "RubyMine",
    "com.jetbrains.webstorm": "WebStorm",
    "com.jetbrains.clion": "CLion",
    "com.jetbrains.AppCode": "AppCode",
    "com.jetbrains.datagrip": "DataGrip",
    "com.jetbrains.android-studio": "Android Studio",
    "com.google.android.studio": "Android Studio",
    "com.sublimetext.4": "Sublime Text",
    "com.sublimetext.3": "Sublime Text",
    "com.github.atom": "Atom",
    "com.panic.Nova": "Nova",
    "io.fleet.Fleet": "Fleet",
    # Terminal multiplexers often used as coding surfaces too — up to
    # the user, but sane defaults.
    "io.github.pkamb.kitty": "kitty",
    "com.github.wez.wezterm": "WezTerm",
    "net.kovidgoyal.kitty": "kitty",
}


class CodingSessionTracker:
    """Perception → island intent bridge for coding activity.

    Constructor parameters:

    - ``intent_sink``: where to emit :class:`CompanionIntent`.
    - ``apps``: bundle-id → display-name map. Defaults to
      :data:`_DEFAULT_IDE_APPS`.
    - ``dwell_ms`` (Phase 13-v): require the same IDE to be frontmost
      for this many milliseconds before emitting ``PRESENT_ISLAND``.
      Debounces rapid cmd-tab flicker. Default ``0`` (no debounce).
    - ``grace_ms`` (Phase 13-v): require the non-IDE app to be
      frontmost for this many milliseconds before emitting
      ``DISMISS_ISLAND``. Debounces brief excursions to Finder /
      Messages mid-coding. Default ``0`` (no grace).
    - ``show_duration`` (Phase 13-iv): when ``True``, append a
      human-formatted session duration (``"23m"``, ``"1h 5m"``) to
      the island detail alongside the window title. Default ``True``;
      the formatter returns nothing for durations under 1 second so
      the initial PRESENT still shows just the title.
    """

    def __init__(
        self,
        intent_sink: IntentSink,
        apps: dict[str, str] | None = None,
        *,
        dwell_ms: int = 0,
        grace_ms: int = 0,
        show_duration: bool = True,
        min_persisted_duration_ms: int = 60_000,
        on_session_end: SessionEndCallback | None = None,
        project_resolver: ProjectResolver | None = None,
    ) -> None:
        self._sink = intent_sink
        self._apps = dict(apps) if apps is not None else dict(_DEFAULT_IDE_APPS)
        self._dwell_ms = max(0, int(dwell_ms))
        self._grace_ms = max(0, int(grace_ms))
        self._show_duration = show_duration
        # Phase 15-i: don't record micro-sessions — they're usually
        # cmd-tab noise that happened to slip past the dwell filter.
        # Default 60 s matches the duration formatter's first visible
        # bucket.
        self._min_persisted_duration_ms = max(0, int(min_persisted_duration_ms))
        self._on_session_end = on_session_end
        # Phase 15-ii: optional hook that maps a foreground IDE to a
        # :class:`ResolvedProject` carrying the current git branch.
        # Kept sync because the default implementation is a cheap
        # file read (``.git/HEAD``).
        self._project_resolver = project_resolver
        # Name of the currently-shown coding activity (display name)
        # or ``None`` when the island is clear.
        self._current_name: str | None = None
        self._current_activity_id: str | None = None
        # Last detail string shipped with PRESENT/UPDATE for the
        # current activity. Kept so we skip redundant UPDATE_ISLANDs.
        self._current_detail: str | None = None
        # Perception ts at which the current session was PRESENTed.
        # Used by the duration formatter and the session-end
        # callback.
        self._session_start_ms: int | None = None
        # Last perception ts — when DISMISS fires we use this as the
        # session end, since a grace-debounced dismiss happens some
        # time AFTER the last in-IDE tick.
        self._last_in_ide_ms: int | None = None
        # Debounce bookkeeping.
        self._pending_name: str | None = None
        self._pending_since_ms: int | None = None
        self._absent_since_ms: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def __call__(self, perception: PerceptionSnapshot) -> None:
        """Handle one perception snapshot.

        Intended to be registered as a
        :data:`dispatcher.PerceptionObserver`.
        """
        now_ms = perception.ts_ms
        bundle_id = perception.app_bundle_id
        name = self._apps.get(bundle_id) if bundle_id else None
        window_title = self._detail_from(perception, ide_name=name)

        if name is None:
            # Foreground is not a known IDE. Clear any pending
            # "IDE becoming foreground" state; maybe start / check
            # the grace timer.
            self._pending_name = None
            self._pending_since_ms = None
            if self._current_activity_id is not None:
                if self._absent_since_ms is None:
                    self._absent_since_ms = now_ms
                if now_ms - self._absent_since_ms >= self._grace_ms:
                    await self._dismiss()
                    self._absent_since_ms = None
            return

        # Frontmost IS a known IDE. Any grace timer is void.
        self._absent_since_ms = None

        # Phase 15-ii: ask the project resolver once per tick so the
        # branch slot tracks the user's actual repo (respects
        # switching branches mid-session).
        branch = self._resolve_branch(
            bundle_id=bundle_id, window_title_raw=perception.window_title
        )

        if name != self._current_name:
            # IDE changed or first-time foregrounded. Check the
            # dwell threshold before emitting. Importantly we do NOT
            # refresh ``_last_in_ide_ms`` here: the field belongs to
            # whatever IDE is currently showing, and we need it
            # intact for the dismiss-of-old-session callback that
            # ``_present`` triggers below.
            if self._pending_name != name:
                self._pending_name = name
                self._pending_since_ms = now_ms
                if self._dwell_ms > 0:
                    return
            if (
                self._pending_since_ms is not None
                and now_ms - self._pending_since_ms < self._dwell_ms
            ):
                return
            self._pending_name = None
            self._pending_since_ms = None
            detail = self._build_detail(
                ide_name=name,
                window_title=window_title,
                branch=branch,
                now_ms=now_ms,
                session_start_ms=now_ms,
            )
            await self._present(name, detail=detail, started_at_ms=now_ms)
            # Post-PRESENT: anchor the new session's "last in IDE" ts.
            self._last_in_ide_ms = now_ms
            return

        # Same IDE continues — this tick counts as "in IDE" for the
        # dismiss end-time accounting.
        self._last_in_ide_ms = now_ms
        self._pending_name = None
        self._pending_since_ms = None
        session_start = (
            self._session_start_ms
            if self._session_start_ms is not None
            else now_ms
        )
        detail = self._build_detail(
            ide_name=name,
            window_title=window_title,
            branch=branch,
            now_ms=now_ms,
            session_start_ms=session_start,
        )
        if detail != self._current_detail:
            await self._update_detail(detail)

    def as_observer(self) -> PerceptionObserver:
        """Return ``self`` typed as a :data:`PerceptionObserver`.

        Purely a readability helper — ``__call__`` already satisfies
        the protocol, but explicit beats implicit for the wiring in
        ``app.py``.
        """
        return self

    # ------------------------------------------------------------------
    # Derivation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _activity_id_for(name: str) -> str:
        # Swift's :class:`IslandOverlay` recognizes this prefix and
        # strips it for display; keep the shape stable.
        return f"coding-{name}"

    # Leading markers editors use to flag an unsaved buffer. We strip
    # them because "did I save?" is an editor-level concern, not
    # something the notch pill needs to report.
    _UNSAVED_PREFIXES = ("● ", "• ", "* ")
    # Trailing markers (Xcode's "— Edited", Sublime's "(modified)", …).
    # Kept as a fixed list of known suffixes rather than a regex so
    # the rules are easy to audit and extend.
    _MODIFIED_SUFFIXES = (
        " — Edited",
        " — edited",
        " - Edited",
        " - edited",
        " (modified)",
        " (Modified)",
        " — Not Saved",
    )

    @classmethod
    def _clean_title(cls, raw: str) -> str:
        """Strip editor 'unsaved / modified' markers from a title.

        Phase 13-vi: AX-read window titles routinely include noise
        like ``"● main.py — project"`` (VSCode dot for dirty buffer)
        or ``"file.swift — MyProject — Edited"`` (Xcode). We keep
        the project suffix because that's actual context, but we
        drop the dirt markers so the pill doesn't yo-yo between
        ``"main.py"`` and ``"● main.py"`` on every keystroke.
        """
        title = raw.strip()
        # Iterate in case an editor stacks markers (it happens).
        changed = True
        while changed:
            changed = False
            for prefix in cls._UNSAVED_PREFIXES:
                if title.startswith(prefix):
                    title = title[len(prefix):].strip()
                    changed = True
                    break
            for suffix in cls._MODIFIED_SUFFIXES:
                if title.endswith(suffix):
                    title = title[: -len(suffix)].strip()
                    changed = True
                    break
        return title

    def _resolve_branch(
        self,
        *,
        bundle_id: str | None,
        window_title_raw: str | None,
    ) -> str | None:
        """Phase 15-ii: hand the raw AX title (not the cleaned one)
        to the project resolver so substring matching on the project
        name still works when the title contains editor markers
        (``● main.py — my-project``)."""
        if self._project_resolver is None:
            return None
        try:
            resolved = self._project_resolver(bundle_id, window_title_raw)
        except Exception:  # noqa: BLE001 — fail-soft
            return None
        if resolved is None:
            return None
        return resolved.branch or None

    @classmethod
    def _detail_from(
        cls,
        perception: PerceptionSnapshot,
        *,
        ide_name: str | None,
    ) -> str | None:
        """Extract the window-title part of the island detail.

        Prefers the window title when it is meaningful (Swift sends
        the frontmost AX window title in :attr:`window_title` when
        accessibility is granted, otherwise it falls back to the
        app's localized name — which is already redundant with the
        pill's primary label). We strip the latter and clean off
        any editor modification markers (Phase 13-vi).
        """
        if ide_name is None:
            return None
        title = cls._clean_title(perception.window_title or "")
        if not title:
            return None
        # Hide the "Xcode" / "VSCode" fallback since the pill already
        # shows the IDE name up-front.
        if title == ide_name:
            return None
        return title

    def _build_detail(
        self,
        *,
        ide_name: str,
        window_title: str | None,
        branch: str | None,
        now_ms: int,
        session_start_ms: int,
    ) -> str | None:
        """Compose the final detail string from (title, branch, duration).

        Returns ``None`` when no piece has anything to show so the
        wire payload stays clean. Branch slots between title and
        duration so reading order stays ``what · where · how long``.
        """
        parts: list[str] = []
        if window_title:
            parts.append(window_title)
        if branch:
            parts.append(branch)
        if self._show_duration:
            duration = self._format_duration_ms(
                max(0, now_ms - session_start_ms)
            )
            if duration is not None:
                parts.append(duration)
        return " · ".join(parts) if parts else None

    @staticmethod
    def _format_duration_ms(ms: int) -> str | None:
        """Human-friendly duration formatter:

        - ``< 1s`` → ``None`` (nothing worth showing yet)
        - ``< 1m`` → ``"<N>s"``
        - ``< 1h`` → ``"<N>m"``
        - otherwise → ``"<H>h <M>m"`` (``M`` omitted when zero)
        """
        seconds = ms // 1000
        if seconds < 1:
            return None
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        rem = minutes % 60
        return f"{hours}h {rem}m" if rem else f"{hours}h"

    # ------------------------------------------------------------------
    # Intent emitters
    # ------------------------------------------------------------------

    async def _present(
        self,
        name: str,
        *,
        detail: str | None,
        started_at_ms: int,
    ) -> None:
        # Dismiss the previous activity first so Swift's island
        # state machine produces a clean ``.liveActivity`` transition
        # rather than stacking.
        if self._current_activity_id is not None:
            await self._dismiss()
        activity_id = self._activity_id_for(name)
        self._current_name = name
        self._current_activity_id = activity_id
        self._current_detail = detail
        self._session_start_ms = started_at_ms
        payload: dict[str, object] = {
            "surface": "live_activity",
            "activity_id": activity_id,
            "priority": "p2",
        }
        if detail is not None:
            payload["detail"] = detail
        await self._sink(
            CompanionIntent(
                kind=IntentKind.PRESENT_ISLAND,
                payload=payload,
            )
        )

    async def _update_detail(self, detail: str | None) -> None:
        # Only called when ``_current_activity_id`` is set.
        assert self._current_activity_id is not None
        self._current_detail = detail
        payload: dict[str, object] = {
            "activity_id": self._current_activity_id,
        }
        if detail is not None:
            payload["detail"] = detail
        await self._sink(
            CompanionIntent(
                kind=IntentKind.UPDATE_ISLAND,
                payload=payload,
            )
        )

    async def _dismiss(self) -> None:
        activity_id = self._current_activity_id
        ended_name = self._current_name
        started_ms = self._session_start_ms
        # Prefer the last in-IDE ts so a grace-debounced dismiss
        # doesn't credit non-IDE minutes to the session.
        ended_ms = self._last_in_ide_ms
        self._current_name = None
        self._current_activity_id = None
        self._current_detail = None
        self._session_start_ms = None
        self._last_in_ide_ms = None
        await self._sink(
            CompanionIntent(
                kind=IntentKind.DISMISS_ISLAND,
                payload={"id": activity_id} if activity_id else {},
            )
        )
        # Phase 15-i: after the intent lands, notify any interested
        # observer. We guard against both (a) impossible timestamps
        # (shouldn't happen, but the callback contract demands real
        # ints) and (b) micro-sessions below the minimum threshold.
        if (
            self._on_session_end is not None
            and ended_name is not None
            and started_ms is not None
            and ended_ms is not None
            and ended_ms >= started_ms
            and (ended_ms - started_ms) >= self._min_persisted_duration_ms
        ):
            await self._on_session_end(ended_name, started_ms, ended_ms)


__all__ = ["CodingSessionTracker"]
