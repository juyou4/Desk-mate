import Foundation

/// Island surface transitions (V10 L2-#7 / L1-E).
///
/// Five-state enum constrained by ``IslandSurfaceKind``. The reducer gates
/// incoming intents on the *priority of the event that produced them* so
/// a higher-priority surface can preempt a lower one but not the reverse
/// (V10 I5).
public struct IslandStateMachine: Equatable, Sendable {
    public enum Transition: String, Equatable, Sendable {
        case slideIn   // empty → non-empty
        case slideOut  // non-empty → empty
        case morph     // non-empty → non-empty (different kind)
        case none      // no visible change
    }

    public struct Effect: Equatable, Sendable {
        public let state: IslandSurfaceState
        public let priority: Priority
        public let transition: Transition
        public let changed: Bool
    }

    public enum Event: Equatable, Sendable {
        case present(
            kind: IslandSurfaceKind,
            sessionId: String?,
            activityId: String?,
            detail: String?,
            priority: Priority,
            tsMs: Int
        )
        /// Refresh the current activity's detail string without
        /// changing kind / priority / transition. Callers pass the
        /// same ``activityId`` they presented with; events targeting
        /// a different activity are dropped by the reducer.
        case update(activityId: String, detail: String?, tsMs: Int)
        case dismiss(id: String?, tsMs: Int)
        case userInteract(tsMs: Int)
        case tick(tsMs: Int)
    }

    public private(set) var surface: IslandSurfaceState
    public private(set) var priority: Priority
    public private(set) var lastTouchedMs: Int
    /// Priorities at or above this stay pinned; the auto-dismiss tick does
    /// not clear them. (Roadmap: expose per-kind policy.)
    public var pinnedPriorityCeiling: Priority
    public var autoDismissMs: Int

    public init(
        surface: IslandSurfaceState = IslandSurfaceState(kind: .empty),
        priority: Priority = .p3,
        lastTouchedMs: Int = 0,
        pinnedPriorityCeiling: Priority = .p1,
        autoDismissMs: Int = 10_000
    ) {
        self.surface = surface
        self.priority = priority
        self.lastTouchedMs = lastTouchedMs
        self.pinnedPriorityCeiling = pinnedPriorityCeiling
        self.autoDismissMs = autoDismissMs
    }

    @discardableResult
    public mutating func apply(_ event: Event) -> Effect {
        switch event {
        case let .present(kind, sessionId, activityId, detail, p, ts):
            return handlePresent(
                kind: kind,
                sessionId: sessionId,
                activityId: activityId,
                detail: detail,
                eventPriority: p,
                tsMs: ts
            )
        case let .update(activityId, detail, ts):
            return handleUpdate(
                activityId: activityId, detail: detail, tsMs: ts
            )
        case let .dismiss(id, ts):
            return handleDismiss(id: id, tsMs: ts)
        case let .userInteract(ts):
            lastTouchedMs = ts
            return noopEffect()
        case let .tick(ts):
            return handleTick(tsMs: ts)
        }
    }

    // MARK: - Handlers

    private mutating func handlePresent(
        kind: IslandSurfaceKind,
        sessionId: String?,
        activityId: String?,
        detail: String?,
        eventPriority: Priority,
        tsMs: Int
    ) -> Effect {
        // Priority gate: a lower-priority event may not replace a higher one.
        if priorityRank(eventPriority) > priorityRank(priority) {
            // "greater rank number" = lower priority
            return noopEffect()
        }
        let previous = surface
        let nextSurface = IslandSurfaceState(
            kind: kind,
            sessionId: sessionId,
            activityId: activityId,
            detail: detail
        )
        let transition = transition(from: previous.kind, to: kind)
        surface = nextSurface
        priority = eventPriority
        lastTouchedMs = tsMs
        return Effect(
            state: surface,
            priority: priority,
            transition: transition,
            changed: nextSurface != previous
        )
    }

    private mutating func handleUpdate(
        activityId: String, detail: String?, tsMs: Int
    ) -> Effect {
        guard surface.activityId == activityId else {
            return noopEffect()
        }
        let previous = surface
        // Phase 13-ii: update now actually mutates the detail slot so
        // the overlay can re-render the secondary label (window title,
        // progress text) in place. Kind / session / activity are
        // untouched — no morph animation is triggered.
        surface.detail = detail
        lastTouchedMs = tsMs
        return Effect(
            state: surface,
            priority: priority,
            transition: .none,
            changed: surface != previous
        )
    }

    private mutating func handleDismiss(id: String?, tsMs: Int) -> Effect {
        let matches: Bool = {
            guard let id else { return true }  // generic dismiss
            return id == surface.sessionId || id == surface.activityId
        }()
        guard matches, surface.kind != .empty else {
            return noopEffect()
        }
        let previous = surface
        surface = IslandSurfaceState(kind: .empty)
        priority = .p3
        lastTouchedMs = tsMs
        return Effect(
            state: surface,
            priority: priority,
            transition: .slideOut,
            changed: surface != previous
        )
    }

    private mutating func handleTick(tsMs: Int) -> Effect {
        guard surface.kind != .empty else { return noopEffect() }
        // Pinned kinds survive timeouts.
        if priorityRank(priority) <= priorityRank(pinnedPriorityCeiling) {
            return noopEffect()
        }
        if tsMs - lastTouchedMs < autoDismissMs {
            return noopEffect()
        }
        let previous = surface
        surface = IslandSurfaceState(kind: .empty)
        priority = .p3
        lastTouchedMs = tsMs
        return Effect(
            state: surface,
            priority: priority,
            transition: .slideOut,
            changed: surface != previous
        )
    }

    // MARK: - Utilities

    private func transition(
        from previous: IslandSurfaceKind, to next: IslandSurfaceKind
    ) -> Transition {
        switch (previous, next) {
        case (.empty, .empty): return .none
        case (.empty, _): return .slideIn
        case (_, .empty): return .slideOut
        case let (a, b) where a == b: return .none
        default: return .morph
        }
    }

    private func priorityRank(_ p: Priority) -> Int {
        switch p {
        case .p0: return 0
        case .p1: return 1
        case .p2: return 2
        case .p3: return 3
        }
    }

    private func noopEffect() -> Effect {
        Effect(state: surface, priority: priority, transition: .none, changed: false)
    }
}
