import Foundation
import CoreGraphics

#if canImport(AppKit)
import AppKit

/// Transparent always-on-top panel that hosts the pet sprite (V10 L2-#1).
///
/// AppKit is imported only here — the rest of ``DeskmateCore`` stays
/// headless-friendly. GUI-exhaustive verification happens in Xcode; the
/// unit tests cover the pure ``PetWindowGeometry`` / ``PetDragController`` /
/// ``PetFrameAnimator`` logic that drives this panel.
public final class PetWindow: NSPanel {
    public init(contentSize: NSSize) {
        super.init(
            contentRect: NSRect(origin: .zero, size: contentSize),
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
        self.isMovableByWindowBackground = false
        self.acceptsMouseMovedEvents = true
        self.hidesOnDeactivate = false
    }

    /// Let clicks fall straight through the pet window to whatever is
    /// beneath it. Toggle this off while the pet is being dragged or the
    /// bubble is showing an actionable surface (V10 L1-C / L2-#1).
    public var passthroughWhenIdle: Bool = true {
        didSet { self.ignoresMouseEvents = passthroughWhenIdle }
    }

    public override var canBecomeKey: Bool { false }
    public override var canBecomeMain: Bool { false }

    /// Snapshot of the current screen layout. Callers pass this into
    /// :type:`PetWindowGeometry` so geometry logic stays side-effect free.
    public static func currentScreens() -> [PetScreen] {
        NSScreen.screens.enumerated().map { index, screen in
            PetScreen(id: index, visibleFrame: screen.visibleFrame)
        }
    }
}

#else

/// Non-AppKit platforms (headless CI, Linux CLI) get a stub so the rest of
/// the core library still compiles — only the Xcode target wires up a real
/// window.
public struct PetWindow {}

#endif
