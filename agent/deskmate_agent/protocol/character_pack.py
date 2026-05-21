"""CharacterPackManifest (V10 I4 / L1-D).

Manifest-first entry point for a character pack. Image directories are the
*resource layer*; code only reads through this manifest so new packs can be
dropped in without code changes.

All models tolerate unknown fields (``extra="allow"``) so future manifest
additions are safe for older readers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SPEC_VERSION = 1

DEFAULT_REQUIRED_STATES: tuple[str, ...] = ("idle", "working", "thinking", "alert")
DEFAULT_FALLBACKS: dict[str, str] = {
    "walking_left": "walking",
    "walking_right": "walking",
}


class CharacterAvatarConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    default_style: str = "pixel"
    supported_styles: list[str] = Field(default_factory=lambda: ["pixel"])


class StateFrames(BaseModel):
    """One animation state inside a character pack."""

    model_config = ConfigDict(extra="allow")

    fps: int = 4
    frames: list[str] = Field(default_factory=list)

    @field_validator("frames")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("states.*.frames must contain at least one frame path")
        return value


class IdleTransitionRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    to: str
    probability: float = 0.0


class BubbleConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    icon: str | None = None
    sounds: dict[str, str] = Field(default_factory=dict)
    templates: dict[str, str] = Field(default_factory=dict)


class AccessoryAction(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    act_list: list[str] = Field(default_factory=list)
    acc_list: list[str] = Field(default_factory=list)


class CharacterPackManifest(BaseModel):
    """Single entry point for a character pack."""

    model_config = ConfigDict(extra="allow")

    spec_version: int = SPEC_VERSION
    id: str
    display_name: str
    author: str | None = None
    canvas_size: tuple[int, int] = (32, 32)
    scale: float = 1.0
    palette: list[str] = Field(default_factory=list)

    avatar: CharacterAvatarConfig = Field(default_factory=CharacterAvatarConfig)

    states: dict[str, StateFrames] = Field(default_factory=dict)
    required_states: list[str] = Field(
        default_factory=lambda: list(DEFAULT_REQUIRED_STATES)
    )
    idle_transitions: dict[str, list[IdleTransitionRule]] = Field(default_factory=dict)
    bubble_config: BubbleConfig = Field(default_factory=BubbleConfig)
    accessory_act: list[AccessoryAction] = Field(default_factory=list)
    fallbacks: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FALLBACKS))
    avatar_slots: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def missing_required_states(self) -> list[str]:
        """Return every required state missing from ``states`` (preserves order)."""
        return [s for s in self.required_states if s not in self.states]

    def resolve_state(self, name: str) -> str | None:
        """Resolve a requested state, honoring ``fallbacks`` chains.

        Returns the concrete state name actually present in ``states``, or
        ``None`` when no fallback exists.
        """
        seen: set[str] = set()
        current: str | None = name
        while current is not None and current not in seen:
            if current in self.states:
                return current
            seen.add(current)
            current = self.fallbacks.get(current)
        return None


__all__ = [
    "DEFAULT_FALLBACKS",
    "DEFAULT_REQUIRED_STATES",
    "SPEC_VERSION",
    "AccessoryAction",
    "BubbleConfig",
    "CharacterAvatarConfig",
    "CharacterPackManifest",
    "IdleTransitionRule",
    "StateFrames",
]
