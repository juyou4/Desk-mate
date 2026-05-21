import Foundation

/// Pure-logic collector for V10 §3.1 row 6 (wake-to-first-frame) and
/// row 8 (frame drop ratio) Swift-side hard budgets.
///
/// The collector does **not** subscribe to ``NSWorkspace`` or drive a
/// ``CVDisplayLink`` itself — those bindings belong to a follow-up
/// integration layer that wraps this type. Keeping the logic
/// time-source-agnostic mirrors :class:`PetFrameAnimator` and lets the
/// XCTest suite verify every transition without spinning up the OS
/// notification center.
///
/// ## Wake budget
///
/// 1. The integration layer observes ``NSWorkspace.didWakeNotification``
///    and calls :func:`recordWake(at:)` with ``CACurrentMediaTime()``.
/// 2. The first SwiftUI render that follows the wake calls
///    :func:`markFirstFrame(at:)`. The elapsed seconds are latched
///    into :attr:`PerfMetricsSnapshot.lastWakeSeconds` and the
///    pending-wake state is cleared.
///
/// First-app-launch — no prior wake — is excluded by design: only
/// system-sleep round-trips contribute.
///
/// ## Frame drop budget
///
/// Every display tick the integration layer calls
/// :func:`recordFrameTick(at:expectedPeriod:)`. A tick whose interval
/// from the previous tick exceeds ``expectedPeriod * dropTolerance``
/// is counted as one dropped frame. The first tick is never counted
/// because we have no prior reading. The exposed ratio is
/// ``dropped / total`` clamped to ``[0, 1]``.
public final class PerfMetricsCollector {
    /// How much slack we allow before a long inter-frame gap counts
    /// as a drop. ``1.5`` keeps single-frame jitter below the
    /// threshold while still catching back-to-back drops.
    public static let defaultDropTolerance: Double = 1.5

    private let dropTolerance: Double
    private var pendingWakeAt: TimeInterval?
    private var lastFrameAt: TimeInterval?
    private(set) var totalFrames: Int = 0
    private(set) var droppedFrames: Int = 0
    private var lastWakeSecondsValue: TimeInterval?

    public init(dropTolerance: Double = PerfMetricsCollector.defaultDropTolerance) {
        precondition(dropTolerance > 1.0, "dropTolerance must be > 1.0 to leave any slack")
        self.dropTolerance = dropTolerance
    }

    // MARK: - Wake -----------------------------------------------------

    /// Mark a wake event. Subsequent :func:`markFirstFrame(at:)`
    /// calls measure the elapsed time against this timestamp until a
    /// frame is observed.
    ///
    /// Multiple back-to-back wakes (rare; e.g. macOS lid wake + sleep
    /// wake within the same second) collapse to the *latest* wake so
    /// the budget reflects the closest wake → frame gap.
    public func recordWake(at timestamp: TimeInterval) {
        pendingWakeAt = timestamp
    }

    /// Latch the wake-to-first-frame elapsed time. No-op if no wake
    /// is currently pending.
    public func markFirstFrame(at timestamp: TimeInterval) {
        guard let pending = pendingWakeAt else { return }
        let elapsed = max(0, timestamp - pending)
        lastWakeSecondsValue = elapsed
        pendingWakeAt = nil
    }

    /// Visible for tests. ``true`` while a wake has fired but the
    /// next frame has not yet been observed.
    public var isAwaitingFirstFrame: Bool {
        pendingWakeAt != nil
    }

    // MARK: - Frame ticks ---------------------------------------------

    /// Record one display tick at ``timestamp``. ``expectedPeriod``
    /// is the nominal inter-frame period (e.g. ``1/60`` for 60 Hz).
    ///
    /// The first tick of a new collector — or after
    /// :func:`resetFrameStats()` — is *not* counted toward
    /// ``totalFrames`` because there's no baseline to compare
    /// against.
    public func recordFrameTick(
        at timestamp: TimeInterval,
        expectedPeriod: TimeInterval
    ) {
        guard expectedPeriod > 0 else { return }
        defer { lastFrameAt = timestamp }
        guard let previous = lastFrameAt else { return }
        let interval = timestamp - previous
        guard interval > 0 else { return }
        totalFrames += 1
        if interval > expectedPeriod * dropTolerance {
            droppedFrames += 1
        }
    }

    /// Reset the frame counters. The pending-wake / last-wake state
    /// is preserved so a counter reset can't hide a prior wake
    /// budget breach.
    public func resetFrameStats() {
        lastFrameAt = nil
        totalFrames = 0
        droppedFrames = 0
    }

    // MARK: - Snapshot -------------------------------------------------

    public func snapshot() -> PerfMetricsSnapshot {
        let ratio: Double
        if totalFrames == 0 {
            ratio = 0
        } else {
            ratio = min(1.0, max(0.0, Double(droppedFrames) / Double(totalFrames)))
        }
        return PerfMetricsSnapshot(
            lastWakeSeconds: lastWakeSecondsValue,
            totalFrames: totalFrames,
            droppedFrames: droppedFrames,
            frameDropRatio: ratio
        )
    }
}

/// Codable wire-shape for the §3.1 Swift-side metrics. Designed to
/// drop straight into a future ``perf.metrics`` envelope payload.
public struct PerfMetricsSnapshot: Codable, Equatable, Sendable {
    public let lastWakeSeconds: TimeInterval?
    public let totalFrames: Int
    public let droppedFrames: Int
    public let frameDropRatio: Double

    public init(
        lastWakeSeconds: TimeInterval?,
        totalFrames: Int,
        droppedFrames: Int,
        frameDropRatio: Double
    ) {
        self.lastWakeSeconds = lastWakeSeconds
        self.totalFrames = totalFrames
        self.droppedFrames = droppedFrames
        self.frameDropRatio = frameDropRatio
    }

    /// Frame drop expressed as a percentage so it lines up with the
    /// :func:`evaluate_budgets` ``frame_drop_pct`` field.
    public var frameDropPct: Double {
        frameDropRatio * 100.0
    }
}
