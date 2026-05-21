import Foundation

public enum IslandModuleSide: String, Codable, Sendable, CaseIterable {
    case leading
    case center
    case trailing
}

public struct IslandModuleRenderDescriptor: Codable, Sendable, Equatable {
    public var title: String
    public var subtitle: String?
    public var badge: String?
    public var systemImageName: String?

    public init(
        title: String,
        subtitle: String? = nil,
        badge: String? = nil,
        systemImageName: String? = nil
    ) {
        self.title = title
        self.subtitle = subtitle
        self.badge = badge
        self.systemImageName = systemImageName
    }

    private enum CodingKeys: String, CodingKey {
        case title, subtitle, badge
        case systemImageName = "system_image_name"
    }
}

/// Pluggable island surface owner (V10 I5).
///
/// A module declares which :type:`IslandSurfaceKind` values it is willing to
/// render + how interaction actions are handled. The five core kinds
/// themselves are frozen (L1-E); modules add **behaviour** for those kinds,
/// not new enum cases.
///
/// Protocol is ``AnyObject``-bound so the registry stores references to a
/// single instance rather than cloning state on every register. Modules that
/// need to be thread-safe enforce that themselves — the registry does not
/// synchronise.
public protocol IslandModule: AnyObject {
    /// Unique string id. Re-registering with the same id replaces the
    /// previous instance (V10 I5: hot-reload friendly).
    var id: String { get }

    var displayName: String { get }
    var defaultSide: IslandModuleSide { get }
    var defaultOrder: Int { get }
    var isVisible: Bool { get }
    var preferredWidth: Double? { get }

    /// Higher values win when multiple modules claim the same state.
    var claimPriority: Int { get }

    /// The surface kinds this module is prepared to render / handle.
    var supportedKinds: Set<IslandSurfaceKind> { get }

    /// Decide whether this module claims the given surface state. Default
    /// implementation matches on ``supportedKinds``.
    func claims(state: IslandSurfaceState) -> Bool

    /// Handle a user interaction. Return ``true`` if the module took the
    /// action (registry will stop dispatching to later modules).
    func handle(_ action: InteractionAction) -> Bool

    /// Return a renderer-neutral descriptor for the active state.
    /// SwiftUI/AppKit views decide how to draw it; the protocol keeps
    /// module ownership and ordering independent from concrete UI.
    func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor?
}

extension IslandModule {
    public var displayName: String { id }
    public var defaultSide: IslandModuleSide { .center }
    public var defaultOrder: Int { 0 }
    public var isVisible: Bool { true }
    public var preferredWidth: Double? { nil }
    public var claimPriority: Int { 0 }

    public func claims(state: IslandSurfaceState) -> Bool {
        isVisible && supportedKinds.contains(state.kind)
    }

    public func handle(_ action: InteractionAction) -> Bool {
        false
    }

    public func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        nil
    }
}
