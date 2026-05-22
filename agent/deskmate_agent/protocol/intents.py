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


class CompanionIntent(BaseModel):
    """Intermediate instruction Python emits and Swift consumes."""

    model_config = ConfigDict(extra="allow")

    kind: IntentKind
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = ["CompanionIntent", "IntentKind"]
