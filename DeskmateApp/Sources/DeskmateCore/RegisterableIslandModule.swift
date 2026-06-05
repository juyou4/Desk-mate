import Foundation

/// V10 island polish #10: wire-shape for an externally-registered
/// island module. Lets agents (kiro, codex, cursor, custom hooks)
/// declare their own compact pill template at runtime instead of
/// requiring per-agent code in the Swift menu bar app.
///
/// Wire format example:
///   {"id": "kiro.spec", "priority": 60,
///    "kind": "live_activity",
///    "activity_prefix": "kiro-spec-",
///    "title": "SPEC", "subtitle": "{detail}",
///    "image": "k.circle"}
///
/// Templating: the substring ``{detail}`` in ``subtitle`` is replaced
/// with the live ``IslandSurfaceState.detail`` at render time so the
/// module can show dynamic text without us re-rendering it from
/// Python on every state change.
public struct IslandModuleSpec: Codable, Sendable, Equatable {
    public var id: String
    public var priority: Int
    public var kind: String
    public var activityPrefix: String?
    public var title: String
    public var subtitle: String?
    public var image: String?

    public init(
        id: String,
        priority: Int = 50,
        kind: String,
        activityPrefix: String? = nil,
        title: String,
        subtitle: String? = nil,
        image: String? = nil
    ) {
        self.id = id
        self.priority = priority
        self.kind = kind
        self.activityPrefix = activityPrefix
        self.title = title
        self.subtitle = subtitle
        self.image = image
    }

    private enum CodingKeys: String, CodingKey {
        case id, priority, kind, title, subtitle, image
        case activityPrefix = "activity_prefix"
    }
}

/// Concrete IslandModule implementation built from a wire-spec.
/// The registry loads these dynamically when a `register_module`
/// intent arrives. Replacing an existing module by id is supported
/// (the registry's `register` already handles this).
public final class RegisteredIslandModule: IslandModule {
    public let id: String
    public let displayName: String
    public let defaultSide: IslandModuleSide = .leading
    public let defaultOrder = 50
    public let claimPriority: Int
    public let supportedKinds: Set<IslandSurfaceKind>

    private let activityPrefix: String?
    private let titleTemplate: String
    private let subtitleTemplate: String?
    private let imageName: String?

    public init(spec: IslandModuleSpec) {
        self.id = spec.id
        self.displayName = spec.id
        self.claimPriority = spec.priority
        self.supportedKinds = Self.parseKind(spec.kind)
        self.activityPrefix = spec.activityPrefix
        self.titleTemplate = spec.title
        self.subtitleTemplate = spec.subtitle
        self.imageName = spec.image
    }

    public func claims(state: IslandSurfaceState) -> Bool {
        guard supportedKinds.contains(state.kind) else { return false }
        if let prefix = activityPrefix, !prefix.isEmpty {
            return state.activityId?.hasPrefix(prefix) == true
        }
        return true
    }

    public func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        guard claims(state: state) else { return nil }
        return IslandModuleRenderDescriptor(
            title: substitute(titleTemplate, with: state),
            subtitle: subtitleTemplate.map { substitute($0, with: state) },
            badge: nil,
            systemImageName: imageName
        )
    }

    public func handle(_ action: InteractionAction) -> Bool {
        false
    }

    // MARK: - Helpers

    private func substitute(_ template: String, with state: IslandSurfaceState) -> String {
        var result = template
        if template.contains("{detail}") {
            result = result.replacingOccurrences(
                of: "{detail}",
                with: state.detail ?? ""
            )
        }
        if template.contains("{activity}") {
            result = result.replacingOccurrences(
                of: "{activity}",
                with: state.activityId ?? ""
            )
        }
        if template.contains("{session}") {
            result = result.replacingOccurrences(
                of: "{session}",
                with: state.sessionId ?? ""
            )
        }
        return result.trimmingCharacters(in: .whitespaces)
    }

    private static func parseKind(_ raw: String) -> Set<IslandSurfaceKind> {
        let normalized = raw.lowercased().trimmingCharacters(in: .whitespaces)
        let mapping: [String: IslandSurfaceKind] = [
            "live_activity": .liveActivity,
            "notification_card": .notificationCard,
            "session_list": .sessionList,
            "compact": .compact,
        ]
        if let single = mapping[normalized] {
            return [single]
        }
        // Comma-separated multi-kind
        let parts = normalized.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        let kinds = parts.compactMap { mapping[String($0)] }
        return Set(kinds)
    }
}
