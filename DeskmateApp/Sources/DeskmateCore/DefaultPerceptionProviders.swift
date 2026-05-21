import Foundation
#if canImport(AppKit)
import AppKit
#endif
#if canImport(CoreGraphics)
import CoreGraphics
#endif
#if canImport(ApplicationServices)
import ApplicationServices
#endif

/// Production-grade closures for :class:`PerceptionSampler`
/// (V10 Phase 11d-i / 13-ii). Kept separate from :class:`PerceptionSampler` so
/// unit tests never need AppKit / CoreGraphics / AX.
///
/// All providers gracefully degrade when their APIs aren't present
/// (e.g. on Linux CI, or when Accessibility permission is denied):
///
/// - ``idleSeconds`` returns 0
/// - ``frontmostApp`` returns ``FrontmostApp.none``
/// - ``frontmostWindowTitle`` returns ``nil``
public enum DefaultPerceptionProviders {

    /// Seconds since the last keyboard/mouse event on any input
    /// source. Uses ``CGEventSource.secondsSinceLastEventType`` under
    /// the hood.
    public static func idleSeconds() -> TimeInterval {
        #if canImport(CoreGraphics)
        // ``kCGAnyInputEventType`` equals ``(1 << 31) - 1`` historically;
        // passing the numeric value avoids leaning on private constants.
        // Apple's public header exposes it as the same sentinel.
        let anyInputEvent = CGEventType(rawValue: ~0) ?? .null
        return CGEventSource.secondsSinceLastEventType(
            .combinedSessionState, eventType: anyInputEvent
        )
        #else
        return 0
        #endif
    }

    /// Bundle id + localized name of the foreground application, plus
    /// (when Accessibility permission is granted) the frontmost
    /// window's AX title — which for most IDEs is the currently-open
    /// file or project.
    public static func frontmostApp() -> PerceptionSampler.FrontmostApp {
        #if canImport(AppKit)
        let app = NSWorkspace.shared.frontmostApplication
        let axTitle: String? = {
            #if canImport(ApplicationServices)
            guard let pid = app?.processIdentifier else { return nil }
            return frontmostWindowTitle(pid: pid)
            #else
            return nil
            #endif
        }()
        return .init(
            bundleId: app?.bundleIdentifier,
            title: axTitle ?? app?.localizedName,
            windowTitle: axTitle
        )
        #else
        return .none
        #endif
    }

    /// Read the frontmost window's title from a given process id via
    /// the Accessibility API. Returns ``nil`` when any of the
    /// following are true:
    ///
    /// - The user hasn't granted Accessibility permission
    /// - The process doesn't expose a focused window (headless helper,
    ///   launcher, …)
    /// - The AX attribute read fails for any reason
    ///
    /// Callers should treat the result as a best-effort hint, never a
    /// required field.
    public static func frontmostWindowTitle(pid: pid_t) -> String? {
        #if canImport(ApplicationServices)
        // Cheap-and-non-intrusive trust check. We deliberately use the
        // non-prompting form: a first-run prompt lives in a dedicated
        // permissions flow, not inside the sampler tick.
        guard AXIsProcessTrusted() else { return nil }

        let appElem = AXUIElementCreateApplication(pid)
        var focusedRef: CFTypeRef?
        let focusedResult = AXUIElementCopyAttributeValue(
            appElem,
            kAXFocusedWindowAttribute as CFString,
            &focusedRef
        )
        guard focusedResult == .success, let focusedAny = focusedRef else {
            return nil
        }
        // Narrow CFTypeRef → AXUIElement. If the system handed us
        // something else (shouldn't happen in practice) we bail.
        guard CFGetTypeID(focusedAny) == AXUIElementGetTypeID() else {
            return nil
        }
        let windowElem = focusedAny as! AXUIElement  // safe: checked type id above

        var titleRef: CFTypeRef?
        let titleResult = AXUIElementCopyAttributeValue(
            windowElem,
            kAXTitleAttribute as CFString,
            &titleRef
        )
        guard titleResult == .success,
              let title = titleRef as? String,
              !title.isEmpty
        else {
            return nil
        }
        return title
        #else
        return nil
        #endif
    }
}

/// TTL wrapper for expensive frontmost-app reads (V10 L3-C2).
///
/// ``NSWorkspace.frontmostApplication`` is cheap-ish, but the paired
/// AX focused-window title read can cross process boundaries. The
/// sampler may tick faster than humans can change foreground apps, so
/// production wiring uses a 1 s cache while tests inject a synthetic
/// clock/provider and verify the refresh boundary exactly.
public final class CachedFrontmostAppProvider {
    public typealias Clock = () -> TimeInterval
    public typealias Provider = () -> PerceptionSampler.FrontmostApp

    private let ttl: TimeInterval
    private let clock: Clock
    private let provider: Provider
    private var cachedAt: TimeInterval?
    private var cachedValue: PerceptionSampler.FrontmostApp?

    public init(
        ttl: TimeInterval = 1.0,
        clock: @escaping Clock = { Date().timeIntervalSinceReferenceDate },
        provider: @escaping Provider
    ) {
        precondition(ttl > 0, "ttl must be positive")
        self.ttl = ttl
        self.clock = clock
        self.provider = provider
    }

    public func callAsFunction() -> PerceptionSampler.FrontmostApp {
        let now = clock()
        if let cachedAt, let cachedValue, now - cachedAt < ttl {
            return cachedValue
        }
        let fresh = provider()
        cachedAt = now
        cachedValue = fresh
        return fresh
    }

    public func invalidate() {
        cachedAt = nil
        cachedValue = nil
    }
}
