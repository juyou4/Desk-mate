"""CodingSessionTracker tests (V10 Phase 13-i)."""

from __future__ import annotations

import pytest

from deskmate_agent.context import PerceptionSnapshot
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.skills import CodingSessionTracker


def _snap(
    app: str | None,
    window_title: str | None = None,
    ts_ms: int | None = None,
) -> PerceptionSnapshot:
    kwargs: dict[str, object] = {
        "app_bundle_id": app,
        "window_title": window_title,
    }
    if ts_ms is not None:
        kwargs["ts_ms"] = ts_ms
    return PerceptionSnapshot(**kwargs)


@pytest.fixture()
def captured() -> list[CompanionIntent]:
    return []


@pytest.fixture()
def sink(captured: list[CompanionIntent]):
    async def _sink(intent: CompanionIntent) -> None:
        captured.append(intent)

    return _sink


@pytest.mark.asyncio
async def test_no_ide_never_emits(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.apple.Safari"))
    await tracker(_snap("com.apple.Finder"))
    await tracker(_snap(None))
    assert captured == []


@pytest.mark.asyncio
async def test_ide_foreground_emits_present_live_activity(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.apple.dt.Xcode"))
    assert len(captured) == 1
    intent = captured[0]
    assert intent.kind is IntentKind.PRESENT_ISLAND
    # Phase 13-ii wire contract: ``surface`` (matching Swift's
    # ``decodePresentIsland``), not ``kind``.
    assert intent.payload["surface"] == "live_activity"
    assert intent.payload["activity_id"] == "coding-Xcode"
    assert intent.payload["priority"] == "p2"
    # No window title was provided → no detail on the wire.
    assert "detail" not in intent.payload


@pytest.mark.asyncio
async def test_ide_foreground_with_window_title_includes_detail(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(
        _snap("com.microsoft.VSCode", window_title="foo.py — deskmate")
    )
    intent = captured[0]
    assert intent.kind is IntentKind.PRESENT_ISLAND
    assert intent.payload["surface"] == "live_activity"
    assert intent.payload["detail"] == "foo.py — deskmate"


@pytest.mark.asyncio
async def test_window_title_equal_to_ide_name_is_stripped(
    sink, captured
) -> None:
    """NSWorkspace's localizedName fallback gives us e.g. "Xcode" as
    the title. That's redundant with the pill's primary label, so
    the tracker drops it."""
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.apple.dt.Xcode", window_title="Xcode"))
    assert "detail" not in captured[0].payload


@pytest.mark.asyncio
async def test_window_title_change_emits_update_island(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.microsoft.VSCode", window_title="a.py"))
    await tracker(_snap("com.microsoft.VSCode", window_title="b.py"))
    assert [c.kind for c in captured] == [
        IntentKind.PRESENT_ISLAND,
        IntentKind.UPDATE_ISLAND,
    ]
    update = captured[1]
    assert update.payload == {
        "activity_id": "coding-VSCode",
        "detail": "b.py",
    }


@pytest.mark.asyncio
async def test_same_window_title_does_not_re_emit_update(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(sink)
    for _ in range(3):
        await tracker(
            _snap("com.microsoft.VSCode", window_title="a.py")
        )
    assert len(captured) == 1
    assert captured[0].kind is IntentKind.PRESENT_ISLAND


@pytest.mark.asyncio
async def test_window_title_vanishes_emits_update_without_detail(
    sink, captured
) -> None:
    """Losing AX permission mid-session should cleanly blank the
    detail label rather than leak the last-known title."""
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("dev.zed.Zed", window_title="main.rs"))
    await tracker(_snap("dev.zed.Zed", window_title=None))
    assert [c.kind for c in captured] == [
        IntentKind.PRESENT_ISLAND,
        IntentKind.UPDATE_ISLAND,
    ]
    # ``detail`` omitted entirely when unset.
    assert captured[1].payload == {"activity_id": "coding-Zed"}


@pytest.mark.asyncio
async def test_same_ide_repeats_do_not_re_emit(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    for _ in range(5):
        await tracker(_snap("com.microsoft.VSCode"))
    assert len(captured) == 1
    assert captured[0].payload["activity_id"] == "coding-VSCode"


@pytest.mark.asyncio
async def test_ide_leave_emits_dismiss(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("dev.zed.Zed"))
    await tracker(_snap("com.apple.Safari"))
    assert [c.kind for c in captured] == [
        IntentKind.PRESENT_ISLAND,
        IntentKind.DISMISS_ISLAND,
    ]
    assert captured[1].payload == {"id": "coding-Zed"}


@pytest.mark.asyncio
async def test_ide_switch_dismisses_old_then_presents_new(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.apple.dt.Xcode"))
    await tracker(_snap("com.microsoft.VSCode"))
    kinds = [c.kind for c in captured]
    assert kinds == [
        IntentKind.PRESENT_ISLAND,
        IntentKind.DISMISS_ISLAND,
        IntentKind.PRESENT_ISLAND,
    ]
    assert captured[0].payload["activity_id"] == "coding-Xcode"
    assert captured[1].payload["id"] == "coding-Xcode"
    assert captured[2].payload["activity_id"] == "coding-VSCode"


@pytest.mark.asyncio
async def test_unknown_bundle_id_is_treated_as_non_ide(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("io.example.random-editor"))
    assert captured == []


@pytest.mark.asyncio
async def test_custom_apps_map_overrides_defaults(sink, captured) -> None:
    tracker = CodingSessionTracker(
        sink, apps={"io.example.myeditor": "MyEditor"}
    )
    # Known default id is no longer in the custom map.
    await tracker(_snap("com.apple.dt.Xcode"))
    assert captured == []
    # Custom id fires.
    await tracker(_snap("io.example.myeditor"))
    assert captured[0].payload["activity_id"] == "coding-MyEditor"


@pytest.mark.asyncio
async def test_dismiss_without_prior_present_is_a_noop(sink, captured) -> None:
    """Leaving a non-IDE app while no activity is showing must not
    emit a phantom DISMISS_ISLAND."""
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.apple.Safari"))
    await tracker(_snap("com.apple.Finder"))
    assert captured == []


# ---------------------------------------------------------------------------
# Phase 13-iv: session duration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duration_appears_after_one_minute(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("dev.zed.Zed", window_title="main.rs", ts_ms=0))
    await tracker(
        _snap("dev.zed.Zed", window_title="main.rs", ts_ms=60_000)
    )
    assert [c.kind for c in captured] == [
        IntentKind.PRESENT_ISLAND,
        IntentKind.UPDATE_ISLAND,
    ]
    # Detail gains a ``· 1m`` tail after a minute elapses.
    assert captured[1].payload["detail"] == "main.rs · 1m"


@pytest.mark.asyncio
async def test_duration_only_detail_when_no_window_title(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=0))
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=5_000))
    # Second tick: still no title, but duration is now 5s.
    update = captured[1]
    assert update.kind is IntentKind.UPDATE_ISLAND
    assert update.payload == {
        "activity_id": "coding-Xcode",
        "detail": "5s",
    }


@pytest.mark.asyncio
async def test_duration_formats_hours(sink, captured) -> None:
    tracker = CodingSessionTracker(sink)
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=0))
    # 1 hour 5 minutes elapsed.
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=65 * 60 * 1000))
    assert captured[1].payload["detail"] == "1h 5m"


@pytest.mark.asyncio
async def test_show_duration_false_keeps_detail_title_only(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(sink, show_duration=False)
    await tracker(
        _snap("com.microsoft.VSCode", window_title="a.py", ts_ms=0)
    )
    await tracker(
        _snap("com.microsoft.VSCode", window_title="a.py", ts_ms=60_000)
    )
    # Same title + duration disabled → no UPDATE fired.
    assert [c.kind for c in captured] == [IntentKind.PRESENT_ISLAND]
    assert captured[0].payload["detail"] == "a.py"


# ---------------------------------------------------------------------------
# Phase 13-v: debounce
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dwell_defers_present_until_threshold(sink, captured) -> None:
    tracker = CodingSessionTracker(sink, dwell_ms=2_000)
    # Tick 1: see VSCode — start the dwell timer, don't emit yet.
    await tracker(_snap("com.microsoft.VSCode", ts_ms=0))
    assert captured == []
    # Tick 2 at +1s: still under the 2s threshold.
    await tracker(_snap("com.microsoft.VSCode", ts_ms=1_000))
    assert captured == []
    # Tick 3 at +2s: dwell satisfied → PRESENT fires.
    await tracker(_snap("com.microsoft.VSCode", ts_ms=2_000))
    assert len(captured) == 1
    assert captured[0].kind is IntentKind.PRESENT_ISLAND
    assert captured[0].payload["activity_id"] == "coding-VSCode"


@pytest.mark.asyncio
async def test_dwell_cancelled_by_ide_leaving_window(
    sink, captured
) -> None:
    """Cmd-tab in and out during the dwell must not fire a phantom
    PRESENT afterwards."""
    tracker = CodingSessionTracker(sink, dwell_ms=2_000)
    await tracker(_snap("com.microsoft.VSCode", ts_ms=0))
    await tracker(_snap("com.apple.Safari", ts_ms=500))
    await tracker(_snap("com.apple.Safari", ts_ms=5_000))
    assert captured == []


@pytest.mark.asyncio
async def test_grace_defers_dismiss_after_leaving_ide(
    sink, captured
) -> None:
    # ``show_duration=False`` isolates the grace-timer path from the
    # duration refresh emission that would otherwise fire an UPDATE
    # when the user cmd-tabs back.
    tracker = CodingSessionTracker(
        sink, grace_ms=2_000, show_duration=False
    )
    await tracker(_snap("dev.zed.Zed", ts_ms=0))
    # Brief cmd-tab to Messages (1s < 2s grace) then back.
    await tracker(_snap("com.apple.MobileSMS", ts_ms=1_000))
    assert len(captured) == 1  # only the initial PRESENT
    await tracker(_snap("dev.zed.Zed", ts_ms=1_500))
    # Still just the initial PRESENT; no DISMISS fired, same detail.
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Phase 13-vi: window title cleanup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("● main.py", "main.py"),
        ("• main.py", "main.py"),
        ("* main.py", "main.py"),
        ("main.py — Edited", "main.py"),
        ("main.py — edited", "main.py"),
        ("main.py - Edited", "main.py"),
        ("main.py (modified)", "main.py"),
        ("main.py (Modified)", "main.py"),
        ("main.py — Not Saved", "main.py"),
        ("● main.py — Edited", "main.py"),  # stacked
        ("  main.py  ", "main.py"),  # pure whitespace
        ("file.swift — MyProject — Edited", "file.swift — MyProject"),
    ],
)
def test_clean_title_strips_editor_dirt_markers(
    raw: str, expected: str
) -> None:
    # Access via the class — it's a @classmethod that doesn't touch
    # instance state, so no sink / fixture needed.
    from deskmate_agent.skills.coding_session import CodingSessionTracker

    assert CodingSessionTracker._clean_title(raw) == expected


@pytest.mark.asyncio
async def test_detail_strips_modified_marker_from_window_title(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(sink, show_duration=False)
    await tracker(
        _snap(
            "com.microsoft.VSCode",
            window_title="● main.py — deskmate",
        )
    )
    assert captured[0].payload["detail"] == "main.py — deskmate"


@pytest.mark.asyncio
async def test_detail_dedups_through_modified_marker_churn(
    sink, captured
) -> None:
    """Saving a file shouldn't thrash the pill: ``● foo.py`` and
    ``foo.py`` must clean to the same detail string, so no
    UPDATE_ISLAND is emitted."""
    tracker = CodingSessionTracker(sink, show_duration=False)
    await tracker(
        _snap("com.microsoft.VSCode", window_title="● foo.py", ts_ms=0)
    )
    await tracker(
        _snap("com.microsoft.VSCode", window_title="foo.py", ts_ms=50)
    )
    # Only the initial PRESENT — no UPDATE from the save toggle.
    assert [c.kind for c in captured] == [IntentKind.PRESENT_ISLAND]
    assert captured[0].payload["detail"] == "foo.py"


@pytest.mark.asyncio
async def test_grace_expires_and_emits_dismiss(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(
        sink, grace_ms=2_000, show_duration=False
    )
    await tracker(_snap("dev.zed.Zed", ts_ms=0))
    await tracker(_snap("com.apple.Safari", ts_ms=1_000))
    assert [c.kind for c in captured] == [IntentKind.PRESENT_ISLAND]
    # Stay away past the grace window.
    await tracker(_snap("com.apple.Safari", ts_ms=3_500))
    assert [c.kind for c in captured] == [
        IntentKind.PRESENT_ISLAND,
        IntentKind.DISMISS_ISLAND,
    ]


# ---------------------------------------------------------------------------
# Phase 15-i: session-end callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_end_callback_fires_on_dismiss(
    sink, captured
) -> None:
    ended: list[tuple[str, int, int]] = []

    async def on_end(ide: str, started: int, ended_ts: int) -> None:
        ended.append((ide, started, ended_ts))

    tracker = CodingSessionTracker(
        sink,
        show_duration=False,
        min_persisted_duration_ms=0,
        on_session_end=on_end,
    )
    await tracker(_snap("com.microsoft.VSCode", ts_ms=0))
    await tracker(
        _snap("com.microsoft.VSCode", window_title="a.py", ts_ms=90_000)
    )
    await tracker(_snap("com.apple.Safari", ts_ms=91_000))
    assert ended == [("VSCode", 0, 90_000)]


@pytest.mark.asyncio
async def test_session_end_callback_uses_last_in_ide_ts_not_dismiss_ts(
    sink, captured
) -> None:
    """End-time is the last moment the IDE was frontmost, not when
    the grace timer finally fires — otherwise we'd credit the grace
    window itself to the session."""
    ended: list[tuple[str, int, int]] = []

    async def on_end(ide: str, started: int, ended_ts: int) -> None:
        ended.append((ide, started, ended_ts))

    tracker = CodingSessionTracker(
        sink,
        grace_ms=2_000,
        show_duration=False,
        min_persisted_duration_ms=0,
        on_session_end=on_end,
    )
    await tracker(_snap("dev.zed.Zed", ts_ms=0))
    await tracker(_snap("dev.zed.Zed", ts_ms=60_000))  # still in Zed
    await tracker(_snap("com.apple.Safari", ts_ms=61_000))  # start grace
    await tracker(_snap("com.apple.Safari", ts_ms=64_000))  # grace expires → dismiss
    assert ended == [("Zed", 0, 60_000)]


@pytest.mark.asyncio
async def test_session_end_skipped_for_sub_threshold_duration(
    sink, captured
) -> None:
    ended: list[tuple[str, int, int]] = []

    async def on_end(ide: str, started: int, ended_ts: int) -> None:
        ended.append((ide, started, ended_ts))

    # Default threshold is 60 s.
    tracker = CodingSessionTracker(
        sink, show_duration=False, on_session_end=on_end
    )
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=0))
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=5_000))
    await tracker(_snap("com.apple.Safari", ts_ms=6_000))
    assert ended == []


# ---------------------------------------------------------------------------
# Phase 15-ii: project resolver / git branch in detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_appears_between_title_and_duration(
    sink, captured
) -> None:
    from pathlib import Path

    from deskmate_agent.projects import ResolvedProject

    def resolver(bundle, title):
        return ResolvedProject(
            name="deskmate", path=Path("/x"), branch="feat/island"
        )

    tracker = CodingSessionTracker(
        sink,
        project_resolver=resolver,
        show_duration=False,
    )
    await tracker(
        _snap(
            "com.microsoft.VSCode",
            window_title="main.ts — deskmate",
            ts_ms=0,
        )
    )
    # ``detail`` = "title · branch" (no duration because 0ms elapsed
    # and show_duration=False anyway).
    assert (
        captured[0].payload["detail"]
        == "main.ts — deskmate · feat/island"
    )


@pytest.mark.asyncio
async def test_branch_absent_when_resolver_returns_none(
    sink, captured
) -> None:
    tracker = CodingSessionTracker(
        sink,
        project_resolver=lambda _b, _t: None,
        show_duration=False,
    )
    await tracker(_snap("com.microsoft.VSCode", window_title="main.ts"))
    assert captured[0].payload["detail"] == "main.ts"


@pytest.mark.asyncio
async def test_branch_absent_when_resolver_raises(sink, captured) -> None:
    def boom(_b, _t):
        raise RuntimeError("no")

    tracker = CodingSessionTracker(
        sink, project_resolver=boom, show_duration=False
    )
    await tracker(_snap("com.microsoft.VSCode", window_title="main.ts"))
    assert captured[0].payload["detail"] == "main.ts"


@pytest.mark.asyncio
async def test_branch_change_triggers_update_island(sink, captured) -> None:
    from pathlib import Path

    from deskmate_agent.projects import ResolvedProject

    branches = iter(["main", "feat/island"])

    def resolver(bundle, title):
        return ResolvedProject(
            name="deskmate", path=Path("/x"), branch=next(branches)
        )

    tracker = CodingSessionTracker(
        sink, project_resolver=resolver, show_duration=False
    )
    await tracker(
        _snap(
            "com.microsoft.VSCode",
            window_title="main.ts — deskmate",
            ts_ms=0,
        )
    )
    await tracker(
        _snap(
            "com.microsoft.VSCode",
            window_title="main.ts — deskmate",
            ts_ms=100,
        )
    )
    kinds = [c.kind for c in captured]
    assert kinds == [IntentKind.PRESENT_ISLAND, IntentKind.UPDATE_ISLAND]
    assert (
        captured[1].payload["detail"]
        == "main.ts — deskmate · feat/island"
    )


@pytest.mark.asyncio
async def test_session_end_fires_once_per_ide_switch(
    sink, captured
) -> None:
    ended: list[tuple[str, int, int]] = []

    async def on_end(ide: str, started: int, ended_ts: int) -> None:
        ended.append((ide, started, ended_ts))

    tracker = CodingSessionTracker(
        sink,
        show_duration=False,
        min_persisted_duration_ms=0,
        on_session_end=on_end,
    )
    # Xcode → VSCode → Safari, two PRESENTs should produce two
    # session-end notifications in order.
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=0))
    await tracker(_snap("com.apple.dt.Xcode", ts_ms=5_000))
    await tracker(_snap("com.microsoft.VSCode", ts_ms=10_000))
    await tracker(_snap("com.microsoft.VSCode", ts_ms=15_000))
    await tracker(_snap("com.apple.Safari", ts_ms=16_000))
    assert ended == [
        ("Xcode", 0, 5_000),
        ("VSCode", 10_000, 15_000),
    ]
