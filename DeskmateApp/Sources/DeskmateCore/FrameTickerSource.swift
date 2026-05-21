import Foundation
import QuartzCore
#if canImport(CoreVideo)
import CoreVideo
#endif

/// Generic frame tick source. Production code wires this to a
/// :class:`CVDisplayLinkFrameTicker`; tests inject a hand-driven
/// mock that calls ``onTick`` synchronously to verify the binding's
/// frame-drop counter without needing a live display link.
///
/// The contract:
/// - ``start(onTick:)`` may be called at most once before ``stop()``.
/// - ``onTick`` is invoked once per display refresh on whatever queue
///   the source uses (CVDisplayLink uses a high-priority thread; the
///   binding's serial queue takes care of state-isolating the
///   collector).
/// - ``stop()`` must release any underlying display link / handler so
///   the source can be reused, and so deallocation doesn't leak.
public protocol FrameTickerSource: AnyObject {
    func start(onTick: @escaping (TimeInterval, TimeInterval) -> Void)
    func stop()
}

#if canImport(CoreVideo)

/// Production frame ticker backed by ``CVDisplayLink`` (macOS 10.4+,
/// deprecated in macOS 15 — the replacement is ``CADisplayLink`` on
/// macOS 14+. We keep the CV path because the package's deployment
/// target is macOS 13).
///
/// Each callback yields ``(timestamp, expectedPeriod)`` derived from
/// the display's reported video refresh period, so the binding can
/// compare actual vs expected interval and count drops without
/// hard-coding 60 Hz.
public final class CVDisplayLinkFrameTicker: FrameTickerSource {
    private var displayLink: CVDisplayLink?
    private var tickHandler: ((TimeInterval, TimeInterval) -> Void)?
    private let lock = NSLock()

    public init() {}

    deinit {
        stop()
    }

    public func start(onTick: @escaping (TimeInterval, TimeInterval) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard displayLink == nil else { return }

        var link: CVDisplayLink?
        let createResult = CVDisplayLinkCreateWithActiveCGDisplays(&link)
        guard createResult == kCVReturnSuccess, let link else { return }

        tickHandler = onTick
        displayLink = link

        let setResult = CVDisplayLinkSetOutputHandler(link) {
            [weak self] _, _, inOutputTime, _, _ -> CVReturn in
            guard let self else { return kCVReturnSuccess }

            let timeScale = TimeInterval(inOutputTime.pointee.videoTimeScale)
            let nowSeconds: TimeInterval
            if timeScale > 0 {
                nowSeconds = TimeInterval(inOutputTime.pointee.videoTime) / timeScale
            } else {
                nowSeconds = CACurrentMediaTime()
            }
            let refreshPeriod = TimeInterval(inOutputTime.pointee.videoRefreshPeriod)
            let expectedPeriod: TimeInterval
            if timeScale > 0, refreshPeriod > 0 {
                expectedPeriod = refreshPeriod / timeScale
            } else {
                expectedPeriod = 1.0 / 60.0  // last-resort guess
            }

            // Snapshot the handler under the lock so a stop() racing
            // with the callback can't see a half-mutated reference.
            self.lock.lock()
            let handler = self.tickHandler
            self.lock.unlock()
            handler?(nowSeconds, expectedPeriod)
            return kCVReturnSuccess
        }
        guard setResult == kCVReturnSuccess else {
            tickHandler = nil
            displayLink = nil
            return
        }
        CVDisplayLinkStart(link)
    }

    public func stop() {
        lock.lock()
        defer { lock.unlock() }
        if let link = displayLink {
            CVDisplayLinkStop(link)
        }
        displayLink = nil
        tickHandler = nil
    }
}

#endif
