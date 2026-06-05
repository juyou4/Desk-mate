"""Deskmate IPC protocol layer (V10 L1).

All types in this package are the Python-side mirror of `shared/protocol.md`
and must stay forward-compatible: unknown fields are preserved, known enums
may only be extended additively.
"""

from __future__ import annotations

from .actions import ActionSource, ActionTarget, InteractionAction, InteractionKind
from .character_pack import (
    AccessoryAction,
    BubbleConfig,
    CharacterAvatarConfig,
    CharacterPackManifest,
    IdleTransitionRule,
    StateFrames,
)
from .envelope import BridgeEnvelope, EnvelopeType, new_trace_id
from .intents import CompanionIntent, IntentKind, IslandModuleSpec, register_module_intent
from .state import (
    AgentMood,
    BubbleAction,
    BubbleKind,
    BubbleSpec,
    DomainState,
    IslandSurfaceKind,
    IslandSurfaceState,
    MenuBarState,
    NestBehaviorPolicy,
    PetAnchor,
    PetAnchorKind,
    PetPresentationState,
    PetVelocity,
    Priority,
    UserFocus,
)

__all__ = [
    "AccessoryAction",
    "ActionSource",
    "ActionTarget",
    "AgentMood",
    "BridgeEnvelope",
    "BubbleAction",
    "BubbleConfig",
    "BubbleKind",
    "BubbleSpec",
    "CharacterAvatarConfig",
    "CharacterPackManifest",
    "CompanionIntent",
    "DomainState",
    "EnvelopeType",
    "IdleTransitionRule",
    "IntentKind",
    "InteractionAction",
    "InteractionKind",
    "IslandSurfaceKind",
    "IslandSurfaceState",
    "IslandModuleSpec",
    "MenuBarState",
    "NestBehaviorPolicy",
    "PetAnchor",
    "PetAnchorKind",
    "PetPresentationState",
    "PetVelocity",
    "Priority",
    "StateFrames",
    "UserFocus",
    "new_trace_id",
    "register_module_intent",
]
