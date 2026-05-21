"""IntentLogger + tail-status tests (V10 Phase 14-iv)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate_agent.intent_logger import IntentLogger
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind


def _intent(kind: IntentKind = IntentKind.SET_AVATAR_MOOD, **payload):
    return CompanionIntent(kind=kind, payload=payload or {"mood": "idle"})


@pytest.mark.asyncio
async def test_logger_writes_one_json_line_per_intent(tmp_path: Path) -> None:
    captured: list[CompanionIntent] = []

    async def inner(intent: CompanionIntent) -> None:
        captured.append(intent)

    ts_iter = iter([1_000, 2_000])
    logger = IntentLogger(
        path=tmp_path / "log.jsonl",
        inner=inner,
        clock_ms=lambda: next(ts_iter),
    )

    await logger(_intent(mood="happy"))
    await logger(_intent(IntentKind.SHOW_PET_BUBBLE, bubble={"text": "hi"}))

    assert len(captured) == 2
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "ts_ms": 1_000,
        "kind": "set_avatar_mood",
        "payload": {"mood": "happy"},
    }
    second = json.loads(lines[1])
    assert second["ts_ms"] == 2_000
    assert second["kind"] == "show_pet_bubble"


@pytest.mark.asyncio
async def test_inner_sink_failure_is_logged_then_re_raised(
    tmp_path: Path,
) -> None:
    async def inner(intent: CompanionIntent) -> None:
        raise RuntimeError("bridge down")

    logger = IntentLogger(
        path=tmp_path / "log.jsonl",
        inner=inner,
        clock_ms=lambda: 42,
    )
    with pytest.raises(RuntimeError):
        await logger(_intent())

    # The log captured the attempt even though the bridge raised.
    row = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[0])
    assert row["inner_error"] == {
        "type": "RuntimeError",
        "message": "bridge down",
    }


@pytest.mark.asyncio
async def test_logger_rotates_over_max_bytes(tmp_path: Path) -> None:
    """Rotation is the standard two-file scheme — the primary is
    renamed to ``*.1`` and a fresh primary starts. A third write
    therefore overwrites the first backup with the second file, so
    only the two most recent rotation windows survive. That's the
    same trade-off ``logrotate`` makes with ``rotate 1``."""

    async def inner(intent: CompanionIntent) -> None:
        return

    # Tiny threshold so two writes already trip the rotate.
    logger = IntentLogger(
        path=tmp_path / "log.jsonl",
        inner=inner,
        max_bytes=80,
        clock_ms=lambda: 1,
    )
    for i in range(3):
        await logger(_intent(payload_index=i, pad="x" * 40))

    # Backup exists with the second-to-latest line; primary has the
    # most recent line.
    backup = tmp_path / "log.jsonl.1"
    assert backup.exists()
    assert (tmp_path / "log.jsonl").exists()

    backup_lines = backup.read_text().splitlines()
    primary_lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(backup_lines) == 1
    assert len(primary_lines) == 1
    # The most recent payload lives in the primary.
    assert '"payload_index": 2' in primary_lines[0]
    # The immediately-previous payload lives in the rotated backup.
    assert '"payload_index": 1' in backup_lines[0]


# ---------------------------------------------------------------------------
# CLI tail-status behaviour
# ---------------------------------------------------------------------------


def test_cli_tail_status_once_prints_seed_lines(
    tmp_path, monkeypatch, capsys
) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(
        '{"ts_ms": 1, "kind": "a", "payload": {}}\n'
        '{"ts_ms": 2, "kind": "b", "payload": {}}\n'
        '{"ts_ms": 3, "kind": "c", "payload": {}}\n'
    )
    monkeypatch.setenv("DESKMATE_INTENT_LOG_PATH", str(log))
    from deskmate_agent.cli import main

    assert main(["tail-status", "-n", "2", "--once"]) == 0
    out = capsys.readouterr().out.splitlines()
    # --once seeds with the last 2 lines, then exits.
    assert out == [
        '{"ts_ms": 2, "kind": "b", "payload": {}}',
        '{"ts_ms": 3, "kind": "c", "payload": {}}',
    ]


def test_cli_tail_status_once_missing_file_is_silent(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(
        "DESKMATE_INTENT_LOG_PATH", str(tmp_path / "nowhere.jsonl")
    )
    from deskmate_agent.cli import main

    assert main(["tail-status", "--once"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_tail_status_zero_lines_seed_does_not_print_existing(
    tmp_path, monkeypatch, capsys
) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text('{"kind": "x"}\n{"kind": "y"}\n')
    monkeypatch.setenv("DESKMATE_INTENT_LOG_PATH", str(log))
    from deskmate_agent.cli import main

    assert main(["tail-status", "-n", "0", "--once"]) == 0
    assert capsys.readouterr().out == ""
