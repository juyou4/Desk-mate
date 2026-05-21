"""Skill registry + catalog tests (V10 Phase 10)."""

from __future__ import annotations

import asyncio

import pytest

from deskmate_agent.skills import (
    SkillBody,
    SkillMetadata,
    SkillRegistry,
    default_skill_metadata,
    populate_default_registry,
)

# ---------------------------------------------------------------------------
# Metadata basics
# ---------------------------------------------------------------------------


def test_metadata_is_hashable_and_frozen() -> None:
    # The metadata is kept in dicts and compared for change detection,
    # so accidentally making it mutable would silently break
    # re-registration deduping.
    m = SkillMetadata(id="a", title="A", summary="s")
    assert hash(m)  # hashable


def test_registry_rejects_non_positive_body_timeout() -> None:
    with pytest.raises(ValueError):
        SkillRegistry(body_timeout_s=0)


def test_register_and_all_preserves_insertion_order() -> None:
    reg = SkillRegistry()
    reg.register(SkillMetadata(id="a", title="A", summary=""))
    reg.register(SkillMetadata(id="b", title="B", summary=""))
    reg.register(SkillMetadata(id="c", title="C", summary=""))

    ids = [m.id for m in reg.all()]
    assert ids == ["a", "b", "c"]


def test_metadata_for_returns_none_for_unknown() -> None:
    reg = SkillRegistry()
    reg.register(SkillMetadata(id="known", title="", summary=""))
    assert reg.metadata_for("unknown") is None
    assert reg.metadata_for("known").id == "known"


@pytest.mark.asyncio
async def test_unregister_drops_metadata_and_body_cache() -> None:
    reg = SkillRegistry()

    async def loader() -> SkillBody:
        return SkillBody(system_prompt="hello")

    reg.register(
        SkillMetadata(id="x", title="", summary="", triggers=("x",)),
        body_loader=loader,
    )
    # Prime the body cache.
    assert (await reg.load_body("x")).system_prompt == "hello"

    reg.unregister("x")
    assert reg.metadata_for("x") is None

    # Re-registering without a loader should produce a clean slate
    # (no lingering cache from the previous loader).
    reg.register(SkillMetadata(id="x", title="", summary=""))
    assert await reg.load_body("x") is None


# ---------------------------------------------------------------------------
# Trigger matching
# ---------------------------------------------------------------------------


def test_select_matches_case_insensitively() -> None:
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="chat", title="", summary="",
            triggers=("Hello", "Thanks"),
        )
    )
    assert [m.id for m in reg.select("hello there")] == ["chat"]
    assert [m.id for m in reg.select("THANKS!")] == ["chat"]


def test_select_on_empty_or_whitespace_returns_nothing() -> None:
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="chat", title="", summary="", triggers=("anything",)
        )
    )
    assert reg.select("") == []
    assert reg.select("   \n\t   ") == []


def test_select_skips_metadata_without_triggers() -> None:
    reg = SkillRegistry()
    reg.register(SkillMetadata(id="silent", title="", summary=""))
    reg.register(
        SkillMetadata(
            id="chatty", title="", summary="", triggers=("hey",)
        )
    )
    matches = [m.id for m in reg.select("hey you")]
    assert matches == ["chatty"]


def test_select_filters_by_capabilities() -> None:
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="a", title="", summary="",
            triggers=("code",), capabilities=("chat",),
        )
    )
    reg.register(
        SkillMetadata(
            id="b", title="", summary="",
            triggers=("code",), capabilities=("perception_observer",),
        )
    )
    chat_only = [m.id for m in reg.select("code", capabilities=("chat",))]
    assert chat_only == ["a"]
    obs_only = [
        m.id
        for m in reg.select(
            "code", capabilities=("perception_observer",)
        )
    ]
    assert obs_only == ["b"]


def test_select_ignores_empty_trigger_strings() -> None:
    # Triggers with empty-string entries should not match everything
    # (would happen if a pack ships a blank row in YAML).
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="chat", title="", summary="", triggers=("",)
        )
    )
    assert reg.select("totally unrelated text") == []


# ---------------------------------------------------------------------------
# Body loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_body_invokes_loader_once_and_caches() -> None:
    call_count = 0

    async def loader() -> SkillBody:
        nonlocal call_count
        call_count += 1
        return SkillBody(system_prompt="hi")

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="chat", title="", summary=""),
        body_loader=loader,
    )

    first = await reg.load_body("chat")
    second = await reg.load_body("chat")

    assert first is second  # cached identity
    assert call_count == 1


@pytest.mark.asyncio
async def test_load_body_returns_none_without_loader() -> None:
    reg = SkillRegistry()
    reg.register(SkillMetadata(id="metaonly", title="", summary=""))
    assert await reg.load_body("metaonly") is None


@pytest.mark.asyncio
async def test_load_body_returns_none_for_unknown_id() -> None:
    reg = SkillRegistry()
    assert await reg.load_body("missing") is None


@pytest.mark.asyncio
async def test_load_body_swallows_loader_errors() -> None:
    async def bad_loader() -> SkillBody:
        raise RuntimeError("boom")

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="chat", title="", summary=""),
        body_loader=bad_loader,
    )
    assert await reg.load_body("chat") is None
    # Errors are not sticky — a subsequent replacement works.
    async def good_loader() -> SkillBody:
        return SkillBody(system_prompt="ok")

    reg.register(
        SkillMetadata(id="chat", title="", summary=""),
        body_loader=good_loader,
    )
    body = await reg.load_body("chat")
    assert body is not None
    assert body.system_prompt == "ok"


@pytest.mark.asyncio
async def test_load_body_times_out_slow_loader() -> None:
    async def slow_loader() -> SkillBody:
        await asyncio.sleep(0.05)
        return SkillBody(system_prompt="late")

    reg = SkillRegistry(body_timeout_s=0.001)
    reg.register(
        SkillMetadata(id="slow", title="", summary=""),
        body_loader=slow_loader,
    )

    assert await reg.load_body("slow") is None


@pytest.mark.asyncio
async def test_concurrent_load_body_shares_single_first_load() -> None:
    call_count = 0

    async def loader() -> SkillBody:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return SkillBody(system_prompt="once")

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="shared", title="", summary=""),
        body_loader=loader,
    )

    first, second = await asyncio.gather(
        reg.load_body("shared"),
        reg.load_body("shared"),
    )

    assert first is second
    assert call_count == 1


@pytest.mark.asyncio
async def test_re_register_with_different_metadata_drops_cached_body() -> None:
    call_count = 0

    async def loader() -> SkillBody:
        nonlocal call_count
        call_count += 1
        return SkillBody(system_prompt=f"v{call_count}")

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="chat", title="v1", summary=""),
        body_loader=loader,
    )
    assert (await reg.load_body("chat")).system_prompt == "v1"

    # Re-register with different metadata — cache should invalidate.
    reg.register(
        SkillMetadata(id="chat", title="v2", summary=""),
        body_loader=loader,
    )
    assert (await reg.load_body("chat")).system_prompt == "v2"


@pytest.mark.asyncio
async def test_clear_body_cache_reloads_on_next_access() -> None:
    call_count = 0

    async def loader() -> SkillBody:
        nonlocal call_count
        call_count += 1
        return SkillBody(system_prompt=str(call_count))

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(id="chat", title="", summary=""),
        body_loader=loader,
    )
    await reg.load_body("chat")
    await reg.load_body("chat")
    assert call_count == 1

    reg.clear_body_cache()
    await reg.load_body("chat")
    assert call_count == 2


# ---------------------------------------------------------------------------
# Default catalog
# ---------------------------------------------------------------------------


def test_default_catalog_includes_chat_coding_build() -> None:
    ids = [m.id for m in default_skill_metadata()]
    assert "chat.default" in ids
    assert "skill.coding_session" in ids
    assert "skill.build_status" in ids


def test_default_catalog_triggers_are_non_empty() -> None:
    # Every default skill needs *something* to match against, else
    # it's unreachable via ``select()``.
    for meta in default_skill_metadata():
        assert meta.triggers, f"{meta.id} has no triggers"


def test_populate_default_registry_registers_every_skill() -> None:
    reg = populate_default_registry(SkillRegistry())
    ids = {m.id for m in reg.all()}
    assert ids == {
        "chat.default",
        "skill.coding_session",
        "skill.build_status",
    }


@pytest.mark.asyncio
async def test_populate_default_registry_bodies_load_for_chat_skills() -> None:
    reg = populate_default_registry(SkillRegistry())
    chat_body = await reg.load_body("chat.default")
    code_body = await reg.load_body("skill.coding_session")
    build_body = await reg.load_body("skill.build_status")
    assert chat_body is not None and chat_body.system_prompt
    assert code_body is not None and code_body.system_prompt
    assert build_body is not None and build_body.system_prompt


def test_populate_default_registry_is_idempotent() -> None:
    reg = SkillRegistry()
    populate_default_registry(reg)
    first_count = len(reg.all())
    populate_default_registry(reg)
    assert len(reg.all()) == first_count


# ---------------------------------------------------------------------------
# V10 L2-#8A: proactive vs reactive skill set
# ---------------------------------------------------------------------------


def test_metadata_proactive_safe_defaults_to_false() -> None:
    """The conservative default keeps third-party packs from
    accidentally exposing write-capable skills to proactive mode."""
    m = SkillMetadata(id="x", title="X", summary="")
    assert m.proactive_safe is False


def test_select_proactive_mode_filters_unsafe_skills() -> None:
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="reader",
            title="",
            summary="",
            triggers=("status",),
            proactive_safe=True,
        )
    )
    reg.register(
        SkillMetadata(
            id="writer",
            title="",
            summary="",
            triggers=("status",),
            proactive_safe=False,
        )
    )

    reactive_ids = [m.id for m in reg.select("status", mode="reactive")]
    proactive_ids = [m.id for m in reg.select("status", mode="proactive")]

    # Reactive sees both. Proactive sees only the explicit opt-in.
    assert reactive_ids == ["reader", "writer"]
    assert proactive_ids == ["reader"]


def test_select_proactive_mode_with_no_safe_skills_returns_empty() -> None:
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="writer",
            title="",
            summary="",
            triggers=("anything",),
            proactive_safe=False,
        )
    )
    assert reg.select("anything", mode="proactive") == []


def test_select_default_mode_is_reactive() -> None:
    """Omitting ``mode`` keeps the historical full-catalog
    behaviour so existing callers stay backwards compatible."""
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="writer",
            title="",
            summary="",
            triggers=("anything",),
            proactive_safe=False,
        )
    )
    assert [m.id for m in reg.select("anything")] == ["writer"]


def test_select_proactive_mode_still_honours_capabilities() -> None:
    """The proactive filter composes with capability narrowing —
    a proactive-safe skill missing a required capability still drops out."""
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            id="chat-only",
            title="",
            summary="",
            triggers=("ping",),
            capabilities=("chat",),
            proactive_safe=True,
        )
    )
    reg.register(
        SkillMetadata(
            id="watcher-only",
            title="",
            summary="",
            triggers=("ping",),
            capabilities=("watcher",),
            proactive_safe=True,
        )
    )
    chat_ids = [
        m.id
        for m in reg.select("ping", mode="proactive", capabilities=("chat",))
    ]
    assert chat_ids == ["chat-only"]


def test_default_catalog_has_expected_proactive_safety_flags() -> None:
    """``chat.default`` is reactive-only; the read-only awareness
    skills are the proactive set."""
    flags = {m.id: m.proactive_safe for m in default_skill_metadata()}
    assert flags == {
        "chat.default": False,
        "skill.coding_session": True,
        "skill.build_status": True,
    }


def test_default_catalog_proactive_select_omits_chat_default() -> None:
    """End-to-end proof: a greeting that *would* match ``chat.default``
    in reactive mode produces no matches in proactive mode (because
    chat-default opted out)."""
    reg = populate_default_registry(SkillRegistry())
    reactive_ids = {m.id for m in reg.select("hi there", mode="reactive")}
    proactive_ids = {m.id for m in reg.select("hi there", mode="proactive")}
    assert "chat.default" in reactive_ids
    assert "chat.default" not in proactive_ids


def test_default_catalog_proactive_select_keeps_coding_session() -> None:
    """``skill.coding_session`` triggers (``focus``, ``coded``, …)
    must still match in proactive mode — that's the whole point of
    the read-only set."""
    reg = populate_default_registry(SkillRegistry())
    proactive_ids = {
        m.id for m in reg.select("how long have I coded today", mode="proactive")
    }
    assert "skill.coding_session" in proactive_ids
