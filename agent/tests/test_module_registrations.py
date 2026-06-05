"""External island module registration queue tests."""

from __future__ import annotations

import json

import pytest

from deskmate_agent.cli import main
from deskmate_agent.module_registrations import (
    ModuleRegistrationWatcher,
    write_module_registration,
)
from deskmate_agent.protocol import IslandModuleSpec


@pytest.mark.asyncio
async def test_module_registration_watcher_consumes_spec_file(tmp_path) -> None:
    spec = IslandModuleSpec(
        id="kiro.spec",
        kind="live_activity",
        title="KIRO",
        activity_prefix="kiro-spec-",
        subtitle="{detail}",
        image="k.circle",
        priority=80,
    )
    path = write_module_registration(spec, queue_dir=tmp_path)
    seen: list[IslandModuleSpec] = []

    async def handle(item: IslandModuleSpec) -> None:
        seen.append(item)

    watcher = ModuleRegistrationWatcher(tmp_path, handle)
    count = await watcher.drain_once()

    assert count == 1
    assert not path.exists()
    assert seen == [spec]


def test_cli_island_module_register_writes_spec_file(tmp_path, capsys) -> None:
    code = main(
        [
            "island",
            "module",
            "register",
            "kiro.spec",
            "--kind",
            "live_activity",
            "--title",
            "KIRO",
            "--activity-prefix",
            "kiro-spec-",
            "--subtitle",
            "{detail}",
            "--image",
            "k.circle",
            "--priority",
            "80",
            "--queue-dir",
            str(tmp_path),
            "--json",
        ]
    )

    assert code == 0
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload == {
        "id": "kiro.spec",
        "kind": "live_activity",
        "title": "KIRO",
        "priority": 80,
        "activity_prefix": "kiro-spec-",
        "subtitle": "{detail}",
        "image": "k.circle",
    }
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "kiro.spec"
    assert out["queue_path"] == str(files[0])
