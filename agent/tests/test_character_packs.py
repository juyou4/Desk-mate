"""Character pack loader tests (V10 Phase 8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate_agent.character_packs import (
    ACTIVE_PACK_ENV,
    BUILTIN_PACK_ID,
    DEFAULT_PACKS_DIR_ENV,
    CharacterPackError,
    CharacterPackRegistry,
    build_default_registry,
    default_packs_dir,
    discover_packs,
    load_manifest,
    resolve_active_pack,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pack(
    root: Path,
    pack_id: str,
    *,
    display_name: str | None = None,
    spec_version: int = 1,
    states: dict[str, dict] | None = None,
    required_states: list[str] | None = None,
    avatar_default_style: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Write a minimal valid pack on disk and return its manifest path."""
    pack_dir = root / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "spec_version": spec_version,
        "id": pack_id,
        "display_name": display_name or pack_id,
        "states": states
        or {
            "idle": {"fps": 4, "frames": ["idle/000.png"]},
            "working": {"fps": 4, "frames": ["working/000.png"]},
            "thinking": {"fps": 4, "frames": ["thinking/000.png"]},
            "alert": {"fps": 4, "frames": ["alert/000.png"]},
        },
    }
    if required_states is not None:
        manifest["required_states"] = required_states
    if avatar_default_style is not None:
        manifest["avatar"] = {"default_style": avatar_default_style}
    if extra:
        manifest.update(extra)
    path = pack_dir / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------


def test_load_manifest_accepts_minimal_valid_pack(tmp_path: Path) -> None:
    path = _write_pack(tmp_path, "pixie")
    m = load_manifest(path)
    assert m.id == "pixie"
    assert m.display_name == "pixie"
    assert set(m.states.keys()) >= {"idle", "working", "thinking", "alert"}


def test_load_manifest_rejects_missing_required_states(
    tmp_path: Path,
) -> None:
    path = _write_pack(
        tmp_path,
        "broken",
        states={"idle": {"fps": 4, "frames": ["idle/000.png"]}},
    )
    with pytest.raises(CharacterPackError) as exc:
        load_manifest(path)
    # The error mentions which states are missing — tests become docs.
    assert "working" in str(exc.value)


def test_load_manifest_rejects_future_spec_version(
    tmp_path: Path,
) -> None:
    path = _write_pack(tmp_path, "futuristic", spec_version=9999)
    with pytest.raises(CharacterPackError) as exc:
        load_manifest(path)
    assert "spec_version" in str(exc.value)


def test_load_manifest_rejects_invalid_json(tmp_path: Path) -> None:
    pack_dir = tmp_path / "broken"
    pack_dir.mkdir()
    (pack_dir / "manifest.json").write_text("{ not-json", encoding="utf-8")
    with pytest.raises(CharacterPackError) as exc:
        load_manifest(pack_dir / "manifest.json")
    assert "JSON" in str(exc.value)


def test_load_manifest_rejects_non_object_root(tmp_path: Path) -> None:
    pack_dir = tmp_path / "broken"
    pack_dir.mkdir()
    (pack_dir / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(CharacterPackError) as exc:
        load_manifest(pack_dir / "manifest.json")
    assert "object" in str(exc.value)


def test_load_manifest_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CharacterPackError):
        load_manifest(tmp_path / "nowhere.json")


# ---------------------------------------------------------------------------
# discover_packs
# ---------------------------------------------------------------------------


def test_discover_packs_returns_empty_for_missing_root(tmp_path: Path) -> None:
    result = discover_packs(tmp_path / "does-not-exist")
    assert result.packs == ()
    assert result.skipped == {}


def test_discover_packs_loads_every_valid_pack(tmp_path: Path) -> None:
    _write_pack(tmp_path, "a")
    _write_pack(tmp_path, "b")
    _write_pack(tmp_path, "c")
    result = discover_packs(tmp_path)
    ids = {p.id for p in result.packs}
    assert ids == {"a", "b", "c"}
    assert result.skipped == {}


def test_discover_packs_is_sorted_deterministically(tmp_path: Path) -> None:
    # Write in reverse order; result should still be alphabetical.
    for pid in ("zebra", "mango", "apple"):
        _write_pack(tmp_path, pid)
    result = discover_packs(tmp_path)
    assert [p.id for p in result.packs] == ["apple", "mango", "zebra"]


def test_discover_packs_skips_bad_packs_and_reports_reason(
    tmp_path: Path,
) -> None:
    _write_pack(tmp_path, "ok")
    # Pack with missing required states — invalid.
    _write_pack(
        tmp_path,
        "broken",
        states={"idle": {"fps": 4, "frames": ["idle/000.png"]}},
    )
    # Directory without manifest.
    (tmp_path / "empty").mkdir()
    # Loose file at the root — ignored.
    (tmp_path / "stray.json").write_text("ignored", encoding="utf-8")

    result = discover_packs(tmp_path)
    assert [p.id for p in result.packs] == ["ok"]
    assert any("broken" in k for k in result.skipped)
    assert any("empty" in k for k in result.skipped)


def test_discover_packs_dedupes_by_id_with_warning(tmp_path: Path) -> None:
    # Two folders both declaring the same pack id.
    _write_pack(tmp_path, "first", extra={"id": "dup"})
    _write_pack(tmp_path, "second", extra={"id": "dup"})
    result = discover_packs(tmp_path)
    ids = [p.id for p in result.packs]
    assert ids == ["dup"]
    # Later-sorted folder wins.
    assert result.packs[0].display_name == "second"


# ---------------------------------------------------------------------------
# CharacterPackRegistry
# ---------------------------------------------------------------------------


def test_registry_preserves_insertion_order(tmp_path: Path) -> None:
    paths = [_write_pack(tmp_path, pid) for pid in ("first", "second", "third")]
    packs = [load_manifest(p) for p in paths]
    reg = CharacterPackRegistry(packs)
    assert reg.ids() == ["first", "second", "third"]


def test_registry_register_replaces_metadata(tmp_path: Path) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, "a", display_name="v1")))
    reg.register(
        load_manifest(_write_pack(tmp_path, "a", display_name="v2"))
    )
    assert len(reg) == 1
    assert reg.get("a").display_name == "v2"


def test_registry_unregister_removes_pack(tmp_path: Path) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, "a")))
    reg.register(load_manifest(_write_pack(tmp_path, "b")))
    reg.unregister("a")
    assert "a" not in reg
    assert reg.ids() == ["b"]


def test_registry_contains_and_len(tmp_path: Path) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, "a")))
    assert len(reg) == 1
    assert "a" in reg
    assert "missing" not in reg


# ---------------------------------------------------------------------------
# select_active_pack
# ---------------------------------------------------------------------------


def test_select_active_pack_prefers_explicit_id(tmp_path: Path) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, BUILTIN_PACK_ID)))
    reg.register(load_manifest(_write_pack(tmp_path, "custom")))
    assert reg.select_active_pack("custom").id == "custom"


def test_select_active_pack_falls_back_to_builtin(tmp_path: Path) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, "extra")))
    reg.register(load_manifest(_write_pack(tmp_path, BUILTIN_PACK_ID)))
    # Preferred id missing → pick built-in.
    assert reg.select_active_pack("ghost").id == BUILTIN_PACK_ID


def test_select_active_pack_falls_back_to_first_registered(
    tmp_path: Path,
) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, "only_one")))
    # No preferred, no builtin — pick the single entry.
    assert reg.select_active_pack().id == "only_one"


def test_select_active_pack_returns_none_for_empty_registry() -> None:
    assert CharacterPackRegistry().select_active_pack() is None


def test_select_active_pack_custom_fallback_order(tmp_path: Path) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, "first")))
    reg.register(load_manifest(_write_pack(tmp_path, "second")))
    pick = reg.select_active_pack(None, fallback_order=("second", "first"))
    assert pick.id == "second"


# ---------------------------------------------------------------------------
# build_default_registry + resolve_active_pack + env vars
# ---------------------------------------------------------------------------


def test_default_packs_dir_respects_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(DEFAULT_PACKS_DIR_ENV, str(tmp_path))
    assert default_packs_dir() == tmp_path


def test_default_packs_dir_defaults_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DEFAULT_PACKS_DIR_ENV, raising=False)
    assert default_packs_dir().as_posix().endswith("/.deskmate/packs")


def test_build_default_registry_reads_extra_roots(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    _write_pack(primary, "a")
    _write_pack(extra, "b")

    import os

    os.environ[DEFAULT_PACKS_DIR_ENV] = str(primary)
    try:
        reg = build_default_registry(extra_roots=(extra,))
    finally:
        del os.environ[DEFAULT_PACKS_DIR_ENV]

    assert set(reg.ids()) == {"a", "b"}


def test_build_default_registry_later_roots_win_on_collision(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    _write_pack(primary, "shared", display_name="from-primary")
    _write_pack(extra, "shared", display_name="from-extra")

    import os

    os.environ[DEFAULT_PACKS_DIR_ENV] = str(primary)
    try:
        reg = build_default_registry(extra_roots=(extra,))
    finally:
        del os.environ[DEFAULT_PACKS_DIR_ENV]

    assert reg.get("shared").display_name == "from-extra"


def test_resolve_active_pack_honours_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, BUILTIN_PACK_ID)))
    reg.register(load_manifest(_write_pack(tmp_path, "custom")))
    monkeypatch.setenv(ACTIVE_PACK_ENV, "custom")
    assert resolve_active_pack(reg).id == "custom"


def test_resolve_active_pack_argument_beats_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reg = CharacterPackRegistry()
    reg.register(load_manifest(_write_pack(tmp_path, "a")))
    reg.register(load_manifest(_write_pack(tmp_path, "b")))
    monkeypatch.setenv(ACTIVE_PACK_ENV, "b")
    assert resolve_active_pack(reg, preferred_id="a").id == "a"


# ---------------------------------------------------------------------------
# Bundled asset pack
# ---------------------------------------------------------------------------


def test_bundled_pixel_default_pack_loads() -> None:
    # Tests also serve as documentation: the shipped pack must stay
    # valid against the loader's rules at HEAD.
    bundled = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "packs"
        / BUILTIN_PACK_ID
        / "manifest.json"
    )
    assert bundled.is_file(), f"shipped pack missing at {bundled}"
    m = load_manifest(bundled)
    assert m.id == BUILTIN_PACK_ID
    assert "pixel" in m.avatar.supported_styles
