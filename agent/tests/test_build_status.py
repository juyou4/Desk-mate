"""BuildStatusSkill tests (V10 Phase 14-i)."""

from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.skills import BuildStatusSkill, BuildStatusWatcher


@pytest.fixture()
def captured() -> list[CompanionIntent]:
    return []


@pytest.fixture()
def sink(captured: list[CompanionIntent]):
    async def _sink(intent: CompanionIntent) -> None:
        captured.append(intent)

    return _sink


# ---------------------------------------------------------------------------
# Skill semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_start_emits_live_activity_at_p1(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("cargo test")
    intent = captured[0]
    assert intent.kind is IntentKind.PRESENT_ISLAND
    assert intent.payload == {
        "surface": "live_activity",
        "activity_id": "build-cargo test",
        "priority": "p1",
        "detail": "🔨 cargo test",
    }


@pytest.mark.asyncio
async def test_build_start_with_branch_shows_branch_in_detail(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("cargo test", branch="feat/island")
    assert captured[0].payload["detail"] == "🔨 cargo test · feat/island"


@pytest.mark.asyncio
async def test_build_done_with_branch_and_message_combines_all(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("pytest", branch="main")
    await skill.on_build_done(
        "pytest", success=True, message="42 passed", branch="main"
    )
    assert captured[1].payload["detail"] == "✅ pytest · main · 42 passed"


@pytest.mark.asyncio
async def test_build_progress_with_branch_interleaves_correctly(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("make", branch="main")
    await skill.on_build_progress(
        "make", 0.5, message="linking", branch="main"
    )
    # Order: emoji task · branch · pct · msg
    assert captured[1].payload["detail"] == "🔨 make · main · 50% · linking"


@pytest.mark.asyncio
async def test_progress_formats_percent_and_optional_message(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("pytest")
    await skill.on_build_progress("pytest", 0.42, message="42 of 100")
    update = captured[1]
    assert update.kind is IntentKind.UPDATE_ISLAND
    assert update.payload["detail"] == "🔨 pytest · 42% · 42 of 100"


@pytest.mark.asyncio
async def test_progress_clamps_out_of_range_values(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("make")
    await skill.on_build_progress("make", -0.5)
    await skill.on_build_progress("make", 2.0)
    # -0.5 → 0%, 2.0 → 100%
    assert captured[1].payload["detail"] == "🔨 make · 0%"
    assert captured[2].payload["detail"] == "🔨 make · 100%"


@pytest.mark.asyncio
async def test_progress_payload_includes_numeric_progress_field(
    sink, captured
) -> None:
    """R4.6: numeric ``progress`` is published alongside ``detail``."""
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("make")
    await skill.on_build_progress("make", 0.5)
    update = captured[1]
    assert update.kind is IntentKind.UPDATE_ISLAND
    assert update.payload["progress"] == 0.5
    assert update.payload["detail"] == "🔨 make · 50%"


@pytest.mark.asyncio
async def test_progress_payload_clamps_above_one_to_one(
    sink, captured
) -> None:
    """R4.6: values above 1.0 clamp to 1.0 in the payload."""
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("make")
    await skill.on_build_progress("make", 1.5)
    assert captured[1].payload["progress"] == 1.0


@pytest.mark.asyncio
async def test_progress_payload_clamps_below_zero_to_zero(
    sink, captured
) -> None:
    """R4.6: negative values clamp to 0.0 in the payload."""
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("make")
    await skill.on_build_progress("make", -0.3)
    assert captured[1].payload["progress"] == 0.0


@pytest.mark.asyncio
async def test_progress_payload_omits_field_when_nan(
    sink, captured
) -> None:
    """R4.7: NaN inputs cause the ``progress`` field to be omitted."""
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("make")
    await skill.on_build_progress("make", float("nan"))
    update = captured[1]
    assert update.kind is IntentKind.UPDATE_ISLAND
    assert "progress" not in update.payload
    assert "detail" in update.payload


@pytest.mark.asyncio
async def test_progress_detail_keeps_percent_for_backward_compat(
    sink, captured
) -> None:
    """R4.6: detail string keeps the percent suffix unchanged."""
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("pytest")
    await skill.on_build_progress("pytest", 0.42)
    update = captured[1]
    assert update.payload["detail"] == "🔨 pytest · 42%"
    assert update.payload["progress"] == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_progress_for_unknown_task_is_ignored(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_progress("ghost", 0.5)
    assert captured == []


@pytest.mark.asyncio
async def test_build_done_success_emits_check_then_auto_dismiss(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink, success_ttl_ms=10)
    await skill.on_build_start("cargo build")
    await skill.on_build_done("cargo build", success=True, message="clean")
    # PRESENT + UPDATE emitted immediately.
    assert captured[-1].kind is IntentKind.UPDATE_ISLAND
    assert captured[-1].payload["detail"] == "✅ cargo build · clean"
    # Wait past the TTL for the deferred DISMISS.
    await asyncio.sleep(0.05)
    assert captured[-1].kind is IntentKind.DISMISS_ISLAND
    assert captured[-1].payload == {"id": "build-cargo build"}


@pytest.mark.asyncio
async def test_build_failed_emits_x_and_long_ttl(sink, captured) -> None:
    skill = BuildStatusSkill(
        sink, success_ttl_ms=10, failure_ttl_ms=10_000
    )
    await skill.on_build_start("pytest")
    await skill.on_build_done("pytest", success=False, message="42 failed")
    assert captured[-1].payload["detail"] == "❌ pytest · 42 failed"
    # 10s TTL shouldn't trip on a 50 ms wait.
    await asyncio.sleep(0.05)
    assert captured[-1].kind is IntentKind.UPDATE_ISLAND


@pytest.mark.asyncio
async def test_second_start_replaces_first_with_dismiss_then_present(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("first")
    await skill.on_build_start("second")
    kinds = [c.kind for c in captured]
    assert kinds == [
        IntentKind.PRESENT_ISLAND,
        IntentKind.DISMISS_ISLAND,
        IntentKind.PRESENT_ISLAND,
    ]
    assert captured[1].payload == {"id": "build-first"}
    assert captured[2].payload["activity_id"] == "build-second"


@pytest.mark.asyncio
async def test_external_dismiss_clears_current_build(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_build_start("pytest")
    await skill.on_external_dismiss()
    assert captured[-1].kind is IntentKind.DISMISS_ISLAND
    assert captured[-1].payload == {"id": "build-pytest"}


@pytest.mark.asyncio
async def test_external_dismiss_is_noop_without_active_build(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    await skill.on_external_dismiss()
    assert captured == []


@pytest.mark.asyncio
async def test_second_start_cancels_pending_auto_dismiss_of_previous(
    sink, captured
) -> None:
    skill = BuildStatusSkill(sink, success_ttl_ms=50)
    await skill.on_build_start("first")
    await skill.on_build_done("first", success=True)
    # New build starts before the auto-dismiss for "first" fires.
    await skill.on_build_start("second")
    # The auto-dismiss task for "first" must not fire late — sleep
    # past its TTL and confirm no stray DISMISS was enqueued for the
    # *new* build's activity id.
    await asyncio.sleep(0.1)
    trailing_dismisses = [
        c for c in captured
        if c.kind is IntentKind.DISMISS_ISLAND
        and c.payload.get("id") == "build-second"
    ]
    assert trailing_dismisses == []


# ---------------------------------------------------------------------------
# Watcher I/O
# ---------------------------------------------------------------------------


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.mark.asyncio
async def test_watcher_routes_started_into_skill(
    tmp_path, sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    watcher = BuildStatusWatcher(
        skill, path=tmp_path / "status.json"
    )
    _write_status(tmp_path / "status.json", {"state": "started", "task": "npm"})
    await watcher._drain_once()
    assert captured[0].payload["activity_id"] == "build-npm"
    # File must be consumed after being dispatched.
    assert not (tmp_path / "status.json").exists()


@pytest.mark.asyncio
async def test_watcher_routes_progress_and_done(
    tmp_path, sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    watcher = BuildStatusWatcher(
        skill, path=tmp_path / "status.json"
    )
    await skill.on_build_start("npm")
    _write_status(
        tmp_path / "status.json",
        {"state": "progress", "task": "npm", "progress": 0.75},
    )
    await watcher._drain_once()
    assert captured[-1].payload["detail"] == "🔨 npm · 75%"

    _write_status(
        tmp_path / "status.json",
        {"state": "done", "task": "npm", "message": "ok"},
    )
    await watcher._drain_once()
    assert captured[-1].payload["detail"] == "✅ npm · ok"


@pytest.mark.asyncio
async def test_watcher_invalid_json_is_dropped_quietly(
    tmp_path, sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    watcher = BuildStatusWatcher(
        skill, path=tmp_path / "status.json"
    )
    (tmp_path / "status.json").write_text("not-json{{{")
    await watcher._drain_once()
    assert captured == []
    assert not (tmp_path / "status.json").exists()


@pytest.mark.asyncio
async def test_watcher_missing_task_logged_and_skipped(
    tmp_path, sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    watcher = BuildStatusWatcher(
        skill, path=tmp_path / "status.json"
    )
    _write_status(tmp_path / "status.json", {"state": "started"})
    await watcher._drain_once()
    assert captured == []


@pytest.mark.asyncio
async def test_watcher_dismiss_payload_dispatches_to_external_dismiss(
    tmp_path, sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    watcher = BuildStatusWatcher(
        skill, path=tmp_path / "status.json"
    )
    await skill.on_build_start("pytest")
    _write_status(tmp_path / "status.json", {"state": "dismiss"})
    await watcher._drain_once()
    assert captured[-1].kind is IntentKind.DISMISS_ISLAND


# ---------------------------------------------------------------------------
# CLI → wire shape
# ---------------------------------------------------------------------------


def test_cli_writes_expected_json_for_each_subcommand(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "status.json"
    monkeypatch.setenv("DESKMATE_BUILD_STATUS_PATH", str(target))
    from deskmate_agent.cli import main

    assert main(["build-start", "cargo test", "--no-branch"]) == 0
    assert json.loads(target.read_text()) == {
        "state": "started",
        "task": "cargo test",
    }

    assert (
        main(
            [
                "build-progress",
                "cargo test",
                "0.5",
                "--message",
                "hi",
                "--no-branch",
            ]
        )
        == 0
    )
    assert json.loads(target.read_text()) == {
        "state": "progress",
        "task": "cargo test",
        "progress": 0.5,
        "message": "hi",
    }

    assert main(["build-done", "cargo test", "--no-branch"]) == 0
    assert json.loads(target.read_text()) == {
        "state": "done",
        "task": "cargo test",
    }

    assert (
        main(
            ["build-failed", "cargo test", "--message", "bad", "--no-branch"]
        )
        == 0
    )
    assert json.loads(target.read_text()) == {
        "state": "failed",
        "task": "cargo test",
        "message": "bad",
    }

    assert main(["build-dismiss"]) == 0
    assert json.loads(target.read_text()) == {"state": "dismiss"}


def test_cli_explicit_branch_override_reaches_payload(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "status.json"
    monkeypatch.setenv("DESKMATE_BUILD_STATUS_PATH", str(target))
    from deskmate_agent.cli import main

    assert (
        main(["build-start", "cargo test", "--branch", "feat/island"]) == 0
    )
    assert json.loads(target.read_text()) == {
        "state": "started",
        "task": "cargo test",
        "branch": "feat/island",
    }


def test_cli_auto_detects_branch_from_cwd(
    tmp_path, monkeypatch
) -> None:
    # Build a fake repo and make it the CWD so the CLI picks up its
    # branch via the pure-Python lookup.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/auto-branch\n")
    monkeypatch.chdir(repo)

    target = tmp_path / "status.json"
    monkeypatch.setenv("DESKMATE_BUILD_STATUS_PATH", str(target))
    from deskmate_agent.cli import main

    assert main(["build-start", "pytest"]) == 0
    assert json.loads(target.read_text()) == {
        "state": "started",
        "task": "pytest",
        "branch": "auto-branch",
    }


@pytest.mark.asyncio
async def test_watcher_forwards_branch_to_skill(
    tmp_path, sink, captured
) -> None:
    skill = BuildStatusSkill(sink)
    watcher = BuildStatusWatcher(
        skill, path=tmp_path / "status.json"
    )
    _write_status(
        tmp_path / "status.json",
        {"state": "started", "task": "npm", "branch": "main"},
    )
    await watcher._drain_once()
    assert captured[0].payload["detail"] == "🔨 npm · main"


# ---------------------------------------------------------------------------
# Phase 14-iii: `deskmate today`
# ---------------------------------------------------------------------------


def _seed_today_db(
    db_dir: Path,
    *,
    rows: list[tuple[str, int, int]],
) -> None:
    """Create the sessions.db layout used by :class:`CodingSessionStore`
    and insert the given rows. Tests read from it via the CLI."""
    import sqlite3 as _sqlite3

    db_dir.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(db_dir / "sessions.db")
    try:
        conn.execute(
            "CREATE TABLE coding_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ide TEXT NOT NULL,"
            "started_at_ms INTEGER NOT NULL,"
            "ended_at_ms INTEGER NOT NULL,"
            "duration_ms INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO coding_sessions "
            "(ide, started_at_ms, ended_at_ms, duration_ms) "
            "VALUES (?, ?, ?, ?)",
            [(ide, s, e, e - s) for ide, s, e in rows],
        )
        conn.commit()
    finally:
        conn.close()


def test_cli_today_degrades_when_db_missing(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "missing"))
    from deskmate_agent.cli import main

    assert main(["today"]) == 0
    out = capsys.readouterr().out
    assert "nothing logged" in out.lower()


def test_cli_today_json_reports_total_and_breakdown(
    tmp_path, monkeypatch, capsys
) -> None:
    # Use a ts that's definitely past midnight in UTC so tz=0 math
    # behaves regardless of the test host's actual TZ.
    now_ms = int(time.time() * 1000)
    # Anchor at start of today in UTC so all seeded rows count.
    day_ms = 24 * 3600 * 1000
    today_start_utc = now_ms - (now_ms % day_ms)
    _seed_today_db(
        tmp_path / "db",
        rows=[
            ("Xcode", today_start_utc + 1_000, today_start_utc + 1_000 + 20 * 60_000),
            ("VSCode", today_start_utc + 30 * 60_000, today_start_utc + 30 * 60_000 + 45 * 60_000),
        ],
    )
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    # Pin TZ so the CLI's local-midnight math matches the UTC anchor.
    monkeypatch.setattr(
        "deskmate_agent.cli._local_tz_offset_s", lambda: 0
    )
    from deskmate_agent.cli import main

    assert main(["today", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_ms"] == (20 + 45) * 60_000
    assert payload["by_ide"] == {
        "VSCode": 45 * 60_000,
        "Xcode": 20 * 60_000,
    }


def test_cli_today_human_readable_emits_per_ide_lines(
    tmp_path, monkeypatch, capsys
) -> None:
    now_ms = int(time.time() * 1000)
    day_ms = 24 * 3600 * 1000
    today_start_utc = now_ms - (now_ms % day_ms)
    _seed_today_db(
        tmp_path / "db",
        rows=[
            ("Zed", today_start_utc + 100, today_start_utc + 100 + 65 * 60_000),
        ],
    )
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(
        "deskmate_agent.cli._local_tz_offset_s", lambda: 0
    )
    from deskmate_agent.cli import main

    assert main(["today"]) == 0
    out = capsys.readouterr().out
    assert "Today: 1h 5m" in out
    assert "Zed" in out
    assert "1h 5m" in out


@pytest.mark.asyncio
@settings(max_examples=50)
@given(
    raw_progress=st.one_of(
        st.floats(allow_nan=True, allow_infinity=True), st.none()
    )
)
async def test_progress_always_in_range_or_absent(raw_progress):
    """Property 4: emitted progress ∈ [0,1] or absent.

    **Validates: Requirements R4.6, R4.7**
    """
    emitted: list = []

    async def sink(intent):
        emitted.append(intent)

    skill = BuildStatusSkill(sink)
    await skill.on_build_start("t")
    if raw_progress is not None:
        await skill.on_build_progress("t", raw_progress)
    for intent in emitted:
        if "progress" in intent.payload:
            p = intent.payload["progress"]
            assert 0.0 <= p <= 1.0
            assert not math.isnan(p)
            assert not math.isinf(p)
