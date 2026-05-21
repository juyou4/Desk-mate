import Foundation

/// Observable class wrapper around the value-typed
/// :class:`IslandStateMachine` (V10 Phase 9 / L1-E).
///
/// Reducer logic (priority gating, auto-dismiss, morph transitions)
/// stays in the pure state machine. This class owns one canonical
/// instance, forwards events into it, and notifies subscribers
/// whenever the resulting :class:`IslandStateMachine.Effect` reports a
/// visible change.
public final class LiveIslandSurfaceStore {
    public typealias Clock = () -> Int

    public struct ChangeEvent: Equatable, Sendable {
        public let state: IslandSurfaceState
        public let priority: Priority
        public let transition: IslandStateMachine.Transition
    }

    private struct PendingTransient: Equatable {
        var surface: IslandSurfaceState
        var priority: Priority
        var ttlMs: Int
        var sequence: Int
    }

    private var machine: IslandStateMachine
    private let clock: Clock
    private var steadySurface: IslandSurfaceState?
    private var steadyPriority: Priority = .p3
    private var transientExpiresAtMs: Int?
    private var transientQueue: [PendingTransient] = []
    private var nextTransientSequence = 0
    private var subscribers: [UUID: (ChangeEvent) -> Void] = [:]

    public init(
        machine: IslandStateMachine = IslandStateMachine(),
        clock: @escaping Clock = LiveIslandSurfaceStore.defaultClock
    ) {
        self.machine = machine
        self.clock = clock
    }

    // MARK: - Reads

    public var surface: IslandSurfaceState { machine.surface }
    public var priority: Priority { machine.priority }
    public var lastTouchedMs: Int { machine.lastTouchedMs }
    public var isTransientActive: Bool { transientExpiresAtMs != nil }
    public var transientQueueCount: Int { transientQueue.count }

    public var debugSummary: String {
        let current = surfaceDebug(machine.surface)
        let steady = steadySurface.map(surfaceDebug) ?? "nil"
        let expires = transientExpiresAtMs.map(String.init) ?? "nil"
        return [
            "surface=\(current)",
            "priority=\(priority.rawValue)",
            "last_touched_ms=\(lastTouchedMs)",
            "transient_active=\(isTransientActive)",
            "transient_expires_at_ms=\(expires)",
            "transient_queue_count=\(transientQueue.count)",
            "steady=\(steady)",
            "steady_priority=\(steadyPriority.rawValue)",
        ].joined(separator: "\n")
    }

    // MARK: - Event injection

    public func present(
        kind: IslandSurfaceKind,
        sessionId: String? = nil,
        activityId: String? = nil,
        detail: String? = nil,
        priority: Priority = .p2
    ) {
        let event = IslandStateMachine.Event.present(
            kind: kind,
            sessionId: sessionId,
            activityId: activityId,
            detail: detail,
            priority: priority,
            tsMs: clock()
        )
        if shouldSneakPeek(kind: kind, priority: priority) {
            presentTransient(event, ttlMs: ttlMs(for: kind, priority: priority))
        } else {
            if transientExpiresAtMs != nil, priority != .p0 {
                guard shouldReplaceSteady(priority) else { return }
                steadySurface = IslandSurfaceState(
                    kind: kind,
                    sessionId: sessionId,
                    activityId: activityId,
                    detail: detail
                )
                steadyPriority = priority
                return
            }
            transientExpiresAtMs = nil
            steadySurface = nil
            transientQueue.removeAll()
            apply(event)
        }
    }

    public func update(activityId: String, detail: String? = nil) {
        if transientExpiresAtMs != nil,
           steadySurface?.activityId == activityId {
            steadySurface?.detail = detail
            return
        }
        apply(.update(activityId: activityId, detail: detail, tsMs: clock()))
    }

    public func dismiss(id: String? = nil) {
        let now = clock()
        if transientExpiresAtMs != nil {
            if id == nil {
                transientExpiresAtMs = nil
                steadySurface = nil
                transientQueue.removeAll()
                apply(.dismiss(id: nil, tsMs: now))
                return
            }
            if matches(id: id, surface: machine.surface) {
                finishCurrentTransient(tsMs: now)
                return
            }
            if matches(id: id, surface: steadySurface) {
                steadySurface = IslandSurfaceState(kind: .empty)
                steadyPriority = .p3
                return
            }
            transientQueue.removeAll { matches(id: id, surface: $0.surface) }
        }
        transientExpiresAtMs = nil
        steadySurface = nil
        transientQueue.removeAll()
        apply(.dismiss(id: id, tsMs: now))
    }

    public func noteUserInteract() {
        apply(.userInteract(tsMs: clock()))
    }

    public func tick() {
        let now = clock()
        if let expires = transientExpiresAtMs, now >= expires {
            finishCurrentTransient(tsMs: now)
            return
        }
        apply(.tick(tsMs: now))
    }

    /// Escape hatch for callers that already constructed a raw event —
    /// e.g. the dispatcher binding. Returns the reducer effect so
    /// diagnostics can log transitions.
    @discardableResult
    public func apply(_ event: IslandStateMachine.Event) -> IslandStateMachine.Effect {
        let effect = machine.apply(event)
        if effect.changed {
            let notice = ChangeEvent(
                state: effect.state,
                priority: effect.priority,
                transition: effect.transition
            )
            emit(notice)
        }
        return effect
    }

    // MARK: - Subscription

    @discardableResult
    public func subscribe(
        _ cb: @escaping (ChangeEvent) -> Void
    ) -> () -> Void {
        let id = UUID()
        subscribers[id] = cb
        return { [weak self] in
            self?.subscribers.removeValue(forKey: id)
        }
    }

    public var subscriberCount: Int { subscribers.count }

    // MARK: - Internals

    private func shouldSneakPeek(
        kind: IslandSurfaceKind,
        priority: Priority
    ) -> Bool {
        if priority == .p0 { return false }
        switch kind {
        case .notificationCard:
            return true
        case .compact, .empty, .liveActivity, .sessionList:
            return false
        }
    }

    private func ttlMs(for kind: IslandSurfaceKind, priority: Priority) -> Int {
        if priority == .p1 { return 2_800 }
        switch kind {
        case .notificationCard: return 2_400
        case .compact, .empty, .liveActivity, .sessionList: return 0
        }
    }

    private func presentTransient(
        _ event: IslandStateMachine.Event,
        ttlMs: Int
    ) {
        let pending = pendingTransient(from: event, ttlMs: ttlMs)
        if transientExpiresAtMs != nil {
            enqueueTransient(pending)
            return
        }
        if transientExpiresAtMs == nil {
            steadySurface = machine.surface
            steadyPriority = machine.priority
        }
        if !showTransient(pending, tsMs: clock()) {
            steadySurface = nil
            transientExpiresAtMs = nil
        }
    }

    private func finishCurrentTransient(tsMs: Int) {
        transientExpiresAtMs = nil
        if let next = popNextTransient() {
            restoreSteadyMachine(tsMs: tsMs)
            if showTransient(next, tsMs: tsMs) {
                return
            }
            finishCurrentTransient(tsMs: tsMs)
            return
        }
        emitSteadyRestore(tsMs: tsMs)
    }

    private func emitSteadyRestore(tsMs: Int) {
        guard let steadySurface else {
            apply(.dismiss(id: nil, tsMs: tsMs))
            return
        }
        self.steadySurface = nil
        let previous = machine.surface
        if steadySurface.kind == .empty {
            let effect = apply(.dismiss(id: nil, tsMs: tsMs))
            if !effect.changed, previous.kind != .empty {
                emit(ChangeEvent(
                    state: machine.surface,
                    priority: machine.priority,
                    transition: .slideOut
                ))
            }
            return
        }
        let pinned = machine.pinnedPriorityCeiling
        let autoDismiss = machine.autoDismissMs
        machine = IslandStateMachine(
            surface: steadySurface,
            priority: steadyPriority,
            lastTouchedMs: tsMs,
            pinnedPriorityCeiling: pinned,
            autoDismissMs: autoDismiss
        )
        if previous != steadySurface {
            emit(ChangeEvent(
                state: machine.surface,
                priority: machine.priority,
                transition: previous.kind == .empty ? .slideIn : .morph
            ))
        }
    }

    private func restoreSteadyMachine(tsMs: Int) {
        guard let steadySurface else { return }
        let pinned = machine.pinnedPriorityCeiling
        let autoDismiss = machine.autoDismissMs
        machine = IslandStateMachine(
            surface: steadySurface,
            priority: steadyPriority,
            lastTouchedMs: tsMs,
            pinnedPriorityCeiling: pinned,
            autoDismissMs: autoDismiss
        )
    }

    private func pendingTransient(
        from event: IslandStateMachine.Event,
        ttlMs: Int
    ) -> PendingTransient {
        guard case let .present(kind, sessionId, activityId, detail, priority, _) = event
        else {
            preconditionFailure("transient event must be present")
        }
        let pending = PendingTransient(
            surface: IslandSurfaceState(
                kind: kind,
                sessionId: sessionId,
                activityId: activityId,
                detail: detail
            ),
            priority: priority,
            ttlMs: ttlMs,
            sequence: nextTransientSequence
        )
        nextTransientSequence += 1
        return pending
    }

    private func showTransient(_ pending: PendingTransient, tsMs: Int) -> Bool {
        let effect = apply(.present(
            kind: pending.surface.kind,
            sessionId: pending.surface.sessionId,
            activityId: pending.surface.activityId,
            detail: pending.surface.detail,
            priority: pending.priority,
            tsMs: tsMs
        ))
        guard effect.changed else { return false }
        transientExpiresAtMs = tsMs + pending.ttlMs
        return true
    }

    private func enqueueTransient(_ pending: PendingTransient) {
        transientQueue.append(pending)
    }

    private func popNextTransient() -> PendingTransient? {
        guard !transientQueue.isEmpty else { return nil }
        var bestIndex = transientQueue.startIndex
        for idx in transientQueue.indices.dropFirst() {
            let candidate = transientQueue[idx]
            let best = transientQueue[bestIndex]
            if priorityRank(candidate.priority) < priorityRank(best.priority)
                || (
                    priorityRank(candidate.priority) == priorityRank(best.priority)
                    && candidate.sequence < best.sequence
                ) {
                bestIndex = idx
            }
        }
        return transientQueue.remove(at: bestIndex)
    }

    private func matches(id: String?, surface: IslandSurfaceState?) -> Bool {
        guard let id, let surface else { return false }
        return id == surface.sessionId || id == surface.activityId
    }

    private func priorityRank(_ p: Priority) -> Int {
        switch p {
        case .p0: return 0
        case .p1: return 1
        case .p2: return 2
        case .p3: return 3
        }
    }

    private func shouldReplaceSteady(_ priority: Priority) -> Bool {
        priorityRank(priority) <= priorityRank(steadyPriority)
    }

    private func surfaceDebug(_ surface: IslandSurfaceState) -> String {
        var parts = [surface.kind.rawValue]
        if let sessionId = surface.sessionId { parts.append("session=\(sessionId)") }
        if let activityId = surface.activityId { parts.append("activity=\(activityId)") }
        if let detail = surface.detail { parts.append("detail=\(detail)") }
        return parts.joined(separator: " ")
    }

    private func emit(_ notice: ChangeEvent) {
        for cb in subscribers.values { cb(notice) }
    }

    public static func defaultClock() -> Int {
        Int(Date().timeIntervalSince1970 * 1000)
    }
}
