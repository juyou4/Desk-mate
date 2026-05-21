"""Opt-in hook installer helpers for external agent tools.

Installers are deliberately conservative: they mutate only managed Deskmate
entries, preserve unrelated user configuration, and can be directed at explicit
test paths from the CLI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

MANAGED_MARKER = "Managed by Deskmate"
DEFAULT_TIMEOUT_S = 45
CLAUDE_PERMISSION_TIMEOUT_S = 86_400
CODEX_MANIFEST_FILE = "deskmate-codex-hooks-install.json"


class HookInstallSource(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    CURSOR = "cursor"


@dataclass(frozen=True)
class HookInstallStatus:
    source: HookInstallSource
    config_path: Path
    installed: bool
    managed_count: int
    message: str


@dataclass(frozen=True)
class HookInstallResult(HookInstallStatus):
    changed: bool


def normalize_install_source(source: str) -> HookInstallSource:
    normalized = source.strip().lower().replace("-", "_")
    if normalized in {"claude_code", "claude"}:
        return HookInstallSource.CLAUDE
    try:
        return HookInstallSource(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported hook source: {source}") from exc


def default_config_path(source: HookInstallSource) -> Path:
    home = Path.home()
    if source is HookInstallSource.CODEX:
        return Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
    if source is HookInstallSource.CLAUDE:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "settings.json"
    if source is HookInstallSource.CURSOR:
        return Path(os.environ.get("CURSOR_CONFIG_DIR", home / ".cursor")) / "hooks.json"
    raise AssertionError(source)


def default_hook_command(source: HookInstallSource) -> str:
    return f"deskmate hook ingest --source {source.value}"


def status_hooks(
    source: HookInstallSource,
    *,
    config_path: Path | None = None,
    hook_command: str | None = None,
) -> HookInstallStatus:
    path = config_path or default_config_path(source)
    command = hook_command or default_hook_command(source)
    if source is HookInstallSource.CODEX and path.suffix != ".json":
        return _status_codex_toml(path, command)
    if not path.exists():
        return HookInstallStatus(source, path, False, 0, "config file not found")
    try:
        contents = path.read_text(encoding="utf-8")
        count = _managed_count(source, contents, command=command)
    except Exception as exc:  # noqa: BLE001
        return HookInstallStatus(source, path, False, 0, f"cannot read config: {exc}")
    return HookInstallStatus(
        source=source,
        config_path=path,
        installed=count > 0,
        managed_count=count,
        message="installed" if count > 0 else "not installed",
    )


def install_hooks(
    source: HookInstallSource,
    *,
    config_path: Path | None = None,
    hook_command: str | None = None,
) -> HookInstallResult:
    path = config_path or default_config_path(source)
    command = hook_command or default_hook_command(source)
    if source is HookInstallSource.CODEX and path.suffix != ".json":
        return _install_codex_toml(path, command)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if source is HookInstallSource.CODEX:
        updated = _install_codex(existing, command)
    elif source is HookInstallSource.CLAUDE:
        updated = _install_claude(existing, command)
    elif source is HookInstallSource.CURSOR:
        updated = _install_cursor(existing, command)
    else:
        raise AssertionError(source)

    changed = updated != existing
    if changed:
        _atomic_write(path, updated)
    status = status_hooks(source, config_path=path, hook_command=command)
    return HookInstallResult(
        source=source,
        config_path=path,
        installed=status.installed,
        managed_count=status.managed_count,
        message="installed" if changed else "already installed",
        changed=changed,
    )


def uninstall_hooks(
    source: HookInstallSource,
    *,
    config_path: Path | None = None,
    hook_command: str | None = None,
) -> HookInstallResult:
    path = config_path or default_config_path(source)
    command = hook_command or default_hook_command(source)
    if source is HookInstallSource.CODEX and path.suffix != ".json":
        return _uninstall_codex_toml(path, command)
    if not path.exists():
        return HookInstallResult(source, path, False, 0, "config file not found", False)
    existing = path.read_text(encoding="utf-8")

    if source is HookInstallSource.CODEX:
        updated = _uninstall_codex(existing, command)
    elif source is HookInstallSource.CLAUDE:
        updated = _uninstall_claude(existing, command)
    elif source is HookInstallSource.CURSOR:
        updated = _uninstall_cursor(existing, command)
    else:
        raise AssertionError(source)

    changed = updated != existing
    if changed:
        _atomic_write(path, updated)
    status = status_hooks(source, config_path=path, hook_command=command)
    return HookInstallResult(
        source=source,
        config_path=path,
        installed=status.installed,
        managed_count=status.managed_count,
        message="uninstalled" if changed else "not installed",
        changed=changed,
    )


def _install_codex(existing: str, command: str) -> str:
    data = _load_json_object(existing)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event, matcher in (
        ("SessionStart", "startup|resume"),
        ("UserPromptSubmit", None),
        ("Stop", None),
        ("PreToolUse", None),
        ("PostToolUse", None),
    ):
        groups = _clean_codex_groups(hooks.get(event), command)
        groups.append(_codex_group(command, matcher=matcher))
        hooks[event] = groups
    data["hooks"] = hooks
    return _dump_json_object(data)


def _status_codex_toml(config_path: Path, command: str) -> HookInstallStatus:
    codex_dir = config_path.parent
    hooks_path = codex_dir / "hooks.json"
    feature_enabled = False
    if config_path.exists():
        try:
            feature_enabled = _codex_feature_enabled(config_path.read_text(encoding="utf-8"))
        except OSError:
            feature_enabled = False
    count = 0
    if hooks_path.exists():
        try:
            count = _managed_count(
                HookInstallSource.CODEX,
                hooks_path.read_text(encoding="utf-8"),
                command=command,
            )
        except Exception:  # noqa: BLE001
            count = 0
    if count > 0 and feature_enabled:
        message = "installed"
    elif count > 0:
        message = "hooks present but codex_hooks feature disabled"
    elif feature_enabled:
        message = "feature enabled but managed hooks missing"
    else:
        message = "not installed"
    return HookInstallStatus(
        source=HookInstallSource.CODEX,
        config_path=config_path,
        installed=count > 0 and feature_enabled,
        managed_count=count,
        message=message,
    )


def _install_codex_toml(config_path: Path, command: str) -> HookInstallResult:
    codex_dir = config_path.parent
    hooks_path = codex_dir / "hooks.json"
    manifest_path = codex_dir / CODEX_MANIFEST_FILE
    existing_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    existing_hooks = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else ""

    feature_was_enabled = _codex_feature_enabled(existing_config)
    updated_config = _enable_codex_feature(existing_config)
    updated_hooks = _install_codex(existing_hooks, command)
    changed = updated_config != existing_config or updated_hooks != existing_hooks

    if updated_config != existing_config:
        _atomic_write(config_path, updated_config)
    if updated_hooks != existing_hooks:
        _atomic_write(hooks_path, updated_hooks)
    _atomic_write(
        manifest_path,
        _dump_json_object(
            {
                "hook_command": command,
                "enabled_codex_hooks_feature": not feature_was_enabled,
            }
        ),
    )
    status = _status_codex_toml(config_path, command)
    return HookInstallResult(
        source=HookInstallSource.CODEX,
        config_path=config_path,
        installed=status.installed,
        managed_count=status.managed_count,
        message="installed" if changed else "already installed",
        changed=changed,
    )


def _uninstall_codex_toml(config_path: Path, command: str) -> HookInstallResult:
    codex_dir = config_path.parent
    hooks_path = codex_dir / "hooks.json"
    manifest_path = codex_dir / CODEX_MANIFEST_FILE
    existing_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    existing_hooks = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else ""
    manifest = _load_manifest(manifest_path)

    updated_hooks = _uninstall_codex(existing_hooks, command) if existing_hooks.strip() else existing_hooks
    updated_config = existing_config
    if manifest.get("enabled_codex_hooks_feature") is True:
        updated_config = _disable_codex_feature(existing_config)

    changed = updated_config != existing_config or updated_hooks != existing_hooks or manifest_path.exists()
    if updated_config != existing_config:
        _atomic_write(config_path, updated_config)
    if updated_hooks != existing_hooks:
        if _json_object_is_empty(updated_hooks):
            hooks_path.unlink(missing_ok=True)
        else:
            _atomic_write(hooks_path, updated_hooks)
    manifest_path.unlink(missing_ok=True)

    status = _status_codex_toml(config_path, command)
    return HookInstallResult(
        source=HookInstallSource.CODEX,
        config_path=config_path,
        installed=status.installed,
        managed_count=status.managed_count,
        message="uninstalled" if changed else "not installed",
        changed=changed,
    )


def _uninstall_codex(existing: str, command: str) -> str:
    data = _load_json_object(existing)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return existing
    for event in list(hooks.keys()):
        groups = _clean_codex_groups(hooks.get(event), command)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return _dump_json_object(data)


def _install_claude(existing: str, command: str) -> str:
    data = _load_json_object(existing)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    specs = [
        ("UserPromptSubmit", None, None),
        ("SessionStart", None, None),
        ("SessionEnd", None, None),
        ("Stop", None, None),
        ("StopFailure", None, None),
        ("SubagentStart", None, None),
        ("SubagentStop", None, None),
        ("Notification", "*", None),
        ("PreToolUse", "*", None),
        ("PermissionRequest", "*", CLAUDE_PERMISSION_TIMEOUT_S),
        ("PostToolUse", "*", None),
        ("PostToolUseFailure", "*", None),
        ("PermissionDenied", "*", None),
        ("PreCompact", None, None),
    ]
    for event, matcher, timeout in specs:
        groups = _clean_claude_groups(hooks.get(event), command)
        groups.append(_claude_group(command, matcher=matcher, timeout=timeout))
        hooks[event] = groups
    data["hooks"] = hooks
    return _dump_json_object(data)


def _uninstall_claude(existing: str, command: str) -> str:
    data = _load_json_object(existing)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return existing
    for event in list(hooks.keys()):
        groups = _clean_claude_groups(hooks.get(event), command)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return _dump_json_object(data)


def _install_cursor(existing: str, command: str) -> str:
    data = _load_json_object(existing)
    data["version"] = data.get("version", 1)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event in (
        "beforeSubmitPrompt",
        "beforeShellExecution",
        "beforeMCPExecution",
        "beforeReadFile",
        "afterFileEdit",
        "stop",
    ):
        entries = _clean_cursor_entries(hooks.get(event), command)
        entries.append({"command": command, "description": MANAGED_MARKER})
        hooks[event] = entries
    data["hooks"] = hooks
    return _dump_json_object(data)


def _uninstall_cursor(existing: str, command: str) -> str:
    data = _load_json_object(existing)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return existing
    for event in list(hooks.keys()):
        entries = _clean_cursor_entries(hooks.get(event), command)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return _dump_json_object(data)


def _codex_group(command: str, *, matcher: str | None) -> dict[str, Any]:
    hook: dict[str, Any] = {
        "type": "command",
        "command": command,
        "timeout": DEFAULT_TIMEOUT_S,
        "description": MANAGED_MARKER,
    }
    group: dict[str, Any] = {"hooks": [hook]}
    if matcher is not None:
        group["matcher"] = matcher
    return group


def _claude_group(command: str, *, matcher: str | None, timeout: int | None) -> dict[str, Any]:
    hook: dict[str, Any] = {
        "type": "command",
        "command": command,
        "description": MANAGED_MARKER,
    }
    if timeout is not None:
        hook["timeout"] = timeout
    group: dict[str, Any] = {"hooks": [hook]}
    if matcher is not None:
        group["matcher"] = matcher
    return group


def _clean_codex_groups(value: Any, command: str) -> list[dict[str, Any]]:
    return _clean_group_hooks(value, command)


def _clean_claude_groups(value: Any, command: str) -> list[dict[str, Any]]:
    return _clean_group_hooks(value, command)


def _clean_group_hooks(value: Any, command: str) -> list[dict[str, Any]]:
    groups = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        hooks = item.get("hooks")
        hook_items = hooks if isinstance(hooks, list) else []
        filtered = [
            hook for hook in hook_items
            if isinstance(hook, dict) and not _is_managed_hook(hook, command)
        ]
        if filtered:
            cloned = dict(item)
            cloned["hooks"] = filtered
            out.append(cloned)
    return out


def _clean_cursor_entries(value: Any, command: str) -> list[dict[str, Any]]:
    entries = value if isinstance(value, list) else []
    return [
        item for item in entries
        if isinstance(item, dict) and not _is_managed_hook(item, command)
    ]


def _managed_count(source: HookInstallSource, contents: str, *, command: str) -> int:
    data = _load_json_object(contents)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    count = 0
    if source in {HookInstallSource.CODEX, HookInstallSource.CLAUDE}:
        for value in hooks.values():
            groups = value if isinstance(value, list) else []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                hook_items = group.get("hooks") if isinstance(group.get("hooks"), list) else []
                count += sum(
                    1 for hook in hook_items
                    if isinstance(hook, dict) and _is_managed_hook(hook, command)
                )
    elif source is HookInstallSource.CURSOR:
        for value in hooks.values():
            entries = value if isinstance(value, list) else []
            count += sum(
                1 for hook in entries
                if isinstance(hook, dict) and _is_managed_hook(hook, command)
            )
    return count


def _is_managed_hook(hook: dict[str, Any], command: str) -> bool:
    hook_command = hook.get("command")
    if hook_command == command:
        return True
    description = str(hook.get("description", ""))
    if MANAGED_MARKER in description:
        return True
    if isinstance(hook_command, str):
        lowered = hook_command.lower()
        return "deskmate" in lowered and "hook ingest" in lowered
    return False


def _codex_feature_enabled(contents: str) -> bool:
    in_features = False
    for raw in contents.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_features = line == "[features]"
            continue
        if in_features and line.split("#", 1)[0].strip() == "codex_hooks = true":
            return True
    return False


def _enable_codex_feature(contents: str) -> str:
    lines = contents.splitlines()
    if _codex_feature_enabled(contents):
        return _ensure_trailing_newline(contents)

    feature_range = _toml_section_range(lines, "features")
    if feature_range is not None:
        start, end = feature_range
        for idx in range(start + 1, end):
            key = lines[idx].split("=", 1)[0].strip()
            if key == "codex_hooks":
                lines[idx] = "codex_hooks = true"
                return "\n".join(lines) + "\n"
        lines.insert(end, "codex_hooks = true")
        return "\n".join(lines) + "\n"

    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(["[features]", "codex_hooks = true"])
    return "\n".join(lines) + "\n"


def _disable_codex_feature(contents: str) -> str:
    lines = contents.splitlines()
    feature_range = _toml_section_range(lines, "features")
    if feature_range is None:
        return _ensure_trailing_newline(contents)
    start, end = feature_range
    remove_idx: int | None = None
    for idx in range(start + 1, end):
        if lines[idx].split("=", 1)[0].strip() == "codex_hooks":
            remove_idx = idx
            break
    if remove_idx is None:
        return _ensure_trailing_newline(contents)
    del lines[remove_idx]

    feature_range = _toml_section_range(lines, "features")
    if feature_range is not None:
        start, end = feature_range
        body = [
            line.strip()
            for line in lines[start + 1:end]
            if line.strip() and not line.strip().startswith("#")
        ]
        if not body:
            del lines[start:end]
            while lines and not lines[-1].strip():
                lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def _toml_section_range(lines: list[str], section: str) -> tuple[int, int] | None:
    header = f"[{section}]"
    start: int | None = None
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not (line.startswith("[") and line.endswith("]")):
            continue
        if start is None:
            if line == header:
                start = idx
        else:
            return start, idx
    if start is None:
        return None
    return start, len(lines)


def _ensure_trailing_newline(contents: str) -> str:
    return contents if not contents or contents.endswith("\n") else contents + "\n"


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_object_is_empty(contents: str) -> bool:
    try:
        return _load_json_object(contents) == {}
    except Exception:  # noqa: BLE001
        return False


def _load_json_object(contents: str) -> dict[str, Any]:
    if not contents.strip():
        return {}
    data = json.loads(contents)
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    return data


def _dump_json_object(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(contents, encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "HookInstallResult",
    "HookInstallSource",
    "HookInstallStatus",
    "default_config_path",
    "default_hook_command",
    "install_hooks",
    "normalize_install_source",
    "status_hooks",
    "uninstall_hooks",
]
