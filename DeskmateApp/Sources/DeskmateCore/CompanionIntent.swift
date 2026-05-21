import Foundation

/// Companion intent kinds emitted by the router (Python) and consumed by
/// the Swift presentation dispatcher (V10 L1-C).
///
/// Python never constructs ``pet.speak`` / ``island.show`` directly anymore;
/// all UI instructions flow through this intermediate layer so the Swift
/// side remains free to change views without touching the protocol.
public enum IntentKind: String, Codable, Sendable, CaseIterable {
    case showPetBubble = "show_pet_bubble"
    case dismissPetBubble = "dismiss_pet_bubble"
    case setPetAnimation = "set_pet_animation"
    case setAvatarMood = "set_avatar_mood"
    case presentIsland = "present_island"
    case updateIsland = "update_island"
    case dismissIsland = "dismiss_island"
    case updateDomainState = "update_domain_state"
    /// Forward-compat sentinel: Python may add new intent kinds that
    /// an older Swift client doesn't yet render. We decode them to
    /// ``.unknown`` so the dispatcher can silently drop them instead
    /// of failing the whole decode.
    case unknown

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = IntentKind(rawValue: raw) ?? .unknown
    }
}

public struct CompanionIntent: Codable, Sendable, Equatable {
    public var kind: IntentKind
    public var payload: [String: AnyJSONValue]

    public init(kind: IntentKind, payload: [String: AnyJSONValue] = [:]) {
        self.kind = kind
        self.payload = payload
    }

    private enum CodingKeys: String, CodingKey {
        case kind, payload
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.kind = try c.decode(IntentKind.self, forKey: .kind)
        self.payload = try c.decodeIfPresent([String: AnyJSONValue].self, forKey: .payload)
            ?? [:]
    }
}
