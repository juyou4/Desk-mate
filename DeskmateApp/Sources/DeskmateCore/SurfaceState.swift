import Foundation

// MARK: - Pet surface (V10 I1)

public enum PetAnchorKind: String, Codable, Sendable, CaseIterable {
    case desktop
    case nest
    case transition
}

public struct PetVelocity: Codable, Sendable, Equatable {
    public var dx: Double
    public var dy: Double

    public init(dx: Double = 0, dy: Double = 0) {
        self.dx = dx
        self.dy = dy
    }
}

public struct PetAnchor: Codable, Sendable, Equatable {
    public var kind: PetAnchorKind
    public var targetNest: String?

    public init(kind: PetAnchorKind = .desktop, targetNest: String? = nil) {
        self.kind = kind
        self.targetNest = targetNest
    }

    private enum CodingKeys: String, CodingKey {
        case kind
        case targetNest = "target_nest"
    }
}

public struct NestBehaviorPolicy: Codable, Sendable, Equatable {
    public var canEnterNest: Bool
    public var shouldLeaveNest: Bool
    public var targetNest: String?

    public init(
        canEnterNest: Bool = true,
        shouldLeaveNest: Bool = false,
        targetNest: String? = nil
    ) {
        self.canEnterNest = canEnterNest
        self.shouldLeaveNest = shouldLeaveNest
        self.targetNest = targetNest
    }

    private enum CodingKeys: String, CodingKey {
        case canEnterNest = "can_enter_nest"
        case shouldLeaveNest = "should_leave_nest"
        case targetNest = "target_nest"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.canEnterNest = try c.decodeIfPresent(Bool.self, forKey: .canEnterNest) ?? true
        self.shouldLeaveNest = try c.decodeIfPresent(Bool.self, forKey: .shouldLeaveNest) ?? false
        self.targetNest = try c.decodeIfPresent(String.self, forKey: .targetNest)
    }
}

public struct PetPresentationState: Codable, Sendable, Equatable {
    public var animationState: String
    public var emotion: String
    public var attentionLevel: Double
    public var anchorKind: PetAnchorKind
    public var velocity: PetVelocity
    public var isInteractive: Bool
    public var bubbleId: String?
    public var avatarStyle: String

    public init(
        animationState: String = "idle",
        emotion: String = "neutral",
        attentionLevel: Double = 0,
        anchorKind: PetAnchorKind = .desktop,
        velocity: PetVelocity = PetVelocity(),
        isInteractive: Bool = true,
        bubbleId: String? = nil,
        avatarStyle: String = "pixel"
    ) {
        self.animationState = animationState
        self.emotion = emotion
        self.attentionLevel = attentionLevel
        self.anchorKind = anchorKind
        self.velocity = velocity
        self.isInteractive = isInteractive
        self.bubbleId = bubbleId
        self.avatarStyle = avatarStyle
    }

    private enum CodingKeys: String, CodingKey {
        case animationState = "animation_state"
        case emotion
        case attentionLevel = "attention_level"
        case anchorKind = "anchor_kind"
        case velocity
        case isInteractive = "is_interactive"
        case bubbleId = "bubble_id"
        case avatarStyle = "avatar_style"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.animationState = try c.decodeIfPresent(String.self, forKey: .animationState) ?? "idle"
        self.emotion = try c.decodeIfPresent(String.self, forKey: .emotion) ?? "neutral"
        self.attentionLevel = try c.decodeIfPresent(Double.self, forKey: .attentionLevel) ?? 0
        self.anchorKind = try c.decodeIfPresent(PetAnchorKind.self, forKey: .anchorKind) ?? .desktop
        self.velocity = try c.decodeIfPresent(PetVelocity.self, forKey: .velocity) ?? PetVelocity()
        self.isInteractive = try c.decodeIfPresent(Bool.self, forKey: .isInteractive) ?? true
        self.bubbleId = try c.decodeIfPresent(String.self, forKey: .bubbleId)
        self.avatarStyle = try c.decodeIfPresent(String.self, forKey: .avatarStyle) ?? "pixel"
    }
}

// MARK: - Island surface (V10 L1-E / I5)

public enum IslandSurfaceKind: String, Codable, Sendable, CaseIterable {
    case compact
    case notificationCard = "notification_card"
    case sessionList = "session_list"
    case liveActivity = "live_activity"
    case empty
}

public struct IslandSurfaceState: Codable, Sendable, Equatable {
    public var kind: IslandSurfaceKind
    public var sessionId: String?
    public var activityId: String?
    /// V10 Phase 13-ii: free-form secondary label (window title,
    /// session duration, progress text). Mirrors Python's
    /// ``IslandSurfaceState.detail``. Omitted on the wire when nil
    /// (``encodeIfPresent``) to keep older snapshots decodeable.
    public var detail: String?

    public init(
        kind: IslandSurfaceKind = .compact,
        sessionId: String? = nil,
        activityId: String? = nil,
        detail: String? = nil
    ) {
        self.kind = kind
        self.sessionId = sessionId
        self.activityId = activityId
        self.detail = detail
    }

    private enum CodingKeys: String, CodingKey {
        case kind
        case sessionId = "session_id"
        case activityId = "activity_id"
        case detail
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.kind = try c.decodeIfPresent(IslandSurfaceKind.self, forKey: .kind) ?? .compact
        self.sessionId = try c.decodeIfPresent(String.self, forKey: .sessionId)
        self.activityId = try c.decodeIfPresent(String.self, forKey: .activityId)
        self.detail = try c.decodeIfPresent(String.self, forKey: .detail)
    }
}

// MARK: - MenuBar surface

public struct MenuBarState: Codable, Sendable, Equatable {
    public var unreadCount: Int
    public var summaryText: String?

    public init(unreadCount: Int = 0, summaryText: String? = nil) {
        self.unreadCount = unreadCount
        self.summaryText = summaryText
    }

    private enum CodingKeys: String, CodingKey {
        case unreadCount = "unread_count"
        case summaryText = "summary_text"
    }
}

// MARK: - BubbleSpec (V10 I3)

public enum BubbleKind: String, Codable, Sendable, CaseIterable {
    case chat
    case status
    case approvalHint = "approval_hint"
    case reminder
    case randomReaction = "random_reaction"
    case system
}

public struct BubbleAction: Codable, Sendable, Equatable {
    public var label: String
    public var interactionKind: String
    public var payload: [String: AnyJSONValue]

    public init(label: String, interactionKind: String, payload: [String: AnyJSONValue] = [:]) {
        self.label = label
        self.interactionKind = interactionKind
        self.payload = payload
    }

    private enum CodingKeys: String, CodingKey {
        case label
        case interactionKind = "interaction_kind"
        case payload
    }
}

public struct BubbleSpec: Codable, Sendable, Equatable {
    public var id: String
    public var kind: BubbleKind
    public var icon: String?
    public var text: String
    public var markdown: String?
    public var actions: [BubbleAction]
    public var startAudio: String?
    public var endAudio: String?
    public var ttlMs: Int?
    public var priority: Priority
    public var sourceEventId: String?

    public init(
        id: String,
        kind: BubbleKind = .chat,
        icon: String? = nil,
        text: String = "",
        markdown: String? = nil,
        actions: [BubbleAction] = [],
        startAudio: String? = nil,
        endAudio: String? = nil,
        ttlMs: Int? = 8000,
        priority: Priority = .p2,
        sourceEventId: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.icon = icon
        self.text = text
        self.markdown = markdown
        self.actions = actions
        self.startAudio = startAudio
        self.endAudio = endAudio
        self.ttlMs = ttlMs
        self.priority = priority
        self.sourceEventId = sourceEventId
    }

    private enum CodingKeys: String, CodingKey {
        case id, kind, icon, text, markdown, actions
        case startAudio = "start_audio"
        case endAudio = "end_audio"
        case ttlMs = "ttl_ms"
        case priority
        case sourceEventId = "source_event_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try c.decode(String.self, forKey: .id)
        self.kind = try c.decodeIfPresent(BubbleKind.self, forKey: .kind) ?? .chat
        self.icon = try c.decodeIfPresent(String.self, forKey: .icon)
        self.text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        self.markdown = try c.decodeIfPresent(String.self, forKey: .markdown)
        self.actions = try c.decodeIfPresent([BubbleAction].self, forKey: .actions) ?? []
        self.startAudio = try c.decodeIfPresent(String.self, forKey: .startAudio)
        self.endAudio = try c.decodeIfPresent(String.self, forKey: .endAudio)
        // ``ttl_ms`` distinguishes three wire states:
        //   - key absent       → use default 8000 ms (legacy shape)
        //   - key = null       → explicit "no auto-hide" (approval / reminder)
        //   - key = <integer>  → that exact ttl
        // ``decodeIfPresent`` collapses absent + null into the same nil, so we
        // must peek with ``decodeNil`` to tell them apart.
        if c.contains(.ttlMs) {
            if try c.decodeNil(forKey: .ttlMs) {
                self.ttlMs = nil
            } else {
                self.ttlMs = try c.decode(Int.self, forKey: .ttlMs)
            }
        } else {
            self.ttlMs = 8000
        }
        self.priority = try c.decodeIfPresent(Priority.self, forKey: .priority) ?? .p2
        self.sourceEventId = try c.decodeIfPresent(String.self, forKey: .sourceEventId)
    }
}
