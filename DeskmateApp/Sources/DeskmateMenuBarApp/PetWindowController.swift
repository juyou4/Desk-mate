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
    private var dragStartFrame: NSRect?
    private var observedResetToken = 0
    private var resetTimer: Timer?
    private let edgeMargin: CGFloat = 16
    private let defaults: UserDefaults

    private enum DefaultsKey {
        static let originX = "deskmate.petWindow.origin.x"
        static let originY = "deskmate.petWindow.origin.y"
    }

    init(runtime: DeskmateMenuBarRuntime, defaults: UserDefaults = .standard) {
        self.runtime = runtime
        self.defaults = defaults
    }

    func install() {
        guard window == nil else { return }
        let host = NSHostingView(
            rootView: PetOverlay(
                runtime: runtime,
                onPetDragBegan: { [weak self] in
                    self?.beginPetDrag()
                },
                onPetDragChanged: { [weak self] translation in
                    self?.updatePetDrag(translation: translation)
                },
                onPetDragEnded: { [weak self] in
                    self?.endPetDrag()
                }
            )
        )
        host.frame = NSRect(x: 0, y: 0, width: 380, height: 340)

        let w = PetOverlayWindow(
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

        restoreOrPositionAtDefault(window: w)
        w.orderFrontRegardless()
        self.window = w
        startResetObserver()
    }

    func close() {
        resetTimer?.invalidate()
        resetTimer = nil
        window?.orderOut(nil)
        window = nil
    }

    func resetPosition() {
        defaults.removeObject(forKey: DefaultsKey.originX)
        defaults.removeObject(forKey: DefaultsKey.originY)
        guard let window else { return }
        if let origin = geometry(for: window).defaultOrigin()?.origin {
            setWindowOrigin(origin, window: window, persist: false)
        }
    }

    private func beginPetDrag() {
        guard let window else { return }
        dragStartFrame = window.frame
    }

    private func updatePetDrag(translation: CGSize) {
        guard let window, let start = dragStartFrame else { return }
        let requested = CGPoint(
            x: start.origin.x + translation.width,
            y: start.origin.y - translation.height
        )
        setWindowOrigin(requested, window: window, persist: false)
    }

    private func endPetDrag() {
        guard let window else {
            dragStartFrame = nil
            return
        }
        persist(window.frame.origin)
        dragStartFrame = nil
    }

    private func restoreOrPositionAtDefault(window w: NSWindow) {
        if let restored = restoredOrigin(for: w) {
            setWindowOrigin(restored, window: w, persist: false)
            return
        }
        guard let origin = geometry(for: w).defaultOrigin()?.origin else { return }
        setWindowOrigin(origin, window: w, persist: false)
    }

    private func restoredOrigin(for w: NSWindow) -> CGPoint? {
        guard defaults.object(forKey: DefaultsKey.originX) != nil,
              defaults.object(forKey: DefaultsKey.originY) != nil
        else {
            return nil
        }
        return CGPoint(
            x: defaults.double(forKey: DefaultsKey.originX),
            y: defaults.double(forKey: DefaultsKey.originY)
        )
    }

    private func setWindowOrigin(
        _ requested: CGPoint,
        window w: NSWindow,
        persist shouldPersist: Bool
    ) {
        let resolved = geometry(for: w).clamp(requested: requested)
        let origin = resolved?.origin ?? requested
        w.setFrameOrigin(origin)
        if shouldPersist {
            persist(origin)
        }
    }

    private func persist(_ origin: CGPoint) {
        defaults.set(Double(origin.x), forKey: DefaultsKey.originX)
        defaults.set(Double(origin.y), forKey: DefaultsKey.originY)
    }

    private func geometry(for w: NSWindow) -> PetWindowGeometry {
        PetWindowGeometry(
            screens: PetWindow.currentScreens(),
            petSize: w.frame.size,
            edgeMargin: edgeMargin
        )
    }

    private func startResetObserver() {
        observedResetToken = runtime.petPositionResetToken
        resetTimer?.invalidate()
        resetTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) {
            [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                let token = self.runtime.petPositionResetToken
                guard token != self.observedResetToken else { return }
                self.observedResetToken = token
                self.resetPosition()
            }
        }
    }
}

private final class PetOverlayWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}
#endif
