import Foundation

public enum HardwareNotchMode: String, Codable, Sendable, CaseIterable {
    case automatic
    case forceNotched = "force_notched"
    case forceFlat = "force_flat"
}

public struct ScreenGeometrySpec: Codable, Sendable, Equatable {
    public var screenId: String
    public var x: Double
    public var y: Double
    public var width: Double
    public var height: Double

    public init(
        screenId: String,
        x: Double,
        y: Double,
        width: Double,
        height: Double
    ) {
        self.screenId = screenId
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }

    private enum CodingKeys: String, CodingKey {
        case screenId = "screen_id"
        case x, y, width, height
    }
}

public struct FeedbackPrefs: Codable, Sendable, Equatable {
    public var audio: Bool
    public var audioName: String?
    public init(audio: Bool = false, audioName: String? = nil) {
        self.audio = audio
        self.audioName = audioName
    }
    private enum CodingKeys: String, CodingKey {
        case audio
        case audioName = "audio_name"
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.audio = try c.decodeIfPresent(Bool.self, forKey: .audio) ?? false
        self.audioName = try c.decodeIfPresent(String.self, forKey: .audioName)
    }
}

/// V10 island polish #7: user-tunable accent color override for
/// the progress capsule and chip color when phase is idle. ``system``
/// defers to the macOS accent color; the named presets give users
/// quick palette options without picking RGB themselves.
public enum IslandAccent: String, Codable, Sendable, CaseIterable {
    case system
    case blue
    case purple
    case pink
    case orange
    case green
    case mint
    case red
}

public struct TopSurfaceCustomization: Codable, Sendable, Equatable {
    public var specVersion: Int
    public var theme: String
    public var fontScale: Double
    public var buddyStyle: String
    public var showBuddy: Bool
    public var hardwareNotchMode: HardwareNotchMode
    public var screenGeometries: [ScreenGeometrySpec]
    public var hoverSpeed: Double
    public var feedback: FeedbackPrefs
    public var preferredScreenId: String?
    /// V10 island polish #7: accent color preset for chip / progress.
    public var accent: IslandAccent
    /// V10 island polish #8: render the progress capsule with a
    /// LinearGradient instead of a solid fill.
    public var useGradientProgress: Bool

    public init(
        specVersion: Int = BridgeProtocol.specVersion,
        theme: String = "system",
        fontScale: Double = 1.0,
        buddyStyle: String = "pixel",
        showBuddy: Bool = true,
        hardwareNotchMode: HardwareNotchMode = .automatic,
        screenGeometries: [ScreenGeometrySpec] = [],
        hoverSpeed: Double = 1.0,
        feedback: FeedbackPrefs = FeedbackPrefs(),
        preferredScreenId: String? = nil,
        accent: IslandAccent = .system,
        useGradientProgress: Bool = true
    ) {
        self.specVersion = specVersion
        self.theme = theme
        self.fontScale = fontScale
        self.buddyStyle = buddyStyle
        self.showBuddy = showBuddy
        self.hardwareNotchMode = hardwareNotchMode
        self.screenGeometries = screenGeometries
        self.hoverSpeed = hoverSpeed
        self.feedback = feedback
        self.preferredScreenId = preferredScreenId
        self.accent = accent
        self.useGradientProgress = useGradientProgress
    }

    private enum CodingKeys: String, CodingKey {
        case specVersion = "spec_version"
        case theme
        case fontScale = "font_scale"
        case buddyStyle = "buddy_style"
        case showBuddy = "show_buddy"
        case hardwareNotchMode = "hardware_notch_mode"
        case screenGeometries = "screen_geometries"
        case hoverSpeed = "hover_speed"
        case feedback
        case preferredScreenId = "preferred_screen_id"
        case accent
        case useGradientProgress = "use_gradient_progress"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.specVersion = try c.decodeIfPresent(Int.self, forKey: .specVersion)
            ?? BridgeProtocol.specVersion
        self.theme = try c.decodeIfPresent(String.self, forKey: .theme) ?? "system"
        self.fontScale = try c.decodeIfPresent(Double.self, forKey: .fontScale) ?? 1.0
        self.buddyStyle = try c.decodeIfPresent(String.self, forKey: .buddyStyle) ?? "pixel"
        self.showBuddy = try c.decodeIfPresent(Bool.self, forKey: .showBuddy) ?? true
        self.hardwareNotchMode = try c.decodeIfPresent(
            HardwareNotchMode.self,
            forKey: .hardwareNotchMode
        ) ?? .automatic
        self.screenGeometries = try c.decodeIfPresent(
            [ScreenGeometrySpec].self,
            forKey: .screenGeometries
        ) ?? []
        self.hoverSpeed = try c.decodeIfPresent(Double.self, forKey: .hoverSpeed) ?? 1.0
        self.feedback = try c.decodeIfPresent(FeedbackPrefs.self, forKey: .feedback)
            ?? FeedbackPrefs()
        self.preferredScreenId = try c.decodeIfPresent(String.self, forKey: .preferredScreenId)
        self.accent = try c.decodeIfPresent(IslandAccent.self, forKey: .accent) ?? .system
        self.useGradientProgress = try c.decodeIfPresent(
            Bool.self, forKey: .useGradientProgress
        ) ?? true
    }
}

public final class TopSurfaceCustomizationStore {
    public private(set) var current: TopSurfaceCustomization
    private var subscribers: [UUID: (TopSurfaceCustomization) -> Void] = [:]

    public init(initial: TopSurfaceCustomization = TopSurfaceCustomization()) {
        self.current = initial
    }

    @discardableResult
    public func apply(_ next: TopSurfaceCustomization) -> Bool {
        guard next != current else { return false }
        current = next
        for cb in subscribers.values {
            cb(next)
        }
        return true
    }

    @discardableResult
    public func subscribe(
        _ cb: @escaping (TopSurfaceCustomization) -> Void
    ) -> () -> Void {
        let id = UUID()
        subscribers[id] = cb
        return { [weak self] in
            self?.subscribers.removeValue(forKey: id)
        }
    }

    public var subscriberCount: Int { subscribers.count }
}
