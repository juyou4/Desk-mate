import Foundation

/// Closed state-space for what the island should present in compact /
/// expanded UI. Kept in Core so priority ordering is testable without
/// importing the AppKit menu-bar target.
public enum IslandContent: Equatable, Sendable {
    case idle
    case build(activityId: String, detail: String?, progress: Double?, isDone: Bool, isFailed: Bool)
    case session(SessionRow)
    case multiSession(sessions: [SessionRow], focus: SessionRow?)
    case approval(session: SessionRow?, approval: ApprovalRow)
    case notification(state: IslandSurfaceState)
}

public enum IslandStatus: Equatable, Sendable {
    case closed
    case opened
}

public enum IslandContentProjection {
    /// Priority order, highest wins:
    /// approval > build live activity > notification > multi-session >
    /// single session > idle.
    public static func compute(
        islandState: IslandSurfaceState?,
        sessions: [SessionRow],
        approvals: [ApprovalRow]
    ) -> IslandContent {
        if let approval = approvals.first {
            let session = sessions.first { $0.sessionId == approval.sessionId }
            return .approval(session: session, approval: approval)
        }

        if let state = islandState,
           state.kind == .liveActivity,
           let activityId = state.activityId,
           activityId.lowercased().hasPrefix("build-") {
            let detail = state.detail
            let isDone = (detail?.contains("\u{2705}") ?? false)
                || (detail?.contains("\u{274C}") ?? false)
            let isFailed = detail?.contains("\u{274C}") ?? false
            return .build(
                activityId: activityId,
                detail: detail,
                progress: state.progress,
                isDone: isDone,
                isFailed: isFailed
            )
        }

        if let state = islandState, state.kind == .notificationCard {
            return .notification(state: state)
        }

        let active = sessions.filter { $0.state != .closed }
        if active.count >= 2 {
            let focus = active.first { $0.needsUserAction } ?? active.first
            return .multiSession(sessions: active, focus: focus)
        }
        if let session = active.first {
            return .session(session)
        }

        return .idle
    }
}

extension IslandContent {
    public var focusedSession: SessionRow? {
        switch self {
        case .session(let session):
            return session
        case .multiSession(_, let focus):
            return focus
        case .approval(let session, _):
            return session
        case .idle, .build, .notification:
            return nil
        }
    }

    public var isApproval: Bool {
        if case .approval = self { return true }
        return false
    }

    public var isBuild: Bool {
        if case .build = self { return true }
        return false
    }

    public var isBuildDone: Bool {
        if case .build(_, _, _, let isDone, _) = self { return isDone }
        return false
    }

    public var isMultiSession: Bool {
        if case .multiSession = self { return true }
        return false
    }
}
