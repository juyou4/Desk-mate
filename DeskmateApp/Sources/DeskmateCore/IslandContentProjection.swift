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
    case task(TaskRow)
    case reminder(ReminderRow)
    case notification(state: IslandSurfaceState)
}

public enum IslandStatus: Equatable, Sendable {
    case closed
    case opened
}

public enum IslandContentProjection {
    /// Priority order, highest wins:
    /// approval > build live activity > notification > visible sessions >
    /// active task > idle.
    public static func compute(
        islandState: IslandSurfaceState?,
        sessions: [SessionRow],
        approvals: [ApprovalRow],
        reminders: [ReminderRow] = [],
        tasks: [TaskRow] = [],
        nowMs: Int = Int(Date().timeIntervalSince1970 * 1000),
        showClosedAfterMs: Int? = 5 * 60 * 1000
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

        let visibleReminders = ReminderListAdapter(maxRows: 3)
            .display(reminders: reminders, nowMs: nowMs)
        if let urgentReminder = visibleReminders.first(where: {
            $0.status == .fired || ($0.status == .pending && $0.dueAtMs <= nowMs)
        }) {
            return .reminder(urgentReminder)
        }

        let visible = SessionListAdapter(maxRows: 5, showClosedAfterMs: showClosedAfterMs)
            .display(sessions: sessions, nowMs: nowMs)
        if visible.count >= 2 {
            let focus = visible.first { $0.needsUserAction } ?? visible.first
            return .multiSession(sessions: visible, focus: focus)
        }
        if let session = visible.first {
            return .session(session)
        }

        if let task = tasks.first(where: { $0.status == .inProgress })
            ?? tasks.first(where: { $0.status == .open }) {
            return .task(task)
        }

        if let nextReminder = visibleReminders.first(where: { $0.status == .pending }) {
            return .reminder(nextReminder)
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
        case .idle, .build, .task, .reminder, .notification:
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

    public var activeTask: TaskRow? {
        if case .task(let task) = self { return task }
        return nil
    }

    public var activeReminder: ReminderRow? {
        if case .reminder(let reminder) = self { return reminder }
        return nil
    }
}
