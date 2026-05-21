import Foundation

/// Hydrates the Swift runtime from a ``state.snapshot`` envelope
/// (V10 Phase 11d-iii / L3-D2).
///
/// The Python agent publishes a ``state.snapshot`` on every new bridge
/// connection. It carries ``domain_state``, ``active_sessions``,
/// ``pending_reminders``, ``pending_approvals_detail`` so the Swift
/// side can reach a correct onboarding view without waiting for delta
/// intents to arrive. This class subscribes to a
/// :class:`EnvelopeReceiver`, filters for ``.stateSnapshot`` envelopes,
/// and applies the embedded :class:`DomainState` to a live store.
///
/// Additional payload fields are surfaced via :meth:`onSnapshot(_:)`
/// as a raw :type:`AnyJSONValue` dictionary so callers can wire
/// session / approval / reminder stores incrementally without
/// requiring all of them to exist today.
public final class SnapshotHydrator {
    public typealias RawSnapshotCallback = ([String: AnyJSONValue]) -> Void

    private let domainStore: LiveDomainStateStore
    private let sessionStore: LiveSessionListStore?
    private let reminderStore: LivePendingRemindersStore?
    private let approvalStore: LivePendingApprovalsStore?
    private let callbackQueue: DispatchQueue
    private let queue = DispatchQueue(label: "deskmate.snapshot.hydrator")
    private var onSnapshotSubs: [UUID: RawSnapshotCallback] = [:]
    private var onDecodeErrorHandler: ((Error) -> Void)?
    private var bridgeUnsub: (() -> Void)?

    public init(
        domainStore: LiveDomainStateStore,
        sessionStore: LiveSessionListStore? = nil,
        reminderStore: LivePendingRemindersStore? = nil,
        approvalStore: LivePendingApprovalsStore? = nil,
        callbackQueue: DispatchQueue = .main
    ) {
        self.domainStore = domainStore
        self.sessionStore = sessionStore
        self.reminderStore = reminderStore
        self.approvalStore = approvalStore
        self.callbackQueue = callbackQueue
    }

    // MARK: - Binding

    /// Subscribe to ``bridge``. Returns immediately; the handler stays
    /// live until :meth:`stop()` is called or the returned unsub is
    /// invoked.
    public func bind(bridge: EnvelopeReceiver) {
        queue.sync {
            self.bridgeUnsub?()
            self.bridgeUnsub = bridge.onEnvelope { [weak self] env in
                self?.handle(env)
            }
        }
    }

    public func stop() {
        queue.sync {
            self.bridgeUnsub?()
            self.bridgeUnsub = nil
        }
    }

    // MARK: - Public subscription API

    @discardableResult
    public func onSnapshot(
        _ cb: @escaping RawSnapshotCallback
    ) -> () -> Void {
        let id = UUID()
        queue.sync { onSnapshotSubs[id] = cb }
        return { [weak self] in
            self?.queue.async {
                self?.onSnapshotSubs.removeValue(forKey: id)
            }
        }
    }

    public func onDecodeError(_ cb: @escaping (Error) -> Void) {
        queue.sync { onDecodeErrorHandler = cb }
    }

    // MARK: - Envelope handler

    /// Exposed for tests — normally called via the bridge subscription.
    public func handle(_ envelope: BridgeEnvelope) {
        guard envelope.type == .stateSnapshot else { return }

        // --- DomainState --------------------------------------------------
        decodeAndApply(
            field: "domain_state",
            in: envelope.payload,
            as: DomainState.self
        ) { [domainStore] state in
            domainStore.apply(state)
        }

        // --- Active sessions list ----------------------------------------
        if let store = sessionStore {
            decodeAndApply(
                field: "active_sessions",
                in: envelope.payload,
                as: [SessionRow].self
            ) { rows in
                store.apply(rows)
            }
        }

        // --- Pending reminders list --------------------------------------
        if let store = reminderStore {
            decodeAndApply(
                field: "pending_reminders",
                in: envelope.payload,
                as: [ReminderRow].self
            ) { rows in
                store.apply(rows)
            }
        }

        // --- Pending approvals detail list -------------------------------
        if let store = approvalStore {
            decodeAndApply(
                field: "pending_approvals_detail",
                in: envelope.payload,
                as: [ApprovalRow].self
            ) { rows in
                store.apply(rows)
            }
        }

        // Notify raw-payload subscribers on the caller's queue so they
        // can feed additional stores from the same envelope without
        // requiring a separate bridge subscription.
        let subs = queue.sync { Array(onSnapshotSubs.values) }
        let payload = envelope.payload
        callbackQueue.async {
            for cb in subs { cb(payload) }
        }
    }

    // MARK: - Private

    private func decodeAndApply<T: Decodable>(
        field: String,
        in payload: [String: AnyJSONValue],
        as _: T.Type,
        _ apply: (T) -> Void
    ) {
        guard let value = payload[field] else { return }
        do {
            let data = try JSONEncoder().encode(value)
            let decoded = try JSONDecoder().decode(T.self, from: data)
            apply(decoded)
        } catch {
            let errorHandler = queue.sync { onDecodeErrorHandler }
            callbackQueue.async { errorHandler?(error) }
        }
    }
}
