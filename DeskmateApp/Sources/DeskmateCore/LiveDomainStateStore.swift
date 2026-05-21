import Foundation

/// Swift-side authoritative mirror of the Python ``DomainState``
/// (V10 Phase 7 / L1-B).
///
/// Updated from two sources:
///   1. ``state.snapshot`` envelopes on reconnect (``apply(_:)``).
///   2. Live ``UPDATE_DOMAIN_STATE`` intents
///      (:class:`CompanionIntentDispatcher` ``bindDomainState(to:)``).
///
/// Consumers — Pet state machine, Island modules, menu bar badge —
/// subscribe to get notified of every change. Subscriptions are cheap
/// UUID-keyed closures; unsubscribing is safe from any thread *as long
/// as* the store itself is only mutated on a single thread (the main
/// actor in the shipped app; the test thread in unit tests).
public final class LiveDomainStateStore {
    public private(set) var current: DomainState
    private var subscribers: [UUID: (DomainState) -> Void] = [:]

    public init(initial: DomainState = DomainState()) {
        self.current = initial
    }

    /// Apply a new state. Identical state produces no notification, so
    /// redundant pushes from the Python projector don't fan out.
    @discardableResult
    public func apply(_ newState: DomainState) -> Bool {
        guard newState != current else { return false }
        current = newState
        for cb in subscribers.values {
            cb(newState)
        }
        return true
    }

    /// Subscribe to future changes. The callback is **not** invoked with
    /// the current state — subscribers read ``current`` if they need
    /// the initial value. Returns an unsubscribe closure.
    @discardableResult
    public func subscribe(
        _ cb: @escaping (DomainState) -> Void
    ) -> () -> Void {
        let id = UUID()
        subscribers[id] = cb
        return { [weak self] in
            self?.subscribers.removeValue(forKey: id)
        }
    }

    /// Visible for diagnostics / tests. Not part of the normal API.
    public var subscriberCount: Int { subscribers.count }
}
