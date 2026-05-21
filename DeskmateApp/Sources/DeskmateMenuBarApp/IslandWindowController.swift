#if canImport(AppKit)
import AppKit
import Combine
import QuartzCore
import SwiftUI
import DeskmateCore

/// Owns a single borderless floating :class:`NSWindow` that hosts the
/// :class:`IslandOverlay` SwiftUI view at the top-center of the main
/// screen (V10 Phase 11d-vi).
///
/// The compact window is click-through so it does not block menu-bar items.
/// Hover is handled by global mouse monitoring; the window only accepts
/// events while expanded.
///
/// V10 Phase 9 · §4 step 5: subscribes to the runtime's
/// ``degradationPolicy`` and, when ``islandOrderOut`` flips on,
/// closes the window via ``orderOut(_:)``. Falling back below the
/// threshold restores the window through the normal install path,
/// so the user sees the pill come back the moment the system stops
/// degrading.
@MainActor
final class IslandWindowController {
    private let runtime: DeskmateMenuBarRuntime
    private var window: NSPanel?
    private var policyCancellable: AnyCancellable?
    private var islandCancellable: AnyCancellable?
    private var sessionsCancellable: AnyCancellable?
    private var approvalsCancellable: AnyCancellable?
    private var localMouseMonitor: Any?
    private var globalMouseMonitor: Any?
    private var hoverOpenWorkItem: DispatchWorkItem?
    private var hoverCloseWorkItem: DispatchWorkItem?
    private var lastMouseMoveAt: CFTimeInterval = 0

    init(runtime: DeskmateMenuBarRuntime) {
        self.runtime = runtime
    }

    /// Visible for tests / diagnostics — true while a window is
    /// presented on screen.
    var isPresented: Bool { window != nil }

    func install() {
        // Always wire the policy subscription, even if the window is
        // already up — this is how a controller installed before the
        // runtime emits its first policy delta picks up later
        // changes. Subscribing is idempotent (we only attach once).
        installPolicySubscriptionIfNeeded()

        // V10 Phase 9 · §4 step 5: respect the latest policy *before*
        // building the window. If the level is already at 5 when the
        // controller is being installed (e.g. cold-start in a
        // degraded state) we never put the pill on screen at all.
        if runtime.degradationPolicy.islandOrderOut { return }

        guard window == nil else { return }
        let host = IslandHostingView(
            rootView: IslandOverlay(runtime: runtime),
            runtime: runtime
        )
        host.translatesAutoresizingMaskIntoConstraints = true
        host.autoresizingMask = [.width, .height]
        host.frame = panelFrame()

        let w = NSPanel(
            contentRect: host.frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        w.isFloatingPanel = true
        w.becomesKeyOnlyIfNeeded = false
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = false
        // ``.statusBar`` sits above menu bar items so the pill remains
        // visible when the user pulls down the menu bar; this is the
        // same level macOS uses for system dialogs like the volume
        // HUD.
        w.level = .statusBar
        // Float across Spaces + fullscreen apps; don't try to own any
        // Mission Control real estate.
        w.collectionBehavior = [
            .canJoinAllSpaces,
            .stationary,
            .fullScreenAuxiliary,
        ]
        w.ignoresMouseEvents = !runtime.isIslandExpanded
        // Prevent accidental drags from knocking the pill away from
        // the notch anchor.
        w.isMovableByWindowBackground = false
        w.isMovable = false
        w.hidesOnDeactivate = false
        w.titleVisibility = .hidden
        w.titlebarAppearsTransparent = true
        w.contentView = host

        positionAtTopCenter(window: w)
        w.orderFrontRegardless()

        self.window = w
        installResizeSubscriptions()
        installMouseMonitors()
        publishDiagnostics()
    }

    func close() {
        removeMouseMonitors()
        window?.orderOut(nil)
        window = nil
        islandCancellable = nil
        sessionsCancellable = nil
        approvalsCancellable = nil
    }

    // MARK: - Degradation policy subscription

    private func installPolicySubscriptionIfNeeded() {
        guard policyCancellable == nil else { return }
        policyCancellable = runtime.$degradationPolicy
            .receive(on: DispatchQueue.main)
            .sink { [weak self] policy in
                self?.applyDegradation(policy)
            }
    }

    private func installResizeSubscriptions() {
        guard islandCancellable == nil else { return }
        islandCancellable = runtime.$island
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.resizeForCurrentState() }
        sessionsCancellable = runtime.$sessions
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.resizeForCurrentState() }
        approvalsCancellable = runtime.$approvals
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.resizeForCurrentState() }
    }

    /// React to a policy change. Visible to tests so they can drive
    /// the level transitions deterministically without spinning a
    /// Combine pipeline.
    func applyDegradation(_ policy: DegradationPolicy) {
        if policy.islandOrderOut {
            close()
        } else if window == nil {
            install()
        }
    }

    // MARK: - Layout

    /// Keep the transparent panel at opened size and attach its top edge to
    /// the screen top. The visible notch surface is drawn inside SwiftUI, so
    /// compact/open transitions do not create a detached AppKit window below
    /// the physical notch.
    private func positionAtTopCenter(window w: NSPanel) {
        guard let screen = targetScreen() else { return }
        let size = panelSize(for: screen)
        let rect = NSRect(
            x: screen.frame.midX - size.width / 2,
            y: screen.frame.maxY - size.height,
            width: size.width,
            height: size.height
        )
        w.setFrame(rect, display: true)
    }

    private func resizeForCurrentState() {
        guard let w = window,
              let host = w.contentView as? IslandHostingView<IslandOverlay>
        else { return }

        if runtime.isIslandExpanded {
            applyPanelFrame(forceExpanded: true)
            w.ignoresMouseEvents = false
        } else if isExpandedPanel(window: w) {
            host.frame = panelFrame(forceExpanded: true)
            w.ignoresMouseEvents = true
        } else {
            host.frame = panelFrame(forceExpanded: false)
            w.ignoresMouseEvents = true
            applyPanelFrame(forceExpanded: false)
        }
        publishDiagnostics()
    }

    private func panelFrame(forceExpanded: Bool? = nil) -> NSRect {
        guard let screen = targetScreen() else {
            return NSRect(x: 0, y: 0, width: 588, height: 314)
        }
        let size = geometry(for: screen, forceExpanded: forceExpanded).panelSize
        return NSRect(x: 0, y: 0, width: size.width, height: size.height)
    }

    private func targetScreen() -> NSScreen? {
        NSScreen.screens.first(where: { $0.deskmateHasPhysicalNotch })
            ?? NSScreen.main
            ?? NSScreen.screens.first
    }

    private func panelSize(for screen: NSScreen, forceExpanded: Bool? = nil) -> CGSize {
        geometry(for: screen, forceExpanded: forceExpanded).panelSize
    }

    private func geometry(
        for screen: NSScreen,
        forceExpanded: Bool? = nil
    ) -> IslandInteractionGeometry {
        IslandInteractionGeometry(input: IslandInteractionInput(
            screenFrame: screen.frame,
            notchSize: screen.deskmateNotchSize,
            hasPhysicalNotch: screen.deskmateHasPhysicalNotch,
            hasCompactPresence: hasCompactPresence(runtime) || forceExpanded == false,
            isExpanded: forceExpanded ?? runtime.isIslandExpanded,
            activeCount: max(runtime.sessions.count, runtime.approvals.count)
        ))
    }

    private func hasCompactPresence(_ runtime: DeskmateMenuBarRuntime) -> Bool {
        if runtime.bridgeState != .connected { return true }
        if !runtime.sessions.isEmpty || !runtime.approvals.isEmpty { return true }
        guard let kind = runtime.island?.state.kind else { return false }
        switch kind {
        case .compact, .empty:
            return false
        case .liveActivity, .notificationCard, .sessionList:
            return true
        }
    }

    private func geometryForCurrentWindow() -> IslandInteractionGeometry? {
        guard let screen = targetScreen() else { return nil }
        return geometry(for: screen)
    }

    private func applyPanelFrame(forceExpanded: Bool) {
        guard let w = window,
              let screen = targetScreen(),
              let host = w.contentView as? IslandHostingView<IslandOverlay>
        else { return }
        let size = panelSize(for: screen, forceExpanded: forceExpanded)
        let rect = NSRect(
            x: screen.frame.midX - size.width / 2,
            y: screen.frame.maxY - size.height,
            width: size.width,
            height: size.height
        )
        host.frame = NSRect(origin: .zero, size: size)
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0
            context.allowsImplicitAnimation = false
            w.setFrame(rect, display: true)
        }
    }

    private func isExpandedPanel(window w: NSPanel) -> Bool {
        guard let screen = targetScreen() else { return false }
        let expanded = panelSize(for: screen, forceExpanded: true)
        return abs(w.frame.width - expanded.width) < 1
            && abs(w.frame.height - expanded.height) < 1
    }

    private func publishDiagnostics() {
        guard let screen = targetScreen() else {
            runtime.updateIslandDiagnostics("screen=<none>")
            return
        }
        let name = screen.localizedName
        runtime.updateIslandDiagnostics(geometry(for: screen).diagnostics(screenName: name))
    }

    // MARK: - Mouse monitors

    private func installMouseMonitors() {
        guard localMouseMonitor == nil, globalMouseMonitor == nil else { return }
        localMouseMonitor = NSEvent.addLocalMonitorForEvents(
            matching: [.mouseMoved, .leftMouseDown, .rightMouseDown, .otherMouseDown]
        ) { [weak self] event in
            self?.handleMouseEvent(event, isGlobal: false)
            return event
        }
        globalMouseMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.mouseMoved, .leftMouseDown, .rightMouseDown, .otherMouseDown]
        ) { [weak self] event in
            self?.handleMouseEvent(event, isGlobal: true)
        }
    }

    private func removeMouseMonitors() {
        hoverOpenWorkItem?.cancel()
        hoverCloseWorkItem?.cancel()
        hoverOpenWorkItem = nil
        hoverCloseWorkItem = nil
        if let localMouseMonitor {
            NSEvent.removeMonitor(localMouseMonitor)
        }
        if let globalMouseMonitor {
            NSEvent.removeMonitor(globalMouseMonitor)
        }
        localMouseMonitor = nil
        globalMouseMonitor = nil
    }

    private func handleMouseEvent(_ event: NSEvent, isGlobal: Bool) {
        guard window != nil else { return }
        if event.type == .mouseMoved {
            let now = CACurrentMediaTime()
            guard now - lastMouseMoveAt > 0.05 else { return }
            lastMouseMoveAt = now
            updateHoverState(for: NSEvent.mouseLocation)
            return
        }
        handleClick(at: NSEvent.mouseLocation)
    }

    private func updateHoverState(for screenPoint: NSPoint) {
        guard let localPoint = panelLocalPoint(for: screenPoint, margin: 32),
              let geometry = geometryForCurrentWindow()
        else {
            hoverOpenWorkItem?.cancel()
            scheduleHoverClose()
            return
        }
        let inside = geometry.hoverActivationRectInPanel.contains(localPoint)
        if inside {
            hoverCloseWorkItem?.cancel()
            scheduleHoverOpen()
        } else {
            hoverOpenWorkItem?.cancel()
            scheduleHoverClose()
        }
    }

    private func handleClick(at screenPoint: NSPoint) {
        guard runtime.isIslandExpanded else { return }
        guard let localPoint = panelLocalPoint(for: screenPoint, margin: 0),
              let geometry = geometryForCurrentWindow(),
              geometry.surfaceRectInPanel.contains(localPoint)
        else {
            runtime.closeIslandSessionList(source: .island)
            return
        }
    }

    private func scheduleHoverOpen() {
        guard !runtime.isIslandExpanded else { return }
        hoverOpenWorkItem?.cancel()
        let item = DispatchWorkItem { [weak self] in
            self?.applyPanelFrame(forceExpanded: true)
            self?.runtime.openIslandSessionList()
        }
        hoverOpenWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.20, execute: item)
    }

    private func scheduleHoverClose() {
        guard runtime.isIslandExpanded else { return }
        hoverCloseWorkItem?.cancel()
        let item = DispatchWorkItem { [weak self] in
            self?.runtime.closeIslandSessionList(source: .island)
        }
        hoverCloseWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.14, execute: item)
    }

    private func panelLocalPoint(for screenPoint: NSPoint, margin: CGFloat) -> NSPoint? {
        guard let w = window else { return nil }
        let frame = w.frame
        guard frame.insetBy(dx: -margin, dy: -margin).contains(screenPoint) else {
            return nil
        }
        return NSPoint(x: screenPoint.x - frame.minX, y: screenPoint.y - frame.minY)
    }
}

private final class IslandHostingView<Content: View>: NSHostingView<Content> {
    private weak var runtime: DeskmateMenuBarRuntime?

    init(rootView: Content, runtime: DeskmateMenuBarRuntime) {
        self.runtime = runtime
        super.init(rootView: rootView)
        configureTransparency()
    }

    required init(rootView: Content) {
        super.init(rootView: rootView)
        configureTransparency()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var isOpaque: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeKey()
        super.mouseDown(with: event)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        configureTransparency()
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let runtime,
              interactionRect(for: runtime, in: bounds).contains(point)
        else { return nil }
        return super.hitTest(point) ?? self
    }

    private func configureTransparency() {
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    private func interactionRect(
        for runtime: DeskmateMenuBarRuntime,
        in bounds: NSRect
    ) -> NSRect {
        let screen = NSScreen.screens.first(where: { $0.deskmateHasPhysicalNotch })
            ?? NSScreen.main
        let geometry = IslandInteractionGeometry(input: IslandInteractionInput(
            screenFrame: screen?.frame ?? CGRect(origin: .zero, size: bounds.size),
            notchSize: screen?.deskmateNotchSize ?? CGSize(width: 224, height: 24),
            hasPhysicalNotch: screen?.deskmateHasPhysicalNotch == true,
            hasCompactPresence: hasCompactPresence(runtime),
            isExpanded: runtime.isIslandExpanded,
            activeCount: max(runtime.sessions.count, runtime.approvals.count)
        ))
        return geometry.surfaceRectInPanel
    }

    private func hasCompactPresence(_ runtime: DeskmateMenuBarRuntime) -> Bool {
        if runtime.bridgeState != .connected { return true }
        if !runtime.sessions.isEmpty || !runtime.approvals.isEmpty { return true }
        guard let kind = runtime.island?.state.kind else { return false }
        switch kind {
        case .compact, .empty:
            return false
        case .liveActivity, .notificationCard, .sessionList:
            return true
        }
    }

    private func compactLabel(for runtime: DeskmateMenuBarRuntime) -> String {
        switch runtime.bridgeState {
        case .stopped:
            return "Deskmate offline"
        case .connecting:
            return "connecting..."
        case .waitingForRetry(let attempt, _):
            return "retry #\(attempt)"
        case .connected:
            if runtime.approvals.count > 0 { return "needs approval" }
            if let active = runtime.domain.activeSessionId,
               let session = runtime.sessions.first(where: { $0.sessionId == active }) {
                return "\(session.phaseLabel): \(session.displayTitle)"
            }
            return "Deskmate"
        }
    }
}
#endif
