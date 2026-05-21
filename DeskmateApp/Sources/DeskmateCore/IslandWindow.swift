import Foundation
import CoreGraphics

#if canImport(AppKit)
import AppKit

/// Transparent, always-on-top panel hosting the Dynamic Island surface
/// (V10 L2-#7 / L3-A10).
///
/// AppKit lives only here and in :type:`PetWindow`. Hover / drag / geometry
/// logic stays headless-friendly so the bulk of the code is unit-tested
/// without a running GUI.
public final class IslandWindow: NSPanel {
    public init(geometry: IslandGeometry) {
        // Start at the compact rect; callers animate + resize later.
        let rect = geometry.compactRect()
        super.init(
            contentRect: rect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = false
        self.level = .statusBar
        self.collectionBehavior = [
            .canJoinAllSpaces,
            .stationary,
            .ignoresCycle,
            .fullScreenAuxiliary,
        ]
        // The island is interactive by default — hover promotes to session
        // list, tap opens the notification card.
        self.ignoresMouseEvents = false
        self.acceptsMouseMovedEvents = true
        self.hidesOnDeactivate = false
    }

    /// Convenience for the drag / event layer. Kept on the window so the
    /// pure :type:`IslandHoverRouter` never imports AppKit.
    @discardableResult
    public func route(
        event: IslandHoverRouter.Event,
        router: IslandHoverRouter,
        current: IslandSurfaceKind
    ) -> IslandHoverRouter.Decision {
        router.decide(event: event, current: current)
    }

    public override var canBecomeKey: Bool { false }
    public override var canBecomeMain: Bool { false }

    /// Capture the current screen layout the way the window sees it. Feed
    /// this into :type:`IslandGeometry` so the geometry math never depends
    /// on live :type:`NSScreen`.
    public static func currentNotchSize() -> CGSize? {
        guard let mainScreen = NSScreen.main else { return nil }
        if #available(macOS 12.0, *) {
            let inset = mainScreen.safeAreaInsets.top
            guard inset > 0 else { return nil }
            // Approximate width based on typical MBP notch (200pt); the
            // actual cutout is reported by the system only via safe-area
            // insets. Callers can override if they have a measured value.
            return CGSize(width: 200, height: inset)
        }
        return nil
    }
}

#else

public struct IslandWindow {}

#endif
