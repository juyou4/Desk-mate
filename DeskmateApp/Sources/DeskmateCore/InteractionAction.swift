import Foundation

/// Typed user interaction actions (V10 L1-F / I8).
///
/// Any UI-originated event — a pet tap, an island Allow button, a menu bar
/// Jump — is produced as an ``InteractionAction``. Free-form string actions
/// (``user.island_action { action: "join" }``) are forbidden at the wire
/// boundary; routing code may translate them internally but must emit typed
/// actions when talking to the agent.
public enum ActionSource: String, Codable, Sendable, CaseIterable {
    case pet
    case island
    case menuBar = "menu_bar"
}

public enum ActionTarget: String, Codable, Sendable, CaseIterable {
    case session
    case reminder
    case skill
    case system
    case bubble
}

public enum InteractionKind: String, Codable, Sendable, CaseIterable {
    // Session / approval
    case permissionResolve = "permission.resolve"
    case questionAnswer = "question.answer"
    case sessionJump = "session.jump"
    case taskOpenDetail = "task.open_detail"

    // Surface lifecycle
    case surfaceDismiss = "surface.dismiss"

    // Developer/demo controls
    case demoTrigger = "demo.trigger"

    // Pet-native
    case petInteract = "pet.interact"
    case petDrag = "pet.drag"
    case petNest = "pet.nest"
}

/// Single typed action produced by any UI surface.
public struct InteractionAction: Codable, Sendable, Equatable {
    public var source: ActionSource
    public var target: ActionTarget
    public var kind: InteractionKind
    public var payload: [String: AnyJSONValue]

    public init(
        source: ActionSource,
        target: ActionTarget,
        kind: InteractionKind,
        payload: [String: AnyJSONValue] = [:]
    ) {
        self.source = source
        self.target = target
        self.kind = kind
        self.payload = payload
    }

    private enum CodingKeys: String, CodingKey {
        case source, target, kind, payload
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.source = try c.decode(ActionSource.self, forKey: .source)
        self.target = try c.decode(ActionTarget.self, forKey: .target)
        self.kind = try c.decode(InteractionKind.self, forKey: .kind)
        self.payload = try c.decodeIfPresent([String: AnyJSONValue].self, forKey: .payload)
            ?? [:]
    }
}
