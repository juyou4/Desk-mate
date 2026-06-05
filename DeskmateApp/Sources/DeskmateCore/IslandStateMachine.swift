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
            surfaceId: String?,
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
        /// R5.9: Notify the state machine that the degradation level
        /// has changed. If a SneakPeek is active and the new level
        /// crosses to >= 4, the peek collapses immediately.
        case degradationChanged(level: Int, tsMs: Int)
    }

    public private(set) var surface: IslandSurfaceState
    public private(set) var priority: Priority
    public private(set) var lastTouchedMs: Int
    /// Priorities at or above this stay pinned; the auto-dismiss tick does
    /// not clear them. (Roadmap: expose per-kind policy.)
    public var pinnedPriorityCeiling: Priority
    public var autoDismissMs: Int

    // MARK: - SneakPeek sub-state (R5)

    /// Whether the current surface is in the transient SneakPeek
    /// intermediate state. Non-pinned regardless of priority.
    public private(set) var isSneakPeek: Bool = false
    /// Absolute timestamp (ms) at which the SneakPeek auto-dismiss
    /// fires if no hover event arrives first.
    public private(set) var peekDeadlineMs: Int = 0
    /// Current degradation level from DegradationPolicy. Updated
    /// externally by the controller layer. Used to gate SneakPeek
    /// entry (R5.6) and to collapse an active peek (R5.9).
    public var degradationLevel: Int = 0

    public init(
        surface: IslandSurfaceState = IslandSurfaceState(kind: .empty),
        priority: Priority = .p3,
        lastTouchedMs: Int = 0,
        pinnedPriorityCeiling: Priority = .p1,
        autoDismissMs: Int = 10_000,
        degradationLevel: Int = 0
    ) {
        self.surface = surface
        self.priority = priority
        self.lastTouchedMs = lastTouchedMs
        self.pinnedPriorityCeiling = pinnedPriorityCeiling
        self.autoDismissMs = autoDismissMs
        self.degradationLevel = degradationLevel
    }

    @discardableResult
    public mutating func apply(_ event: Event) -> Effect {
        switch event {
        case let .present(kind, sessionId, activityId, detail, surfaceId, p, ts):
            return handlePresent(
                kind: kind,
                sessionId: sessionId,
                activityId: activityId,
                detail: detail,
                surfaceId: surfaceId,
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
        case let .degradationChanged(level, ts):
            return handleDegradationChanged(level: level, tsMs: ts)
        }
    }

    /// R5.5: Hover during SneakPeek promotes to full notification_card.
    /// Called by IslandWindowController when hover enters the hot rect
    /// while a transient SneakPeek is active.
    public mutating func promoteSneakPeek(tsMs: Int) -> Effect {
        guard isSneakPeek else { return noopEffect() }
        isSneakPeek = false
        peekDeadlineMs = 0
        lastTouchedMs = tsMs
        // The surface is already showing the notification_card;
        // keep it in place as the new steady state (now pinnable
        // by normal priority rules).
        return Effect(
            state: surface,
            priority: priority,
            transition: .none,
            changed: true
        )
    }

    // MARK: - Handlers

    private mutating func handlePresent(
        kind: IslandSurfaceKind,
        sessionId: String?,
        activityId: String?,
        detail: String?,
        surfaceId: String?,
        eventPriority: Priority,
        tsMs: Int
    ) -> Effect {
        // R5.8: If a new present arrives during an active SneakPeek,
        // preempt the existing peek and apply the new event's rules.
        let wasInSneakPeek = isSneakPeek
        if isSneakPeek {
            isSneakPeek = false
            peekDeadlineMs = 0
            // Reset priority so the new event isn't blocked by the
            // peek's non-pinned priority.
            priority = .p3
        }

        // Priority gate: a lower-priority event may not replace a higher one.
        // Exception: if we just preempted a SneakPeek, allow the new event
        // through regardless (R5.8).
        if !wasInSneakPeek && priorityRank(eventPriority) > priorityRank(priority) {
            // "greater rank number" = lower priority
            return noopEffect()
        }

        let previous = surface
        let nextSurface = IslandSurfaceState(
            kind: kind,
            sessionId: sessionId,
            activityId: activityId,
            detail: detail,
            surfaceId: surfaceId
        )

        // R5: SneakPeek entry decision
        if shouldEnterSneakPeek(kind: kind, eventPriority: eventPriority) {
            // R5.1: Enter SneakPeek for 1800ms (from tuning constant)
            isSneakPeek = true
            peekDeadlineMs = tsMs + Int(IslandAnimationTuning.default.sneakPeekDuration * 1000)
            surface = nextSurface
            priority = eventPriority
            lastTouchedMs = tsMs
            // R5.4: SneakPeek surfaces are non-pinned
            let transition = self.transition(from: previous.kind, to: kind)
            return Effect(
                state: surface,
                priority: priority,
                transition: transition,
                changed: nextSurface != previous
            )
        }

        // R5.3: P0 or degradation >= 4 → skip SneakPeek, pin directly
        let transition = self.transition(from: previous.kind, to: kind)
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
        // R3.4: If dismiss carries an id AND the current surface has a surfaceId,
        // require exact match. Mismatched dismiss is a no-op.
        if let dismissId = id,
           let currentSurfaceId = surface.surfaceId,
           !currentSurfaceId.isEmpty {
            guard dismissId == currentSurfaceId else {
                return noopEffect()  // R3.4: preserve current surface
            }
        }

        // R3.5: If no surface is displayed (empty), dismiss is a no-op
        // (existing logic handles this via the `surface.kind != .empty` guard below)

        // Fallback: generic dismiss matching on sessionId / activityId
        let matches: Bool = {
            guard let id else { return true }  // generic dismiss
            // If we already matched on surfaceId above, fall through to dismiss.
            if let currentSurfaceId = surface.surfaceId,
               !currentSurfaceId.isEmpty,
               id == currentSurfaceId {
                return true
            }
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
        // R5.2: SneakPeek timeout — collapse to compact/empty
        if isSneakPeek && tsMs >= peekDeadlineMs {
            isSneakPeek = false
            peekDeadlineMs = 0
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

    /// R5.1/R5.3/R5.6: Determine whether a present event should enter
    /// the SneakPeek intermediate state.
    private func shouldEnterSneakPeek(
        kind: IslandSurfaceKind,
        eventPriority: Priority
    ) -> Bool {
        // R5.3: P0 always skips SneakPeek
        if eventPriority == .p0 { return false }
        // R5.6: Degradation >= 4 universally skips SneakPeek
        if degradationLevel >= 4 { return false }
        // R5.1: Only notification_card surfaces enter SneakPeek
        switch kind {
        case .notificationCard:
            return true
        case .compact, .empty, .liveActivity, .sessionList:
            return false
        }
    }

    /// R5.9: When degradation crosses from < 4 to >= 4 while a SneakPeek
    /// is active, collapse immediately.
    private mutating func handleDegradationChanged(level: Int, tsMs: Int) -> Effect {
        let previousLevel = degradationLevel
        degradationLevel = level
        // R5.9: If we cross the threshold while peeking, collapse
        if isSneakPeek && previousLevel < 4 && level >= 4 {
            isSneakPeek = false
            peekDeadlineMs = 0
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
        return noopEffect()
    }

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
