import Foundation

public extension IslandModuleRegistry {
    static func deskmateDefaultModules() -> IslandModuleRegistry {
        IslandModuleRegistry(modules: [
            NotificationIslandModule(),
            SessionListIslandModule(),
            LiveActivityIslandModule(),
            IdleIslandModule(),
        ])
    }
}

public final class NotificationIslandModule: IslandModule {
    public let id = "deskmate.notification"
    public let displayName = "Notification"
    public let defaultSide: IslandModuleSide = .leading
    public let defaultOrder = 10
    public let claimPriority = 100
    public let supportedKinds: Set<IslandSurfaceKind> = [.notificationCard]

    public init() {}

    public func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        guard claims(state: state) else { return nil }
        let idText = (state.activityId ?? state.sessionId ?? "notice").lowercased()
        let title: String
        let image: String
        if idText.contains("reminder") {
            title = "REMIND"
            image = "bell.fill"
        } else if idText.contains("approval") || idText.contains("question") {
            title = "ASK"
            image = "exclamationmark.triangle.fill"
        } else {
            title = "NOTICE"
            image = "sparkle"
        }
        return IslandModuleRenderDescriptor(
            title: title,
            subtitle: _cleanSubtitle(state.detail) ?? "now",
            badge: "now",
            systemImageName: image
        )
    }
}

public final class LiveActivityIslandModule: IslandModule {
    public let id = "deskmate.live_activity"
    public let displayName = "Live Activity"
    public let defaultSide: IslandModuleSide = .leading
    public let defaultOrder = 20
    public let claimPriority = 80
    public let supportedKinds: Set<IslandSurfaceKind> = [.liveActivity]

    public init() {}

    public func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        guard claims(state: state) else { return nil }
        let activity = (state.activityId ?? "").lowercased()
        let title: String
        let image: String
        if activity.contains("build") {
            title = "BUILD"
            image = "hammer.fill"
        } else if activity.contains("coding") {
            title = "CODE"
            image = "curlybraces"
        } else {
            title = "LIVE"
            image = "bolt.fill"
        }
        return IslandModuleRenderDescriptor(
            title: title,
            subtitle: _cleanSubtitle(state.detail) ?? _cleanSubtitle(state.activityId),
            badge: nil,
            systemImageName: image
        )
    }
}

public final class SessionListIslandModule: IslandModule {
    public let id = "deskmate.sessions"
    public let displayName = "Sessions"
    public let defaultSide: IslandModuleSide = .leading
    public let defaultOrder = 30
    public let claimPriority = 70
    public let supportedKinds: Set<IslandSurfaceKind> = [.sessionList]

    public init() {}

    public func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        guard claims(state: state) else { return nil }
        return IslandModuleRenderDescriptor(
            title: "SESS",
            subtitle: _cleanSubtitle(state.detail) ?? "expanded",
            badge: nil,
            systemImageName: "list.bullet"
        )
    }
}

public final class IdleIslandModule: IslandModule {
    public let id = "deskmate.idle"
    public let displayName = "Idle"
    public let defaultSide: IslandModuleSide = .center
    public let defaultOrder = 100
    public let claimPriority = 1
    public let supportedKinds: Set<IslandSurfaceKind> = [.compact, .empty]

    public init() {}

    public func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        guard claims(state: state) else { return nil }
        return IslandModuleRenderDescriptor(
            title: "DM",
            subtitle: nil,
            badge: nil,
            systemImageName: nil
        )
    }
}

private func _cleanSubtitle(_ value: String?) -> String? {
    guard let text = value?.trimmingCharacters(in: .whitespacesAndNewlines),
          !text.isEmpty
    else { return nil }
    return text
}
