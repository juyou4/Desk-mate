"""Hook installer tests."""

from __future__ import annotations

import json

from deskmate_agent.cli import main
from deskmate_agent.hook_installers import (
    CLAUDE_PERMISSION_TIMEOUT_S,
    install_hooks,
    normalize_install_source,
    status_hooks,
    uninstall_hooks,
)


def test_codex_install_preserves_user_hooks_and_uninstall_removes_managed(tmp_path) -> None:
    path = tmp_path / "codex-config.json"
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [{"type": "command", "command": "user-script"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    source = normalize_install_source("codex")

    installed = install_hooks(
        source,
        config_path=path,
        hook_command="python -m deskmate_agent.cli hook ingest --source codex",
    )

    assert installed.changed
    data = json.loads(path.read_text(encoding="utf-8"))
    session_groups = data["hooks"]["SessionStart"]
    assert any(g["hooks"][0]["command"] == "user-script" for g in session_groups)
    assert status_hooks(source, config_path=path).installed

    removed = uninstall_hooks(source, config_path=path)

    assert removed.changed
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "user-script"
    assert status_hooks(source, config_path=path).managed_count == 0


def test_codex_toml_install_writes_feature_and_hooks_json_then_rolls_back(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[features]\nmemories = true\n', encoding="utf-8")
    source = normalize_install_source("codex")

    installed = install_hooks(source, config_path=config)

    assert installed.installed
    assert "codex_hooks = true" in config.read_text(encoding="utf-8")
    hooks = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
    assert "SessionStart" in hooks["hooks"]
    assert (tmp_path / "deskmate-codex-hooks-install.json").exists()

    removed = uninstall_hooks(source, config_path=config)

    assert removed.changed
    assert "codex_hooks" not in config.read_text(encoding="utf-8")
    assert not (tmp_path / "hooks.json").exists()


def test_codex_toml_uninstall_preserves_user_enabled_feature(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[features]\ncodex_hooks = true\nmemories = true\n', encoding="utf-8")
    source = normalize_install_source("codex")

    install_hooks(source, config_path=config)
    uninstall_hooks(source, config_path=config)

    text = config.read_text(encoding="utf-8")
    assert "codex_hooks = true" in text
    assert "memories = true" in text


def test_claude_install_uses_long_permission_timeout(tmp_path) -> None:
    path = tmp_path / "settings.json"
    source = normalize_install_source("claude")

    install_hooks(source, config_path=path)

    data = json.loads(path.read_text(encoding="utf-8"))
    permission = data["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert permission["timeout"] == CLAUDE_PERMISSION_TIMEOUT_S
    assert "deskmate hook ingest --source claude" in permission["command"]


def test_cursor_install_status_uninstall_via_cli(tmp_path) -> None:
    path = tmp_path / "hooks.json"

    assert main(["hook", "install", "--source", "cursor", "--config", str(path)]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert "beforeSubmitPrompt" in data["hooks"]
    assert "afterFileEdit" in data["hooks"]

    assert main(["hook", "status", "--source", "cursor", "--config", str(path)]) == 0
    assert main(["hook", "uninstall", "--source", "cursor", "--config", str(path)]) == 0
    assert main(["hook", "status", "--source", "cursor", "--config", str(path)]) == 1
