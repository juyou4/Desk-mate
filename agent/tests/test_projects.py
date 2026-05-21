"""ProjectRegistry tests (V10 Phase 15-ii)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate_agent.projects import (
    ProjectRegistry,
    ResolvedProject,
)


def _new_registry(tmp_path: Path) -> ProjectRegistry:
    reg = ProjectRegistry(path=tmp_path / "projects.json")
    reg.load()
    return reg


def test_add_persists_entry_to_json(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    reg.add("deskmate", repo, bundle_hints=["com.microsoft.VSCode"])
    data = json.loads((tmp_path / "projects.json").read_text())
    assert data["entries"][0] == {
        "name": "deskmate",
        "path": str(repo.resolve()),
        "bundle_hints": ["com.microsoft.VSCode"],
    }


def test_add_upserts_by_name(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    reg.add("deskmate", tmp_path / "a")
    reg.add("deskmate", tmp_path / "b")
    assert len(reg.list_all()) == 1
    assert reg.list_all()[0].path == str((tmp_path / "b").resolve())


def test_add_rejects_empty_name(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    with pytest.raises(ValueError):
        reg.add("   ", tmp_path)


def test_remove_returns_false_when_not_present(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    assert reg.remove("ghost") is False


def test_remove_removes_and_persists(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    reg.add("a", tmp_path)
    reg.add("b", tmp_path)
    assert reg.remove("a") is True
    reg2 = _new_registry(tmp_path)
    assert {e.name for e in reg2.list_all()} == {"b"}


def test_load_degrades_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "projects.json").write_text("not-json")
    reg = _new_registry(tmp_path)
    assert reg.list_all() == []


def test_resolve_returns_none_when_no_title(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    reg.add("deskmate", tmp_path)
    assert reg.resolve(bundle_id=None, window_title=None) is None


def test_resolve_matches_by_name_substring(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    (tmp_path / "deskmate").mkdir()
    reg.add("deskmate", tmp_path / "deskmate")
    resolved = reg.resolve(
        bundle_id="com.microsoft.VSCode",
        window_title="main.py — deskmate",
        branch_reader=lambda _: "feat/island",
    )
    assert isinstance(resolved, ResolvedProject)
    assert resolved.name == "deskmate"
    assert resolved.branch == "feat/island"


def test_resolve_prefers_bundle_hinted_entries(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    reg.add("app", tmp_path / "a")  # no hints
    reg.add("other", tmp_path / "b", bundle_hints=["com.apple.dt.Xcode"])
    # Window title contains both "app" and "other"; without hints
    # longest substring wins ("other"), but the Xcode hint steers
    # resolution to the hinted entry.
    resolved = reg.resolve(
        bundle_id="com.apple.dt.Xcode",
        window_title="app file — other",
        branch_reader=lambda _: "main",
    )
    assert resolved is not None
    assert resolved.name == "other"


def test_resolve_longest_substring_wins(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    reg.add("app", tmp_path / "a")
    reg.add("my-app", tmp_path / "b")
    resolved = reg.resolve(
        bundle_id=None,
        window_title="main.py — my-app",
        branch_reader=lambda _: "main",
    )
    assert resolved is not None
    assert resolved.name == "my-app"


def test_resolve_none_when_no_title_match(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    reg.add("deskmate", tmp_path / "deskmate")
    assert (
        reg.resolve(
            bundle_id=None,
            window_title="Safari — Daily news",
            branch_reader=lambda _: "main",
        )
        is None
    )


def test_resolve_absorbs_branch_reader_failures(tmp_path: Path) -> None:
    reg = _new_registry(tmp_path)
    reg.add("deskmate", tmp_path / "deskmate")

    def broken_reader(path: Path) -> str | None:
        raise RuntimeError("disk on fire")

    resolved = reg.resolve(
        bundle_id=None,
        window_title="main.py — deskmate",
        branch_reader=broken_reader,
    )
    # Entry matched but branch read blew up → branch None, not a
    # raised exception.
    assert resolved is not None
    assert resolved.branch is None


# ---------------------------------------------------------------------------
# CLI bindings
# ---------------------------------------------------------------------------


def test_cli_project_add_writes_registry(tmp_path, monkeypatch, capsys) -> None:
    projects = tmp_path / "projects.json"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DESKMATE_PROJECTS_PATH", str(projects))
    from deskmate_agent.cli import main

    rc = main(
        [
            "project",
            "add",
            str(repo),
            "--name",
            "deskmate",
            "--bundle-hint",
            "com.microsoft.VSCode",
        ]
    )
    assert rc == 0
    data = json.loads(projects.read_text())
    assert data["entries"][0]["name"] == "deskmate"
    assert data["entries"][0]["bundle_hints"] == ["com.microsoft.VSCode"]
    out = capsys.readouterr().out
    assert "Registered deskmate" in out


def test_cli_project_list_empty_and_populated(
    tmp_path, monkeypatch, capsys
) -> None:
    projects = tmp_path / "projects.json"
    monkeypatch.setenv("DESKMATE_PROJECTS_PATH", str(projects))
    from deskmate_agent.cli import main

    assert main(["project", "list"]) == 0
    assert "No projects registered" in capsys.readouterr().out

    main(["project", "add", str(tmp_path), "--name", "alpha"])
    capsys.readouterr()  # drain the "Registered" line
    main(["project", "list"])
    out = capsys.readouterr().out
    assert "alpha" in out
    assert str(tmp_path.resolve()) in out


def test_cli_project_list_json_shape(tmp_path, monkeypatch, capsys) -> None:
    projects = tmp_path / "projects.json"
    monkeypatch.setenv("DESKMATE_PROJECTS_PATH", str(projects))
    from deskmate_agent.cli import main

    main(
        [
            "project",
            "add",
            str(tmp_path),
            "--name",
            "alpha",
            "--bundle-hint",
            "com.microsoft.VSCode",
        ]
    )
    capsys.readouterr()
    main(["project", "list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "name": "alpha",
            "path": str(tmp_path.resolve()),
            "bundle_hints": ["com.microsoft.VSCode"],
        }
    ]


def test_cli_project_remove_reports_missing(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(
        "DESKMATE_PROJECTS_PATH", str(tmp_path / "projects.json")
    )
    from deskmate_agent.cli import main

    rc = main(["project", "remove", "ghost"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ghost" in err


def test_cli_project_remove_deletes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(
        "DESKMATE_PROJECTS_PATH", str(tmp_path / "projects.json")
    )
    from deskmate_agent.cli import main

    main(["project", "add", str(tmp_path), "--name", "alpha"])
    capsys.readouterr()
    rc = main(["project", "remove", "alpha"])
    assert rc == 0
    main(["project", "list"])
    assert "No projects registered" in capsys.readouterr().out
