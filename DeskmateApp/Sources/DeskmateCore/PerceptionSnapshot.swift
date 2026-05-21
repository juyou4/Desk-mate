import Foundation

/// Swift-side mirror of Python :class:`deskmate_agent.context.PerceptionSnapshot`
/// (V10 Phase 11b / L3-D1).
///
/// The Swift shell samples OS-level perception signals (focus, idle
/// duration, active window…) and diffs them; when something changes it
/// ships a :class:`BridgeEnvelope` of type ``perception`` whose payload
/// is this struct. Wire keys match the Python side's
/// ``_context_from_perception`` reader exactly.
public struct PerceptionSnapshot: Codable, Sendable, Equatable {
    public var userState: String
    public var focus: UserFocus
    /// The foreground app bundle id (e.g. ``com.apple.Safari``).
    public var app: String?
    /// The foreground window title, when readable.
    public var title: String?
    public var idleMs: Int
    /// Optional timestamp; Python defaults it to server receive time
    /// when omitted, so callers only populate it if they want to pin
    /// the moment on the Swift side.
    public var tsMs: Int?

    public init(
        userState: String = "idle",
        focus: UserFocus = .casual,
        app: String? = nil,
        title: String? = nil,
        idleMs: Int = 0,
        tsMs: Int? = nil
    ) {
        self.userState = userState
        self.focus = focus
        self.app = app
        self.title = title
        self.idleMs = idleMs
        self.tsMs = tsMs
    }

    private enum CodingKeys: String, CodingKey {
        case userState = "user_state"
        case focus
        case app
        case title
        case idleMs = "idle_ms"
        case tsMs = "ts_ms"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.userState = try c.decodeIfPresent(String.self, forKey: .userState) ?? "idle"
        self.focus = try c.decodeIfPresent(UserFocus.self, forKey: .focus) ?? .casual
        self.app = try c.decodeIfPresent(String.self, forKey: .app)
        self.title = try c.decodeIfPresent(String.self, forKey: .title)
        self.idleMs = try c.decodeIfPresent(Int.self, forKey: .idleMs) ?? 0
        self.tsMs = try c.decodeIfPresent(Int.self, forKey: .tsMs)
    }
}
