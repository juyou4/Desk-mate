"""Island ``notification_card`` publisher (V10 L2-#1).

The intent type ``PRESENT_ISLAND`` already speaks five surfaces, but
*production code* (Phase 13-i / 14-i) only ever emitted ``live_activity``
intents — the ``notification_card`` surface had no real producer. This
module is the single seam everything new should go through when it
wants to surface a transient interruption on the island instead of
the pet:

- :class:`IslandNotificationPublisher.show_notification` builds a
  well-formed ``surface=notification_card`` intent and emits it.
- When :data:`suppress_frontmost_notifications` is on (default) and
  the user is already looking at the active session's window, the
  publisher silently drops the intent and reports ``False``. This is
  the user-facing behaviour V10 plan §L2-#1 promises: "前台同会话时，
  岛不重复弹 notificationCard".

The frontmost ↔ session match relies on two opt-in fields in
:attr:`SessionInfo.extras`:

- ``frontmost_bundle_id`` — exact bundle-id match against
  ``perception.app_bundle_id``.
- ``frontmost_window_substring`` — case-insensitive substring match
  against ``perception.window_title``.

A session is considered "frontmost" when **all** the configured
fields match. A session with neither field never suppresses anything
— it has no claim on a window so the publisher can't reason about
whether the user is "already there".
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .context import PerceptionSnapshot
from .dispatcher import IntentSink
from .logging_setup import get_logger
from .protocol.intents import CompanionIntent, IntentKind
from .protocol.state import IslandSurfaceKind, Priority
from .sessions import SessionInfo

_LOGGER = get_logger(__name__)

ActiveSessionProvider = Callable[[], SessionInfo | None]
PerceptionProvider = Callable[[], PerceptionSnapshot | None]

# Configurable extras keys — exposed as constants so tests + adopters
# don't need to hardcode the magic strings (and a future rename only
# touches one place).
EXTRA_FRONTMOST_BUNDLE_ID = "frontmost_bundle_id"
EXTRA_FRONTMOST_WINDOW_SUBSTRING = "frontmost_window_substring"

# Island-polish-enhancements R3.6: surface_id wire-format guard.
# Length 1..128, ASCII letters/digits/`:_-` only. Compiled once at
# module load so the validation hot path is a single match call.
_SURFACE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


def _is_valid_surface_id(s: str) -> bool:
    """Return True iff ``s`` matches the R3.6 surface_id grammar.

    Length 1..128, ASCII letters/digits/`:`/`_`/`-` only. Anything
    else (empty, too long, unicode, whitespace, punctuation outside
    the allowed set) returns False.
    """
    if not isinstance(s, str):
        return False
    return _SURFACE_ID_RE.match(s) is not None


@dataclass(frozen=True)
class NotificationOutcome:
    """Result of a publish attempt.

    Returned by :meth:`IslandNotificationPublisher.show_notification`
    so callers can log / branch without re-querying the publisher
    state.
    """

    emitted: bool
    suppressed_reason: str | None = None


class IslandNotificationPublisher:
    """Emit ``surface=notification_card`` PRESENT_ISLAND intents
    with optional frontmost-suppression.

    All providers are dependency-injected so the publisher is fully
    unit-testable without a running :class:`Dispatcher` /
    :class:`SessionStore`.
    """

    def __init__(
        self,
        intent_sink: IntentSink,
        *,
        active_session_provider: ActiveSessionProvider,
        perception_provider: PerceptionProvider,
        suppress_frontmost_notifications: bool = True,
        silenced_session_ids: Iterable[str] = (),
    ) -> None:
        self._sink = intent_sink
        self._active_session = active_session_provider
        self._perception = perception_provider
        self._suppress = suppress_frontmost_notifications
        # V10 L2-#3 “不重弹旧通知” 保险丝. Sessions hydrated from
        # disk at startup go straight in here so any caller that
        # would normally fire a notification for them (reminder due,
        # build status flip, future skill / orchestrator paths) is
        # silently dropped instead of waking the user up about
        # something that happened yesterday. The set is mutable on
        # purpose: real activity on a silenced session (a fresh
        # ``upsert`` from the orchestrator, a ``user.message``
        # routed to it) calls :meth:`unsilence` to restore normal
        # notification behaviour.
        self._silenced: set[str] = {sid for sid in silenced_session_ids if sid}

    @property
    def suppress_frontmost_notifications(self) -> bool:
        return self._suppress

    def set_suppression(self, enabled: bool) -> None:
        """Toggle the suppression at runtime — useful for the
        DegradationController (future Phase) to turn off niceties
        during low-power mode without recreating the publisher."""
        self._suppress = bool(enabled)

    # ------------------------------------------------------------------
    # Silenced session ids (V10 L2-#3 restore-without-replay fuse)
    # ------------------------------------------------------------------

    def silence(self, session_id: str) -> None:
        """Mark a session as silenced. Subsequent
        :meth:`show_notification` calls scoped to that session id
        return ``emitted=False`` with reason ``restored_session_silenced``
        until :meth:`unsilence` clears it (or ``force=True`` is passed).
        """
        if session_id:
            self._silenced.add(session_id)

    def unsilence(self, session_id: str) -> bool:
        """Clear the silenced flag for ``session_id``.

        Returns ``True`` when the id was previously silenced and is
        now removed, ``False`` otherwise. Callers (typically the
        ``SessionStore`` upsert subscriber) can use the return value
        to log the transition once instead of every event.
        """
        if session_id and session_id in self._silenced:
            self._silenced.discard(session_id)
            return True
        return False

    def is_silenced(self, session_id: str) -> bool:
        return session_id in self._silenced

    @property
    def silenced_session_ids(self) -> frozenset[str]:
        """Read-only view of the currently silenced ids — useful for
        diagnostics and tests; intentionally a ``frozenset`` so
        callers can't mutate the set behind our back."""
        return frozenset(self._silenced)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def show_notification(
        self,
        *,
        activity_id: str,
        session_id: str | None = None,
        priority: Priority = Priority.P2,
        detail: str | None = None,
        force: bool = False,
        surface_id: str | None = None,
    ) -> NotificationOutcome:
        """Emit a ``notification_card`` PRESENT_ISLAND intent.

        Parameters mirror the Swift-side decoder. ``force=True`` skips
        the suppression check — used for genuinely-urgent surfaces
        (P0 approvals, security prompts) where user attention is
        required regardless of where they're looking.

        ``surface_id`` (R3) is the stable identifier used by the
        close-loop dismiss path; callers pass values like
        ``approval:<id>`` or ``question:<sid>:<seq>``. When the
        caller provides one, it is validated against the R3.6
        grammar and a malformed value rejects the emission entirely
        (R3.7). When ``surface_id`` is None, ``activity_id`` is used
        as a backwards-compatible fallback so existing call sites
        (``hook-{session_id}-{phase}`` and friends) keep working;
        if even the fallback fails validation we emit without a
        ``surface_id`` rather than dropping the notification.
        """
        if not activity_id:
            raise ValueError("activity_id must be non-empty")

        # R3.7 — explicit surface_id must conform to the grammar.
        if surface_id is not None and not _is_valid_surface_id(surface_id):
            _LOGGER.warning(
                "island_notification.invalid_surface_id",
                activity_id=activity_id,
                session_id=session_id,
                surface_id=surface_id,
            )
            return NotificationOutcome(
                emitted=False, suppressed_reason="invalid_surface_id"
            )

        if not force and session_id is not None and session_id in self._silenced:
            # Restored-but-silent path. Logged at info because a
            # production debugger pulling logs after a restart
            # really wants to see *which* sessions got muted.
            _LOGGER.info(
                "island_notification.silenced_restored_session",
                activity_id=activity_id,
                session_id=session_id,
                priority=priority.value,
            )
            return NotificationOutcome(
                emitted=False, suppressed_reason="restored_session_silenced"
            )

        if not force and self._should_suppress(session_id=session_id):
            _LOGGER.info(
                "island_notification.suppressed",
                activity_id=activity_id,
                session_id=session_id,
                priority=priority.value,
            )
            return NotificationOutcome(
                emitted=False, suppressed_reason="frontmost_matches_session"
            )

        payload: dict[str, object] = {
            "surface": IslandSurfaceKind.NOTIFICATION_CARD.value,
            "activity_id": activity_id,
            "priority": priority.value,
        }
        if session_id is not None:
            payload["session_id"] = session_id
        if detail is not None:
            payload["detail"] = detail

        # R3.1/R3.2 — inject the surface identifier. Caller-provided
        # values have already been validated above; the activity_id
        # fallback is validated here and dropped on failure (graceful
        # degradation: emit without surface_id rather than swallow
        # the whole notification, since the upstream caller didn't
        # opt into the new contract).
        if surface_id is not None:
            payload["surface_id"] = surface_id
        elif _is_valid_surface_id(activity_id):
            payload["surface_id"] = activity_id
        else:
            _LOGGER.warning(
                "island_notification.activity_id_unfit_for_surface_id",
                activity_id=activity_id,
                session_id=session_id,
            )

        await self._sink(
            CompanionIntent(
                kind=IntentKind.PRESENT_ISLAND,
                payload=payload,
            )
        )
        return NotificationOutcome(emitted=True)

    # ------------------------------------------------------------------
    # Suppression rule
    # ------------------------------------------------------------------

    def _should_suppress(self, *, session_id: str | None) -> bool:
        if not self._suppress:
            return False
        # When the caller scopes the notification to a specific
        # session, that session has to be the one frontmost; anything
        # else has no business suppressing.
        active = self._active_session()
        if active is None:
            return False
        if session_id is not None and session_id != active.session_id:
            return False
        perception = self._perception()
        if perception is None:
            return False
        return self._matches_frontmost(active, perception)

    @staticmethod
    def _matches_frontmost(
        session: SessionInfo, perception: PerceptionSnapshot
    ) -> bool:
        """Return True iff ``session.extras`` claims this frontmost.

        Both knobs are *optional*; a session with neither never
        suppresses (it has no window claim). When both are set, both
        must match.
        """
        bundle_claim = session.extras.get(EXTRA_FRONTMOST_BUNDLE_ID)
        window_claim = session.extras.get(EXTRA_FRONTMOST_WINDOW_SUBSTRING)
        if not bundle_claim and not window_claim:
            return False

        if bundle_claim:
            if not isinstance(bundle_claim, str):
                return False
            if perception.app_bundle_id != bundle_claim:
                return False

        if window_claim:
            if not isinstance(window_claim, str):
                return False
            actual = (perception.window_title or "")
            if window_claim.lower() not in actual.lower():
                return False

        return True


__all__ = [
    "ActiveSessionProvider",
    "EXTRA_FRONTMOST_BUNDLE_ID",
    "EXTRA_FRONTMOST_WINDOW_SUBSTRING",
    "IslandNotificationPublisher",
    "NotificationOutcome",
    "PerceptionProvider",
]
