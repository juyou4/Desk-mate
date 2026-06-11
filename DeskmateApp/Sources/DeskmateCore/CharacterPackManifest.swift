import Foundation

/// Character pack manifest (V10 I4 / L1-D).
///
/// Image directories are treated as resources only; code must read through
/// this manifest so new packs can ship without code changes.
public struct CharacterPackManifest: Codable, Sendable, Equatable {
    public static let defaultRequiredStates: [String] = [
        "idle", "working", "thinking", "alert",
    ]
    public static let defaultFallbacks: [String: String] = [
        "running": "working",
        "review": "thinking",
        "waiting": "alert",
        "waving": "happy",
        "jumping": "happy",
        "failed": "alert",
        "dozing": "idle",
        "sleeping": "idle",
        "waking": "jumping",
        "drag": "running",
        "react-click": "waving",
        "petting": "waving",
        "editing": "running",
        "testing": "waiting",
        "success": "jumping",
        "error": "failed",
        "celebrating": "jumping",
        "notification": "waving",
        "walking": "running",
        "walking_left": "running-left",
        "walking_right": "running-right",
        "running_left": "running-left",
        "running_right": "running-right",
        "running-left": "walking",
        "running-right": "walking",
    ]

    public var specVersion: Int
    public var id: String
    public var displayName: String
    public var author: String?
    public var canvasSize: [Int]
    public var scale: Double
    public var palette: [String]
    public var avatar: CharacterAvatarConfig
    public var states: [String: StateFrames]
    public var requiredStates: [String]
    public var idleTransitions: [String: [IdleTransitionRule]]
    public var bubbleConfig: BubbleConfig
    public var accessoryAct: [AccessoryAction]
    public var fallbacks: [String: String]
    public var avatarSlots: [String: AnyJSONValue]
    public var capabilities: [String]

    public init(
        specVersion: Int = BridgeProtocol.specVersion,
        id: String,
        displayName: String,
        author: String? = nil,
        canvasSize: [Int] = [32, 32],
        scale: Double = 1,
        palette: [String] = [],
        avatar: CharacterAvatarConfig = CharacterAvatarConfig(),
        states: [String: StateFrames] = [:],
        requiredStates: [String] = CharacterPackManifest.defaultRequiredStates,
        idleTransitions: [String: [IdleTransitionRule]] = [:],
        bubbleConfig: BubbleConfig = BubbleConfig(),
        accessoryAct: [AccessoryAction] = [],
        fallbacks: [String: String] = CharacterPackManifest.defaultFallbacks,
        avatarSlots: [String: AnyJSONValue] = [:],
        capabilities: [String] = []
    ) {
        self.specVersion = specVersion
        self.id = id
        self.displayName = displayName
        self.author = author
        self.canvasSize = canvasSize
        self.scale = scale
        self.palette = palette
        self.avatar = avatar
        self.states = states
        self.requiredStates = requiredStates
        self.idleTransitions = idleTransitions
        self.bubbleConfig = bubbleConfig
        self.accessoryAct = accessoryAct
        self.fallbacks = fallbacks
        self.avatarSlots = avatarSlots
        self.capabilities = capabilities
    }

    /// States listed in ``requiredStates`` but missing from ``states``.
    public func missingRequiredStates() -> [String] {
        requiredStates.filter { states[$0] == nil }
    }

    /// Resolve a state name by walking ``fallbacks`` until a concrete entry
    /// in ``states`` is found. Returns ``nil`` when no fallback matches
    /// (including pathological cycles).
    public func resolveState(_ name: String) -> String? {
        var seen = Set<String>()
        var current: String? = name
        while let value = current, !seen.contains(value) {
            if states[value] != nil {
                return value
            }
            seen.insert(value)
            current = fallbacks[value]
        }
        return nil
    }

    private enum CodingKeys: String, CodingKey {
        case specVersion = "spec_version"
        case id
        case displayName = "display_name"
        case author
        case canvasSize = "canvas_size"
        case scale
        case palette
        case avatar
        case states
        case requiredStates = "required_states"
        case idleTransitions = "idle_transitions"
        case bubbleConfig = "bubble_config"
        case accessoryAct = "accessory_act"
        case fallbacks
        case avatarSlots = "avatar_slots"
        case capabilities
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.specVersion = try c.decodeIfPresent(Int.self, forKey: .specVersion)
            ?? BridgeProtocol.specVersion
        self.id = try c.decode(String.self, forKey: .id)
        self.displayName = try c.decode(String.self, forKey: .displayName)
        self.author = try c.decodeIfPresent(String.self, forKey: .author)
        self.canvasSize = try c.decodeIfPresent([Int].self, forKey: .canvasSize) ?? [32, 32]
        self.scale = try c.decodeIfPresent(Double.self, forKey: .scale) ?? 1
        self.palette = try c.decodeIfPresent([String].self, forKey: .palette) ?? []
        self.avatar = try c.decodeIfPresent(CharacterAvatarConfig.self, forKey: .avatar)
            ?? CharacterAvatarConfig()
        self.states = try c.decodeIfPresent([String: StateFrames].self, forKey: .states) ?? [:]
        self.requiredStates = try c.decodeIfPresent([String].self, forKey: .requiredStates)
            ?? CharacterPackManifest.defaultRequiredStates
        self.idleTransitions = try c.decodeIfPresent(
            [String: [IdleTransitionRule]].self, forKey: .idleTransitions
        ) ?? [:]
        self.bubbleConfig = try c.decodeIfPresent(BubbleConfig.self, forKey: .bubbleConfig)
            ?? BubbleConfig()
        self.accessoryAct = try c.decodeIfPresent([AccessoryAction].self, forKey: .accessoryAct)
            ?? []
        self.fallbacks = try c.decodeIfPresent([String: String].self, forKey: .fallbacks)
            ?? CharacterPackManifest.defaultFallbacks
        self.avatarSlots = try c.decodeIfPresent(
            [String: AnyJSONValue].self, forKey: .avatarSlots
        ) ?? [:]
        self.capabilities = try c.decodeIfPresent([String].self, forKey: .capabilities) ?? []
    }
}

public struct CharacterAvatarConfig: Codable, Sendable, Equatable {
    public var defaultStyle: String
    public var supportedStyles: [String]

    public init(defaultStyle: String = "pixel", supportedStyles: [String] = ["pixel"]) {
        self.defaultStyle = defaultStyle
        self.supportedStyles = supportedStyles
    }

    private enum CodingKeys: String, CodingKey {
        case defaultStyle = "default_style"
        case supportedStyles = "supported_styles"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.defaultStyle = try c.decodeIfPresent(String.self, forKey: .defaultStyle) ?? "pixel"
        self.supportedStyles = try c.decodeIfPresent([String].self, forKey: .supportedStyles)
            ?? ["pixel"]
    }
}

public struct StateFrames: Codable, Sendable, Equatable {
    public var fps: Int
    public var frames: [String]

    public init(fps: Int = 4, frames: [String] = []) {
        self.fps = fps
        self.frames = frames
    }

    private enum CodingKeys: String, CodingKey {
        case fps, frames
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.fps = try c.decodeIfPresent(Int.self, forKey: .fps) ?? 4
        self.frames = try c.decodeIfPresent([String].self, forKey: .frames) ?? []
    }
}

public struct IdleTransitionRule: Codable, Sendable, Equatable {
    public var to: String
    public var probability: Double

    public init(to: String, probability: Double = 0) {
        self.to = to
        self.probability = probability
    }

    private enum CodingKeys: String, CodingKey {
        case to, probability
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.to = try c.decode(String.self, forKey: .to)
        self.probability = try c.decodeIfPresent(Double.self, forKey: .probability) ?? 0
    }
}

public struct BubbleConfig: Codable, Sendable, Equatable {
    public var icon: String?
    public var sounds: [String: String]
    public var templates: [String: String]

    public init(
        icon: String? = nil,
        sounds: [String: String] = [:],
        templates: [String: String] = [:]
    ) {
        self.icon = icon
        self.sounds = sounds
        self.templates = templates
    }

    private enum CodingKeys: String, CodingKey {
        case icon, sounds, templates
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.icon = try c.decodeIfPresent(String.self, forKey: .icon)
        self.sounds = try c.decodeIfPresent([String: String].self, forKey: .sounds) ?? [:]
        self.templates = try c.decodeIfPresent([String: String].self, forKey: .templates) ?? [:]
    }
}

public struct AccessoryAction: Codable, Sendable, Equatable {
    public var name: String
    public var actList: [String]
    public var accList: [String]

    public init(name: String, actList: [String] = [], accList: [String] = []) {
        self.name = name
        self.actList = actList
        self.accList = accList
    }

    private enum CodingKeys: String, CodingKey {
        case name
        case actList = "act_list"
        case accList = "acc_list"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.name = try c.decode(String.self, forKey: .name)
        self.actList = try c.decodeIfPresent([String].self, forKey: .actList) ?? []
        self.accList = try c.decodeIfPresent([String].self, forKey: .accList) ?? []
    }
}
