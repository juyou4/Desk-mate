import Foundation

/// Pure builders for every :class:`InteractionAction` the UI layer
/// emits (V10 Phase 11d-ix).
///
/// Separating payload construction from the SwiftUI runtime has two
/// benefits:
///
/// 1. **Regression coverage.** XCTest / smoke can assert the exact
///    snake_case wire shape the Python side expects without booting
///    SwiftUI or a live bridge.
/// 2. **No behavioural drift.** All callers funnel through these
///    factories, so the translation from a human-friendly boolean
///    like ``allow: true`` into the canonical ``payload["allow"] =
///    .bool(true)`` is written once.
public enum InteractionActionFactory {
    /// Resolve an approval. The Python :class:`ApprovalRouter` reads
    /// ``approval_id`` and ``allow``; both fields must land as
    /// snake_case keys.
    public static func resolveApproval(
        id: String, allow: Bool, source: ActionSource = .menuBar
    ) -> InteractionAction {
        InteractionAction(
            source: source,
            target: .system,
            kind: .permissionResolve,
            payload: [
                "approval_id": .string(id),
                "allow": .bool(allow),
            ]
        )
    }

    /// Ask the agent to restore focus on a session. The Python
    /// :class:`SessionInteractionRouter` routes on ``session_id``.
    public static func jumpToSession(
        id: String, source: ActionSource = .menuBar
    ) -> InteractionAction {
        InteractionAction(
            source: source,
            target: .session,
            kind: .sessionJump,
            payload: ["session_id": .string(id)]
        )
    }

    /// Answer an agent question inline from the island/menu bar. Python's
    /// session router reads ``session_id`` and ``answer``.
    public static func answerQuestion(
        sessionId: String,
        answer: String,
        source: ActionSource = .island
    ) -> InteractionAction {
        InteractionAction(
            source: source,
            target: .session,
            kind: .questionAnswer,
            payload: [
                "session_id": .string(sessionId),
                "answer": .string(answer),
            ]
        )
    }

    public static func dismissSurface(
        surface: IslandSurfaceKind? = nil,
        source: ActionSource = .island
    ) -> InteractionAction {
        var payload: [String: AnyJSONValue] = [:]
        if let surface {
            payload["surface"] = .string(surface.rawValue)
        }
        return InteractionAction(
            source: source,
            target: .system,
            kind: .surfaceDismiss,
            payload: payload
        )
    }

    /// Developer-demo trigger. The menu bar never mutates local
    /// stores for demos; it sends this action to Python and waits for
    /// the normal intent/snapshot path to hydrate UI state.
    public static func demoTrigger(
        scenario: String, source: ActionSource = .menuBar
    ) -> InteractionAction {
        InteractionAction(
            source: source,
            target: .system,
            kind: .demoTrigger,
            payload: ["scenario": .string(scenario)]
        )
    }

    /// Raw "the user clicked the pet sprite" event — used when the
    /// pet itself is the source surface and there's no bubble-action
    /// payload to carry. Produces a ``pet.interact`` action which
    /// Python can route to skills later.
    public static func petInteract(
        gesture: String = "click", source: ActionSource = .pet
    ) -> InteractionAction {
        InteractionAction(
            source: source,
            target: .bubble,
            kind: .petInteract,
            payload: ["gesture": .string(gesture)]
        )
    }

    /// Translate a :class:`BubbleAction` (declared by Python inside a
    /// :class:`BubbleSpec`) into a typed :class:`InteractionAction`
    /// ready for the wire. Returns ``nil`` if the bubble's
    /// ``interaction_kind`` string is outside the finite
    /// :class:`InteractionKind` set — a newer agent shipping an
    /// action an older shell doesn't understand must degrade
    /// silently, not crash.
    public static func bubbleAction(
        _ action: BubbleAction, bubbleId: String,
        source: ActionSource = .pet
    ) -> InteractionAction? {
        guard let kind = InteractionKind(rawValue: action.interactionKind)
        else { return nil }
        var payload = action.payload
        payload["bubble_id"] = .string(bubbleId)
        return InteractionAction(
            source: source,
            target: .bubble,
            kind: kind,
            payload: payload
        )
    }
}
