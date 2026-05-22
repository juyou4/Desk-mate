import Foundation

/// V10 island polish: pure value type that encapsulates the
/// expand/collapse animation timings used by the menu-bar layer.
///
/// Both the SwiftUI surface (``IslandOverlay``) and the AppKit
/// window resize (``IslandWindowController.applyPanelFrame``) must
/// share the same numbers — otherwise the panel frame and the
/// notch shape inside it finish their transition at different
/// instants, producing the "panel snaps before the surface
/// catches up" stutter that earlier versions of the island had.
///
/// Inspired by ``boring.notch/boringNotch/ContentView.swift`` (open
/// vs close springs at lines 123-124) and
/// ``MioIsland/ClaudeIsland/UI/Views/NotchView.swift`` (hover
/// debounce + asymmetric springs at lines 215-217). We expose these
/// numbers as properties on a struct so the smoke binary can lock
/// the values without having to import the menu-bar AppKit module.
public struct IslandAnimationTuning: Equatable, Sendable {
    /// Base hover-to-open delay, scaled by ``hoverSpeed``.
    public var hoverOpenBaseDelay: TimeInterval
    /// Hover-to-close delay. Held constant — closing should always
    /// feel responsive regardless of ``hoverSpeed``.
    public var hoverCloseDelay: TimeInterval
    /// Grace window after the cursor leaves the activation rect
    /// during which we *don't* cancel an in-flight open timer.
    public var hoverOpenCancelGrace: TimeInterval
    /// Duration of the AppKit ``animator().setFrame`` when the
    /// panel is growing into its expanded size.
    public var panelOpenDuration: TimeInterval
    /// Duration of the AppKit ``animator().setFrame`` when the
    /// panel is shrinking back into its compact size. Slightly
    /// longer than ``panelOpenDuration`` so the close lands
    /// without a visible bounce — matches MioIsland's
    /// ``Animation.spring(response: 0.45, dampingFraction: 1.0)``.
    public var panelCloseDuration: TimeInterval
    /// Lower clamp on the hover-speed multiplier — a typo of 0
    /// would otherwise pin the delay to infinity.
    public var hoverSpeedMin: Double
    /// Upper clamp — anything above this is rounded down so a
    /// runaway value can't make the open animation feel jumpy.
    public var hoverSpeedMax: Double

    public init(
        hoverOpenBaseDelay: TimeInterval = 0.20,
        hoverCloseDelay: TimeInterval = 0.14,
        hoverOpenCancelGrace: TimeInterval = 0.10,
        panelOpenDuration: TimeInterval = 0.42,
        panelCloseDuration: TimeInterval = 0.45,
        hoverSpeedMin: Double = 0.25,
        hoverSpeedMax: Double = 4.0
    ) {
        self.hoverOpenBaseDelay = hoverOpenBaseDelay
        self.hoverCloseDelay = hoverCloseDelay
        self.hoverOpenCancelGrace = hoverOpenCancelGrace
        self.panelOpenDuration = panelOpenDuration
        self.panelCloseDuration = panelCloseDuration
        self.hoverSpeedMin = hoverSpeedMin
        self.hoverSpeedMax = hoverSpeedMax
    }

    /// Resolve the hover-open delay for the given user-facing
    /// ``hoverSpeed``. ``speed == 1.0`` returns ``hoverOpenBaseDelay``;
    /// higher values shorten the delay, lower values lengthen it.
    /// ``speed <= 0`` is treated as MioIsland's "instant" mode and
    /// returns ``0`` — useful for accessibility users who want the
    /// island to react without any debounce.
    public func resolvedHoverOpenDelay(hoverSpeed: Double) -> TimeInterval {
        if hoverSpeed <= 0 { return 0 }
        let clamped = min(max(hoverSpeed, hoverSpeedMin), hoverSpeedMax)
        return hoverOpenBaseDelay / clamped
    }

    /// Pick the AppKit ``setFrame`` duration for an expand/collapse
    /// transition. Centralised so SwiftUI and AppKit can't drift
    /// apart accidentally — the smoke harness exercises this
    /// function directly.
    public func panelFrameDuration(forceExpanded: Bool, animated: Bool) -> TimeInterval {
        guard animated else { return 0 }
        return forceExpanded ? panelOpenDuration : panelCloseDuration
    }

    public static let `default` = IslandAnimationTuning()
}
