import Foundation

/// Generic observable wrapper around a list of wire rows (V10 Phase
/// 11d-iv). Used by the "list" surfaces — sessions, reminders,
/// approvals, tasks — that Python pushes inside ``state.snapshot`` and (later)
/// via delta intents.
///
/// The API is deliberately tiny: ``current`` for a snapshot read,
/// ``apply(_:)`` for a dedup-checked replace, ``subscribe(_:)`` for
/// change notifications. All mutations go through a private serial
/// queue so concurrent hydrator writes and subscriber reads stay
/// consistent even from off-thread callers.
public final class LiveListStore<Row: Equatable>: @unchecked Sendable {
    public typealias Subscriber = ([Row]) -> Void

    private let queue = DispatchQueue(label: "deskmate.live.list")
    private var _current: [Row]
    private var subscribers: [UUID: Subscriber] = [:]

    public init(initial: [Row] = []) { self._current = initial }

    public var current: [Row] { queue.sync { _current } }

    /// Replace the list contents. Returns ``true`` when the new list
    /// differed from the old one (and subscribers were notified),
    /// ``false`` when it was a no-op.
    @discardableResult
    public func apply(_ rows: [Row]) -> Bool {
        let toNotify: [Subscriber] = queue.sync {
            guard _current != rows else { return [] }
            _current = rows
            return Array(subscribers.values)
        }
        guard !toNotify.isEmpty else { return false }
        for cb in toNotify { cb(rows) }
        return true
    }

    /// Register an observer. Fires once immediately with the current
    /// list so new subscribers don't need a separate bootstrap path.
    @discardableResult
    public func subscribe(_ cb: @escaping Subscriber) -> () -> Void {
        let id = UUID()
        let snapshot: [Row] = queue.sync {
            subscribers[id] = cb
            return _current
        }
        cb(snapshot)
        return { [weak self] in
            self?.queue.async {
                self?.subscribers.removeValue(forKey: id)
            }
        }
    }
}

/// Observable list of active :class:`SessionRow` values. Hydrated
/// from ``state.snapshot.active_sessions``.
public final class LiveSessionListStore: @unchecked Sendable {
    private let inner = LiveListStore<SessionRow>()

    public init() {}

    public var current: [SessionRow] { inner.current }

    @discardableResult
    public func apply(_ rows: [SessionRow]) -> Bool { inner.apply(rows) }

    @discardableResult
    public func subscribe(
        _ cb: @escaping ([SessionRow]) -> Void
    ) -> () -> Void {
        inner.subscribe(cb)
    }
}

/// Observable list of pending :class:`ReminderRow` values. Hydrated
/// from ``state.snapshot.pending_reminders``.
public final class LivePendingRemindersStore: @unchecked Sendable {
    private let inner = LiveListStore<ReminderRow>()

    public init() {}

    public var current: [ReminderRow] { inner.current }

    @discardableResult
    public func apply(_ rows: [ReminderRow]) -> Bool { inner.apply(rows) }

    @discardableResult
    public func subscribe(
        _ cb: @escaping ([ReminderRow]) -> Void
    ) -> () -> Void {
        inner.subscribe(cb)
    }
}

/// Observable list of pending :class:`ApprovalRow` values. Hydrated
/// from ``state.snapshot.pending_approvals_detail``.
public final class LivePendingApprovalsStore: @unchecked Sendable {
    private let inner = LiveListStore<ApprovalRow>()

    public init() {}

    public var current: [ApprovalRow] { inner.current }

    @discardableResult
    public func apply(_ rows: [ApprovalRow]) -> Bool { inner.apply(rows) }

    @discardableResult
    public func subscribe(
        _ cb: @escaping ([ApprovalRow]) -> Void
    ) -> () -> Void {
        inner.subscribe(cb)
    }
}

/// Observable list of active durable :class:`TaskRow` values. Hydrated
/// from ``state.snapshot.active_tasks``.
public final class LiveActiveTasksStore: @unchecked Sendable {
    private let inner = LiveListStore<TaskRow>()

    public init() {}

    public var current: [TaskRow] { inner.current }

    @discardableResult
    public func apply(_ rows: [TaskRow]) -> Bool { inner.apply(rows) }

    @discardableResult
    public func subscribe(
        _ cb: @escaping ([TaskRow]) -> Void
    ) -> () -> Void {
        inner.subscribe(cb)
    }
}
