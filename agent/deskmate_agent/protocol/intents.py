"""CompanionIntent: router → presentation middleware (V10 L1-C).

The router (Python) emits CompanionIntents describing *what* should be shown;
the Swift side decides *how* to render. Python never constructs raw view
instructions like ``pet.speak`` or ``island.show`` directly anymore.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IntentKind(StrEnum):
    SHOW_PET_BUBBLE = "show_pet_bubble"
    UPDATE_PET_BUBBLE = "update_pet_bubble"
    DISMISS_PET_BUBBLE = "dismiss_pet_bubble"
    SET_PET_ANIMATION = "set_pet_animation"
    SET_AVATAR_MOOD = "set_avatar_mood"
    PRESENT_ISLAND = "present_island"
    UPDATE_ISLAND = "update_island"
    DISMISS_ISLAND = "dismiss_island"
    UPDATE_DOMAIN_STATE = "update_domain_state"
    REGISTER_MODULE = "register_module"


class CompanionIntent(BaseModel):
    """Intermediate instruction Python emits and Swift consumes."""

    model_config = ConfigDict(extra="allow")

    kind: IntentKind
    payload: dict[str, Any] = Field(default_factory=dict)


class IslandModuleSpec(BaseModel):
    """Wire spec for a Swift-side island module registration."""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    title: str
    priority: int = 50
    activity_prefix: str | None = None
    subtitle: str | None = None
    image: str | None = None

    def to_intent(self) -> CompanionIntent:
        """Build the canonical ``register_module`` CompanionIntent."""

        return CompanionIntent(
            kind=IntentKind.REGISTER_MODULE,
            payload=self.model_dump(exclude_none=True),
        )


def register_module_intent(spec: IslandModuleSpec) -> CompanionIntent:
    """Convenience builder for external live-activity module registration."""

    return spec.to_intent()


__all__ = [
    "CompanionIntent",
    "IntentKind",
    "IslandModuleSpec",
    "register_module_intent",
]
