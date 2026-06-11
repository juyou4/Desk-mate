import Foundation

/// Headless composition of everything the macOS app needs at runtime
/// (V10 Phase 11c).
///
/// The shell owns one instance of each live store, one dispatcher, and
/// one reconnecting bridge client, wired together exactly the way a
/// SwiftUI / AppKit shell would do it. It exposes no UI — that lives
/// one layer up. This separation is deliberate: the shell is unit
/// testable (``Tests/DeskmateCoreTests``) and smoke-testable
/// (``Sources/DeskmateCoreSmoke``) because every moving part has an
/// injection seam.
///
/// Seams at a glance:
///
/// * ``Configuration.socketPath`` drives the production factory that
///   calls ``BridgeClient.connect(to:)`` on ``start()``.
/// * ``Configuration.clientFactory`` overrides the default factory.
///   Tests plug in a ``socketpair(2)`` factory here to drive the shell
///   end-to-end without touching the filesystem.
public final class DeskmateShell {

    // MARK: - Configuration

    public struct Configuration: Sendable {
        public var socketPath: String?
        public var bubbleMaxActive: Int
        public var bridgeBackoff: ReconnectingBridgeClient.Configuration
        public var clientFactory: ReconnectingBridgeClient.ClientFactory?
        public var clock: @Sendable () -> Int

        public init(
            socketPath: String? = nil,
            bubbleMaxActive: Int = 3,
            bridgeBackoff: ReconnectingBridgeClient.Configuration = .init(),
            clientFactory: ReconnectingBridgeClient.ClientFactory? = nil,
            clock: @escaping @Sendable () -> Int = {
                Int(Date().timeIntervalSince1970 * 1000)
            }
        ) {
            self.socketPath = socketPath
            self.bubbleMaxActive = bubbleMaxActive
            self.bridgeBackoff = bridgeBackoff
            self.clientFactory = clientFactory
            self.clock = clock
        }
    }

    // MARK: - Stored components

    public let configuration: Configuration

    /// Live mirror of :class:`DomainState`. Pet + Island + MenuBar
    /// should subscribe to this for reactive rendering.
    public let domainState: LiveDomainStateStore

    /// FIFO-within-priority queue of bubbles awaiting display.
    public let bubbleQueue: LiveBubbleQueue

    /// The single Island surface reducer. Subscribe for transition
    /// events to drive the notch overlay.
    public let islandSurface: LiveIslandSurfaceStore

    /// Active sessions, hydrated on every ``state.snapshot``. Useful
    /// for menu-bar / palette surfaces that want a live list view.
    public let sessionList: LiveSessionListStore

    /// Pending reminders — same story as ``sessionList``.
    public let pendingReminders: LivePendingRemindersStore

    /// Pending approvals (full row detail, not just the IDs echoed in
    /// ``DomainState.pendingApprovals``).
    public let pendingApprovals: LivePendingApprovalsStore

    /// Active durable user tasks, hydrated from
    /// ``state.snapshot.active_tasks``.
    public let activeTasks: LiveActiveTasksStore

    /// Routes incoming :class:`CompanionIntent` values into the above
    /// stores. Pre-wired with ``bindDomainState`` / ``bindBubbleQueue``
    /// / ``bindIslandSurface``.
    public let dispatcher: CompanionIntentDispatcher

    /// Consumes ``state.snapshot`` envelopes and hydrates the live
    /// stores so the Swift side reaches correct onboarding state the
    /// instant the bridge connects — no waiting for deltas.
    public let snapshotHydrator: SnapshotHydrator

    /// The self-healing bridge. Subscribes to its ``.intent`` envelopes
    /// via the dispatcher; non-intent envelopes (snapshot, ready, …)
    /// flow through :class:`SnapshotHydrator` / host-app handlers.
    public let bridge: ReconnectingBridgeClient

    // MARK: - Init

    public init(
        configuration: Configuration,
        callbackQueue: DispatchQueue = .main
    ) {
        self.configuration = configuration

        self.domainState = LiveDomainStateStore()
        self.bubbleQueue = LiveBubbleQueue(
            maxActive: configuration.bubbleMaxActive,
            clock: configuration.clock
        )
        self.islandSurface = LiveIslandSurfaceStore(clock: configuration.clock)
        self.sessionList = LiveSessionListStore()
        self.pendingReminders = LivePendingRemindersStore()
        self.pendingApprovals = LivePendingApprovalsStore()
        self.activeTasks = LiveActiveTasksStore()

        let dispatcher = CompanionIntentDispatcher()
        dispatcher.bindDomainState(to: self.domainState)
        dispatcher.bindBubbleQueue(to: self.bubbleQueue)
        dispatcher.bindIslandSurface(to: self.islandSurface)
        self.dispatcher = dispatcher

        self.snapshotHydrator = SnapshotHydrator(
            domainStore: self.domainState,
            sessionStore: self.sessionList,
            reminderStore: self.pendingReminders,
            approvalStore: self.pendingApprovals,
            taskStore: self.activeTasks,
            callbackQueue: callbackQueue
        )

        let factory: ReconnectingBridgeClient.ClientFactory =
            configuration.clientFactory
            ?? Self.productionFactory(
                socketPath: configuration.socketPath,
                callbackQueue: callbackQueue
            )
        self.bridge = ReconnectingBridgeClient(
            factory: factory,
            configuration: configuration.bridgeBackoff,
            callbackQueue: callbackQueue
        )
        dispatcher.bind(bridge: bridge)
        snapshotHydrator.bind(bridge: bridge)
    }

    // MARK: - Lifecycle

    public func start() { bridge.start() }
    public func stop() { bridge.stop() }

    // MARK: - Production factory

    /// Default client factory: open a Unix socket at ``socketPath`` and
    /// start the read loop. Throws ``BridgeClient.Error.noSocketPath``
    /// if ``socketPath`` is nil (tests must supply their own factory).
    static func productionFactory(
        socketPath: String?,
        callbackQueue: DispatchQueue
    ) -> ReconnectingBridgeClient.ClientFactory {
        return {
            guard let path = socketPath else {
                throw BridgeClient.Error.noSocketPath
            }
            let c = BridgeClient(
                configuration: .init(socketPath: path),
                callbackQueue: callbackQueue
            )
            try c.start()
            return c
        }
    }
}

// MARK: - EnvelopeSender conformance

extension DeskmateShell: EnvelopeSender {
    /// Forward raw envelopes to the reconnecting bridge. The typed
    /// helpers (``send(action:)``, ``sendUserMessage``,
    /// ``sendUserClickPet``, ``sendPerception``) come for free via the
    /// :class:`EnvelopeSender` protocol extension.
    public func send(_ envelope: BridgeEnvelope) throws {
        try bridge.send(envelope)
    }
}
