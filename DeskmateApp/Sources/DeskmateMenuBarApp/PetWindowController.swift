#if canImport(AppKit)
import AppKit
import SwiftUI
import DeskmateCore

/// Owns the borderless floating window that hosts
/// :class:`PetOverlay` (V10 Phase 11d-vii). Parks the sprite at the
/// bottom-right of the main screen by default; the window is
/// click-through-safe for the aura but not the sprite Button.
@MainActor
final class PetWindowController {
    private let runtime: DeskmateMenuBarRuntime
    private var window: NSWindow?

    init(runtime: DeskmateMenuBarRuntime) {
        self.runtime = runtime
    }

    func install() {
        guard window == nil else { return }
        let host = NSHostingView(rootView: PetOverlay(runtime: runtime))
        host.frame = NSRect(x: 0, y: 0, width: 300, height: 260)

        let w = NSWindow(
            contentRect: host.frame,
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = false
        // ``.floating`` keeps the pet above normal windows but below
        // the island pill (``.statusBar``). Stacking order is:
        //     menu bar popover > island > pet > regular windows.
        w.level = .floating
        w.collectionBehavior = [
            .canJoinAllSpaces,
            .stationary,
            .fullScreenAuxiliary,
        ]
        // Clicks on the pet button + bubble action buttons must land;
        // SwiftUI's hit-testing takes care of the transparent aura
        // area rejecting clicks automatically.
        w.ignoresMouseEvents = false
        w.contentView = host
        w.setContentSize(host.fittingSize)

        positionAtBottomRight(window: w)
        w.orderFrontRegardless()
        self.window = w
    }

    func close() {
        window?.orderOut(nil)
        window = nil
    }

    private func positionAtBottomRight(window w: NSWindow) {
        guard let screen = NSScreen.main else { return }
        let frame = screen.visibleFrame
        let size = w.frame.size
        let margin: CGFloat = 16
        let x = frame.maxX - size.width - margin
        let y = frame.minY + margin
        w.setFrame(
            NSRect(x: x, y: y, width: size.width, height: size.height),
            display: true
        )
    }
}
#endif
