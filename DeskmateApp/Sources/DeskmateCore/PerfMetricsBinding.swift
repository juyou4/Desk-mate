import Foundation
import QuartzCore

/// Wires :class:`PerfMetricsCollector` into a notification-center
/// wake observer + a periodic envelope push so V10 §3.1 row 6
/// (wake-to-first-frame) and row 8 (frame drop ratio) reach the
/// Python agent over the bridge.
///
/// The notification source is injected so production code passes
/// ``NSWorkspace.shared.notificationCenter`` and
/// ``NSWorkspace.didWakeNotification`` (from
/// ``DeskmateMenuBarRuntime``) while smoke + XCTest pass a freshly
/// constructed ``NotificationCenter()`` and post synthetic
/// notifications through it. This keeps the binding testable
/// without AppKit.
///
/// The CVDisplayLink-driven frame ticker is intentionally **not**
/// bundled in this slice. ``recordFrameTick(at:expectedPeriod:)`` is
/// exposed so a follow-up patch can swap in the real link without
/// changing the binding's public surface.
public final class PerfMetricsBinding {
    public typealias Clock = () -> TimeInterval
    public typealias EnvelopeSenderRef = () -> EnvelopeSender?

    public let collector: PerfMetricsCollector
    private let center: NotificationCenter
    private let wakeName: Notification.Name
    private let clock: Clock
    private let senderProvider: EnvelopeSenderRef
    private let frameTickerSource: FrameTickerSource?
    private let queue = DispatchQueue(label: "deskmate.perfmetrics.binding")

    private var observer: NSObjectProtocol?
    private var pushTimer: DispatchSourceTimer?

    /// Mirror of ``NSWorkspace.didWakeNotification`` — exposed by
    /// raw name so the binding compiles + tests on platforms where
    /// AppKit isn't linked (e.g. Command Line Tools toolchain).
    public static let defaultWakeNotificationName =
        Notification.Name("NSWorkspaceDidWakeNotification")

    public init(
        collector: PerfMetricsCollector = .init(),
        center: NotificationCenter = .default,
        wakeNotificationName: Notification.Name =
            PerfMetricsBinding.defaultWakeNotificationName,
        clock: @escaping Clock = { CACurrentMediaTime() },
        sender: @escaping EnvelopeSenderRef,
        frameTickerSource: FrameTickerSource? = nil
    ) {
        self.collector = collector
        self.center = center
        self.wakeName = wakeNotificationName
        self.clock = clock
        self.senderProvider = sender
        self.frameTickerSource = frameTickerSource
    }

    // MARK: - Lifecycle ------------------------------------------------

    /// Start observing wake notifications, schedule the recurring
    /// envelope push (when ``pushInterval > 0``), and start the
    /// frame ticker (if one was injected). Calling this twice is a
    /// no-op past the first time.
    public func start(pushInterval: TimeInterval = 5.0) {
        queue.sync {
            installObserverLocked()
            if pushInterval > 0 {
                schedulePushLocked(interval: pushInterval)
            }
            installFrameTickerLocked()
        }
    }

    public func stop() {
        queue.sync {
            if let observer { center.removeObserver(observer) }
            observer = nil
            pushTimer?.cancel()
            pushTimer = nil
            frameTickerSource?.stop()
        }
    }

    // MARK: - Public API -----------------------------------------------

    /// Latch the wake-to-first-frame budget. Call from the first
    /// SwiftUI render after the menu bar app boots and after each
    /// wake. Idempotent when no wake is pending.
    public func markFirstFrame() {
        let t = clock()
        queue.sync { collector.markFirstFrame(at: t) }
    }

    /// Fire one envelope push immediately. Used by the timer and by
    /// tests that want a deterministic flush.
    public func pushSnapshot() {
        queue.sync { pushSnapshotLocked() }
    }

    /// Test-only entry point until ``CVDisplayLink`` lands in a
    /// follow-up slice. Production code goes through whatever frame
    /// source the binding's owner installs.
    public func recordFrameTick(
        at timestamp: TimeInterval, expectedPeriod: TimeInterval
    ) {
        queue.sync {
            collector.recordFrameTick(
                at: timestamp, expectedPeriod: expectedPeriod
            )
        }
    }

    // MARK: - Internals ------------------------------------------------

    private func installObserverLocked() {
        guard observer == nil else { return }
        observer = center.addObserver(
            forName: wakeName, object: nil, queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            // The notification center may dispatch us on any queue;
            // hop onto the binding's serial queue so the collector
            // is touched from a single owner.
            let now = self.clock()
            self.queue.sync { self.collector.recordWake(at: now) }
        }
    }

    private func installFrameTickerLocked() {
        guard let source = frameTickerSource else { return }
        source.start { [weak self] timestamp, expectedPeriod in
            guard let self else { return }
            // CVDisplayLink dispatches on a high-priority thread.
            // Hop to the binding's serial queue before touching
            // the collector — the queue is the only writer.
            self.queue.async {
                self.collector.recordFrameTick(
                    at: timestamp, expectedPeriod: expectedPeriod
                )
            }
        }
    }

    private func schedulePushLocked(interval: TimeInterval) {
        pushTimer?.cancel()
        let src = DispatchSource.makeTimerSource(queue: queue)
        src.schedule(deadline: .now() + interval, repeating: interval)
        src.setEventHandler { [weak self] in self?.pushSnapshotLocked() }
        pushTimer = src
        src.resume()
    }

    private func pushSnapshotLocked() {
        guard let sender = senderProvider() else { return }
        let snap = collector.snapshot()
        var payload: [String: AnyJSONValue] = [
            "total_frames": .int(snap.totalFrames),
            "dropped_frames": .int(snap.droppedFrames),
            "frame_drop_ratio": .double(snap.frameDropRatio),
        ]
        if let wake = snap.lastWakeSeconds {
            payload["last_wake_seconds"] = .double(wake)
        } else {
            payload["last_wake_seconds"] = .null
        }
        do {
            try sender.send(
                BridgeEnvelope.of(.perfMetrics, payload: payload)
            )
        } catch {
            // Sender is responsible for reconnect / backoff. We don't
            // log here because the binding sits in DeskmateCore and
            // logging belongs to the menu bar runtime.
        }
    }
}
