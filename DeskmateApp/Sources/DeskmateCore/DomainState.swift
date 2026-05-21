import Foundation

/// Authoritative non-UI state (V10 L1-A / L1-B).
///
/// Pet / Island / MenuBar views subscribe to this state via the
/// ``CompanionStateStore`` and never mutate it directly.
public enum Priority: String, Codable, Sendable, CaseIterable {
    case p0 = "P0"
    case p1 = "P1"
    case p2 = "P2"
    case p3 = "P3"
}

public enum UserFocus: String, Codable, Sendable, CaseIterable {
    case focused
    case casual
    case idleBack = "idle_back"
}

public enum AgentMood: String, Codable, Sendable, CaseIterable {
    case idle
    case working
    case thinking
    case happy
    case alert
}

public struct DomainState: Codable, Sendable, Equatable {
    public var specVersion: Int
    public var currentPriority: Priority
    public var userFocus: UserFocus
    public var agentMood: AgentMood
    public var pendingApprovals: [String]
    public var activeSessionId: String?
    /// V10 Phase 15-i: running sum of today's coding time in ms,
    /// computed from the persistent coding-session log.
    public var codingTodayMs: Int
    /// V10 Phase 15-i+: per-IDE breakdown of the same window,
    /// preserved in the order Python sent (descending by duration).
    /// An ``OrderedPairList`` is overkill here — Swift's ``[String: Int]``
    /// doesn't guarantee order, so we store it as a parallel
    /// ``[(String, Int)]`` for the menu bar to iterate, while the
    /// wire shape stays a JSON object.
    public var codingTodayByIde: [(String, Int)]
    /// V10 Phase 9 · §4 degradation: mirrors Python's
    /// ``degradation_level`` (0..6, monotonic). Swift decides FPS tier
    /// / SneakPeek HUD / island orderOut / camera observer policy by
    /// reading this field — no separate channel needed.
    public var degradationLevel: Int

    public init(
        specVersion: Int = BridgeProtocol.specVersion,
        currentPriority: Priority = .p3,
        userFocus: UserFocus = .casual,
        agentMood: AgentMood = .idle,
        pendingApprovals: [String] = [],
        activeSessionId: String? = nil,
        codingTodayMs: Int = 0,
        codingTodayByIde: [(String, Int)] = [],
        degradationLevel: Int = 0
    ) {
        self.specVersion = specVersion
        self.currentPriority = currentPriority
        self.userFocus = userFocus
        self.agentMood = agentMood
        self.pendingApprovals = pendingApprovals
        self.activeSessionId = activeSessionId
        self.codingTodayMs = codingTodayMs
        self.codingTodayByIde = codingTodayByIde
        self.degradationLevel = degradationLevel
    }

    /// ``==`` for the tuple array since Swift won't synthesize it
    /// automatically.
    public static func == (lhs: DomainState, rhs: DomainState) -> Bool {
        return lhs.specVersion == rhs.specVersion
            && lhs.currentPriority == rhs.currentPriority
            && lhs.userFocus == rhs.userFocus
            && lhs.agentMood == rhs.agentMood
            && lhs.pendingApprovals == rhs.pendingApprovals
            && lhs.activeSessionId == rhs.activeSessionId
            && lhs.codingTodayMs == rhs.codingTodayMs
            && lhs.degradationLevel == rhs.degradationLevel
            && lhs.codingTodayByIde.count == rhs.codingTodayByIde.count
            && zip(lhs.codingTodayByIde, rhs.codingTodayByIde).allSatisfy {
                $0.0 == $1.0 && $0.1 == $1.1
            }
    }

    private enum CodingKeys: String, CodingKey {
        case specVersion = "spec_version"
        case currentPriority = "current_priority"
        case userFocus = "user_focus"
        case agentMood = "agent_mood"
        case pendingApprovals = "pending_approvals"
        case activeSessionId = "active_session_id"
        case codingTodayMs = "coding_today_ms"
        case codingTodayByIde = "coding_today_by_ide"
        case degradationLevel = "degradation_level"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.specVersion = try c.decodeIfPresent(Int.self, forKey: .specVersion)
            ?? BridgeProtocol.specVersion
        self.currentPriority = try c.decodeIfPresent(Priority.self, forKey: .currentPriority) ?? .p3
        self.userFocus = try c.decodeIfPresent(UserFocus.self, forKey: .userFocus) ?? .casual
        self.agentMood = try c.decodeIfPresent(AgentMood.self, forKey: .agentMood) ?? .idle
        self.pendingApprovals = try c.decodeIfPresent([String].self, forKey: .pendingApprovals)
            ?? []
        self.activeSessionId = try c.decodeIfPresent(String.self, forKey: .activeSessionId)
        self.codingTodayMs = try c.decodeIfPresent(Int.self, forKey: .codingTodayMs) ?? 0
        // Clamp defensively to 0..6 (plan's monotonic range) so a
        // corrupt snapshot can't push us into a silent negative /
        // out-of-range state.
        let rawLevel = try c.decodeIfPresent(Int.self, forKey: .degradationLevel) ?? 0
        self.degradationLevel = max(0, min(6, rawLevel))
        // Preserve Python's insertion order by decoding into a
        // sequenced ``[(String, Int)]``. We accept both
        // wire shapes for forward compatibility:
        //   1. JSON object keyed by IDE name (what the projector ships)
        //   2. JSON array of ``[name, ms]`` pairs (future-proof)
        let presentAndNonNil: Bool
        if c.contains(.codingTodayByIde) {
            presentAndNonNil = try !c.decodeNil(forKey: .codingTodayByIde)
        } else {
            presentAndNonNil = false
        }
        let hasBreakdown = presentAndNonNil
        if hasBreakdown {
            self.codingTodayByIde = try Self.decodeOrderedBreakdown(
                from: c, key: .codingTodayByIde
            )
        } else {
            self.codingTodayByIde = []
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(specVersion, forKey: .specVersion)
        try c.encode(currentPriority, forKey: .currentPriority)
        try c.encode(userFocus, forKey: .userFocus)
        try c.encode(agentMood, forKey: .agentMood)
        try c.encode(pendingApprovals, forKey: .pendingApprovals)
        try c.encodeIfPresent(activeSessionId, forKey: .activeSessionId)
        try c.encode(codingTodayMs, forKey: .codingTodayMs)
        try c.encode(degradationLevel, forKey: .degradationLevel)
        // Round-trip as a JSON object to match the Python shape;
        // Swift loses the order on re-serialization, which is fine
        // because Swift never ships this field back to Python.
        let pairs: [(String, Int)] = codingTodayByIde
        var dict: [String: Int] = [:]
        for (k, v) in pairs {
            dict[k] = v
        }
        try c.encode(dict, forKey: .codingTodayByIde)
    }

    private static func decodeOrderedBreakdown(
        from c: KeyedDecodingContainer<CodingKeys>,
        key: CodingKeys
    ) throws -> [(String, Int)] {
        // Try the canonical object shape first.
        if let dict = try? c.decode(
            [String: Int].self, forKey: key
        ) {
            // Python's ``json.dumps`` preserves insertion order, but
            // Swift's ``JSONDecoder`` → ``[String: Int]`` does not.
            // Sort descending by ms to reconstruct the intended
            // presentation order.
            return dict.sorted { $0.value > $1.value }
                .map { ($0.key, $0.value) }
        }
        // Future-compat: ``[["Xcode", 3600000], ["Zed", 900000]]``.
        if let arr = try? c.decode([[String: Int]].self, forKey: key) {
            return arr.compactMap { pair in
                guard let first = pair.first else { return nil }
                return (first.key, first.value)
            }
        }
        return []
    }
}
