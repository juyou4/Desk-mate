import Foundation

/// Self-healing wrapper around :class:`BridgeClient` (V10 Phase 11a / L3-D3).
///
/// Production reality: the agent process may not be running yet when
/// the macOS app launches, may crash, or may be bounced by the user
/// during a development build. The app UI must not care — it calls
/// :meth:`start()` once and the wrapper keeps trying to establish +
/// re-establish the socket link using exponential backoff with jitter.
///
/// Design shape:
///
/// * **Factory injection** for the inner :class:`BridgeClient`. The
///   factory must return an already-started client or throw. This keeps
///   all real-vs-test wiring in one configurable seam.
/// * **Serial ``workQueue``** guards state. All callbacks are hopped to
///   ``callbackQueue`` (defaults to main) so consumers can be MainActor
///   without extra ceremony.
/// * **Explicit :class:`State` machine** — ``stopped`` → ``connecting``
///   → ``connected`` / ``waitingForRetry`` → ``connecting`` … —
///   observable via :meth:`onStateChange(_:)` so UI badges can surface
///   "agent offline" cues.
public final class ReconnectingBridgeClient {

    // MARK: - Types

    public enum State: Equatable, Sendable {
        case stopped
        case connecting
        case connected
        case waitingForRetry(attempt: Int, delayMs: Int)
    }

    public struct Configuration: Sendable {
        public var initialBackoff: TimeInterval
        public var maxBackoff: TimeInterval
        public var multiplier: Double
        /// 0.0 means "no jitter"; 0.2 means "±20% of the current backoff".
        public var jitterFraction: Double

        public init(
            initialBackoff: TimeInterval = 0.2,
            maxBackoff: TimeInterval = 10.0,
            multiplier: Double = 2.0,
            jitterFraction: Double = 0.2
        ) {
            self.initialBackoff = initialBackoff
            self.maxBackoff = maxBackoff
            self.multiplier = multiplier
            self.jitterFraction = jitterFraction
        }
    }

    public typealias ClientFactory = @Sendable () throws -> BridgeClient

    // MARK: - Stored properties

    public let configuration: Configuration
    private let factory: ClientFactory
    private let callbackQueue: DispatchQueue
    private let workQueue = DispatchQueue(
        label: "deskmate.bridge.reconnecting", qos: .userInitiated
    )

    private var running: Bool = false
    private var current: BridgeClient?
    private var pendingRetry: DispatchWorkItem?
    private var attemptCount: Int = 0
    private var nextBackoff: TimeInterval = 0
    private var _state: State = .stopped

    private var envelopeHandlers: [UUID: (BridgeEnvelope) -> Void] = [:]
    private var envelopeUnsubs: [UUID: () -> Void] = [:]
    private var stateHandler: ((State) -> Void)?
    private var decodeErrorHandler: ((EnvelopeFraming.Error) -> Void)?

    public var state: State { workQueue.sync { _state } }

    // MARK: - Init

    public init(
        factory: @escaping ClientFactory,
        configuration: Configuration = .init(),
        callbackQueue: DispatchQueue = .main
    ) {
        self.factory = factory
        self.configuration = configuration
        self.callbackQueue = callbackQueue
    }

    // MARK: - Public API

    /// Begin the connect / reconnect loop. Idempotent — a second call
    /// while already running is a no-op.
    public func start() {
        workQueue.async { [weak self] in
            guard let self else { return }
            guard !self.running else { return }
            self.running = true
            self.nextBackoff = self.configuration.initialBackoff
            self.attemptCount = 0
            self.attemptConnectLocked()
        }
    }

    /// Tear everything down: cancel pending retries, stop the current
    /// client, transition to ``.stopped``. Safe to call multiple times.
    public func stop() {
        workQueue.sync {
            self.running = false
            self.pendingRetry?.cancel()
            self.pendingRetry = nil
            self.current?.stop()
            self.current = nil
            self.setStateLocked(.stopped)
        }
    }

    /// Forward an envelope through the active client. Throws
    /// :attr:`BridgeClient.Error.notConnected` when currently in any
    /// state other than ``.connected``.
    public func send(_ envelope: BridgeEnvelope) throws {
        try workQueue.sync {
            guard let c = current else {
                throw BridgeClient.Error.notConnected
            }
            try c.send(envelope)
        }
    }

    /// Register a handler that survives reconnects — the wrapper
    /// installs it on every fresh inner :class:`BridgeClient` produced
    /// by the factory. Multiple subscribers are supported.
    @discardableResult
    public func onEnvelope(
        _ cb: @escaping (BridgeEnvelope) -> Void
    ) -> () -> Void {
        let id = UUID()
        workQueue.sync {
            envelopeHandlers[id] = cb
            if let client = current {
                envelopeUnsubs[id] = client.onEnvelope(cb)
            }
        }
        return { [weak self] in
            self?.workQueue.async {
                self?.envelopeHandlers.removeValue(forKey: id)
                if let unsub = self?.envelopeUnsubs.removeValue(forKey: id) {
                    unsub()
                }
            }
        }
    }

    public func onStateChange(_ cb: @escaping (State) -> Void) {
        workQueue.sync { stateHandler = cb }
    }

    public func onDecodeError(
        _ cb: @escaping (EnvelopeFraming.Error) -> Void
    ) {
        workQueue.sync {
            decodeErrorHandler = cb
            current?.onDecodeError(cb)
        }
    }

    // MARK: - Internals (workQueue)

    private func attemptConnectLocked() {
        guard running else { return }
        setStateLocked(.connecting)
        attemptCount += 1
        do {
            let client = try factory()
            // Replay every observer onto the fresh inner client and
            // remember the per-id unsub so ``handleClientStateChangeLocked``
            // can drop them when the client dies.
            envelopeUnsubs.removeAll()
            for (id, cb) in envelopeHandlers {
                envelopeUnsubs[id] = client.onEnvelope(cb)
            }
            if let cb = decodeErrorHandler { client.onDecodeError(cb) }
            client.onStateChange { [weak self] state in
                self?.workQueue.async {
                    self?.handleClientStateChangeLocked(state)
                }
            }
            current = client
            nextBackoff = configuration.initialBackoff
            attemptCount = 0
            setStateLocked(.connected)
        } catch {
            scheduleRetryLocked()
        }
    }

    private func handleClientStateChangeLocked(_ state: BridgeClient.State) {
        guard running else { return }
        // Ignore the inner .connected echo — attemptConnectLocked
        // already fired our own .connected state.
        switch state {
        case .connected:
            return
        case .disconnected, .failed:
            // The inner client has already torn its socket down by the
            // time it reports this. Drop the reference + plan a retry.
            current = nil
            envelopeUnsubs.removeAll()
            scheduleRetryLocked()
        }
    }

    private func scheduleRetryLocked() {
        guard running else { return }
        let base = nextBackoff
        let fraction = configuration.jitterFraction
        let jitterRange = base * fraction
        let offset = jitterRange > 0
            ? Double.random(in: -jitterRange...jitterRange)
            : 0
        let delay = max(0, base + offset)
        let delayMs = Int(delay * 1000)
        setStateLocked(.waitingForRetry(
            attempt: attemptCount, delayMs: delayMs
        ))
        let task = DispatchWorkItem { [weak self] in
            self?.attemptConnectLocked()
        }
        pendingRetry = task
        workQueue.asyncAfter(deadline: .now() + delay, execute: task)
        nextBackoff = min(
            configuration.maxBackoff,
            nextBackoff * configuration.multiplier
        )
    }

    private func setStateLocked(_ s: State) {
        guard _state != s else { return }
        _state = s
        let handler = stateHandler
        callbackQueue.async { handler?(s) }
    }
}

extension ReconnectingBridgeClient: EnvelopeReceiver {}
