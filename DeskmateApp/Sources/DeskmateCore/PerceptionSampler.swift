import Foundation

/// Polls OS signals into :class:`PerceptionSnapshot` deltas and pushes
/// them through an ``EnvelopeSender`` (V10 Phase 11d-i / L3-D1).
///
/// The sampler is designed to be *parsimonious*: it only sends when the
/// perception actually changed, plus an occasional heartbeat so Python
/// knows the pipe is still live. Callers inject pure closures for the
/// idle duration and frontmost-app reads, which makes this class
/// testable without AppKit.
///
/// Typical production wiring (see ``DefaultPerceptionProviders``):
///
/// ```swift
/// let sampler = PerceptionSampler(
///     sender: shell,
///     configuration: .init(
///         idleProvider: DefaultPerceptionProviders.idleSeconds,
///         frontmostAppProvider: DefaultPerceptionProviders.frontmostApp
///     )
/// )
/// sampler.start()
/// ```
public final class PerceptionSampler {
    public typealias IdleSecondsProvider = () -> TimeInterval
    public typealias FrontmostAppProvider = () -> FrontmostApp
    public typealias FocusInferrer = (TimeInterval) -> UserFocus
    public typealias Clock = () -> Int

    public struct FrontmostApp: Equatable, Sendable {
        public var bundleId: String?
        /// Display title. Defaults to :attr:`windowTitle` when
        /// Accessibility permission yields a frontmost window title;
        /// otherwise the app's localized name.
        public var title: String?
        /// AX-read window title (V10 Phase 13-ii). ``nil`` when AX is
        /// not trusted or the frontmost app has no focused window.
        /// This is the honest signal — tracking code that wants to
        /// distinguish "real file name" from "just the app name"
        /// should read this field instead of :attr:`title`.
        public var windowTitle: String?
        public init(
            bundleId: String? = nil,
            title: String? = nil,
            windowTitle: String? = nil
        ) {
            self.bundleId = bundleId
            self.title = title
            self.windowTitle = windowTitle
        }
        public static let none = FrontmostApp()
    }

    public struct Configuration {
        public var tickInterval: TimeInterval
        /// Minimum elapsed real time before we send a heartbeat even
        /// when the snapshot didn't change.
        public var heartbeatInterval: TimeInterval
        /// Crossover from "active" → "idle" for ``user_state``.
        public var idleSecondsForIdleState: TimeInterval
        public var idleProvider: IdleSecondsProvider
        public var frontmostAppProvider: FrontmostAppProvider
        public var focusInferrer: FocusInferrer
        public var clock: Clock

        public init(
            tickInterval: TimeInterval = 1.0,
            heartbeatInterval: TimeInterval = 30.0,
            idleSecondsForIdleState: TimeInterval = 30.0,
            idleProvider: @escaping IdleSecondsProvider,
            frontmostAppProvider: @escaping FrontmostAppProvider,
            focusInferrer: @escaping FocusInferrer = PerceptionSampler.defaultFocusInferrer,
            clock: @escaping Clock = {
                Int(Date().timeIntervalSince1970 * 1000)
            }
        ) {
            self.tickInterval = tickInterval
            self.heartbeatInterval = heartbeatInterval
            self.idleSecondsForIdleState = idleSecondsForIdleState
            self.idleProvider = idleProvider
            self.frontmostAppProvider = frontmostAppProvider
            self.focusInferrer = focusInferrer
            self.clock = clock
        }
    }

    // MARK: - Stored state

    private let sender: EnvelopeSender
    private var configuration: Configuration
    private let queue = DispatchQueue(label: "deskmate.perception.sampler")
    private var timer: DispatchSourceTimer?
    private var lastSent: PerceptionSnapshot?
    private var lastSentAtMs: Int = 0
    private var paused: Bool = false
    private(set) public var sentCount: Int = 0

    // MARK: - Init

    public init(
        sender: EnvelopeSender,
        configuration: Configuration
    ) {
        self.sender = sender
        self.configuration = configuration
    }

    // MARK: - Lifecycle

    public func start() {
        queue.sync {
            guard timer == nil else { return }
            let src = DispatchSource.makeTimerSource(queue: queue)
            src.schedule(
                deadline: .now(),
                repeating: configuration.tickInterval
            )
            src.setEventHandler { [weak self] in self?.tickLocked() }
            timer = src
            src.resume()
        }
    }

    public func stop() {
        queue.sync {
            timer?.cancel()
            timer = nil
        }
    }

    /// Fire one sampling pass synchronously. Tests use this to drive the
    /// sampler deterministically without a real timer.
    public func tick() {
        queue.sync { tickLocked() }
    }

    /// Pause or resume sampling without tearing down the timer. Used
    /// by the app shell when macOS reports display/system sleep so
    /// A7 does not keep polling ``NSWorkspace`` while no pixels can
    /// be shown. The dedupe cursor is preserved; on resume the next
    /// changed snapshot or heartbeat flows normally.
    public func setPaused(_ value: Bool) {
        queue.sync { paused = value }
    }

    public var isPaused: Bool {
        queue.sync { paused }
    }

    // MARK: - Internals (queue)

    private func tickLocked() {
        guard !paused else { return }
        let snapshot = buildSnapshotLocked()
        guard shouldSendLocked(snapshot) else { return }
        do {
            try sender.sendPerception(snapshot)
            lastSent = snapshot
            lastSentAtMs = configuration.clock()
            sentCount += 1
        } catch {
            // Transport hiccup — keep lastSent as is so the next tick
            // retries delivering the same state (don't advance the
            // dedupe cursor on failure).
        }
    }

    private func buildSnapshotLocked() -> PerceptionSnapshot {
        let idleSec = configuration.idleProvider()
        let app = configuration.frontmostAppProvider()
        let focus = configuration.focusInferrer(idleSec)
        let state = idleSec >= configuration.idleSecondsForIdleState
            ? "idle" : "active"
        // Phase 13-ii: prefer the AX window title (honest signal)
        // over the display-fallback ``title``. Python's
        // ``PerceptionSnapshot.window_title`` carries it verbatim.
        return PerceptionSnapshot(
            userState: state,
            focus: focus,
            app: app.bundleId,
            title: app.windowTitle ?? app.title,
            idleMs: Int(idleSec * 1000),
            tsMs: configuration.clock()
        )
    }

    private func shouldSendLocked(_ next: PerceptionSnapshot) -> Bool {
        guard let last = lastSent else { return true }
        if !Self.perceptuallyEqual(last, next) { return true }
        let elapsedMs = configuration.clock() - lastSentAtMs
        let heartbeatMs = Int(configuration.heartbeatInterval * 1000)
        return elapsedMs >= heartbeatMs
    }

    /// Two snapshots are perceptually equal when the *stable* fields
    /// match. ``idleMs`` and ``tsMs`` change every tick by design and
    /// are excluded from the comparison.
    static func perceptuallyEqual(
        _ a: PerceptionSnapshot, _ b: PerceptionSnapshot
    ) -> Bool {
        a.userState == b.userState
            && a.focus == b.focus
            && a.app == b.app
            && a.title == b.title
    }

    // MARK: - Default focus curve

    /// Maps idle seconds to :class:`UserFocus`. Heuristic matches V10
    /// L1-B's default "focused if just-active; idle_back after a
    /// multi-minute pause".
    public static func defaultFocusInferrer(
        idleSeconds: TimeInterval
    ) -> UserFocus {
        if idleSeconds < 10 { return .focused }
        if idleSeconds < 120 { return .casual }
        return .idleBack
    }
}
