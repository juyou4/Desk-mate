#if canImport(AppKit)
import AppKit
import Combine
import CoreGraphics
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
    private var customizationUnsub: (() -> Void)?
    private var islandCancellable: AnyCancellable?
    private var sessionsCancellable: AnyCancellable?
    private var approvalsCancellable: AnyCancellable?
    private var localMouseMonitor: Any?
    private var globalMouseMonitor: Any?
    /// V10 island polish: observer for
    /// ``NSApplication.didChangeScreenParametersNotification``. Fires
    /// when the user hot-plugs an external display, toggles
    /// resolution, or returns from a clamshell — we re-pick the
    /// preferred notched screen and reposition the panel so the
    /// island never ends up offscreen.
    private var screenParametersObserver: NSObjectProtocol?
    private var hoverOpenWorkItem: DispatchWorkItem?
    private var hoverCloseWorkItem: DispatchWorkItem?
    /// V10 island polish: 100ms grace period after the cursor leaves
    /// the hover-activation rect during which we *don't* cancel the
    /// pending ``hoverOpenWorkItem``. Avoids restarting the 200ms
    /// open timer every time the mouse jitters across the notch
    /// edge — open-vibe-island's `OverlayPanelController` does the
    /// same thing.
    private var hoverOpenCancelGrace: DispatchWorkItem?
    private var lastMouseMoveAt: CFTimeInterval = 0
    /// V10 island polish: single source of truth for hover delays
    /// and AppKit panel-frame durations. Tests in the smoke
    /// binary lock the maths in
    /// :class:`IslandAnimationTuning` directly so the controller
    /// stays a thin wrapper around well-tested values.
    private static let tuning = IslandAnimationTuning.default

    /// V10 I7 hoverSpeed: the user-facing customization scales the
    /// hover-open delay. ``hoverSpeed == 1.0`` keeps the default
    /// 200 ms cadence (matches macOS Dynamic Island feel); a higher
    /// value shortens the wait, a lower value lengthens it.
    private var hoverOpenDelay: TimeInterval {
        Self.tuning.resolvedHoverOpenDelay(
            hoverSpeed: runtime.topSurfaceCustomization.current.hoverSpeed
        )
    }

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
        // V11 architecture polish: ViewModel that collapses the
        // runtime's many @Published fields into IslandStatus +
        // IslandContent. Owned by the controller for the lifetime
        // of the panel so SwiftUI doesn't recreate it on every
        // resize.
        let viewModel = IslandViewModel(runtime: runtime)
        let host = IslandHostingView(
            rootView: IslandOverlay(runtime: runtime, viewModel: viewModel),
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
        // V10 island polish: hide the panel from screen-capture /
        // ScreenCaptureKit so users sharing their screen on Zoom /
        // OBS don't see the island float over their content. macOS
        // honours this for borderless panels at ``.statusBar``
        // level.
        w.sharingType = .none
        // V10 island polish #1 (MioIsland-inspired): start at the
        // collapsed level. ``resizeForCurrentState`` will elevate to
        // ``.popUpMenu`` whenever the island opens. Sitting at
        // ``.mainMenu + 3`` keeps the pill above the menu bar
        // background but below status bar items so clicks reach the
        // system menus.
        w.level = .mainMenu + 3
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
        // V10 polish (NotchDrop-inspired): slide the panel down from
        // above the screen on first appearance instead of just popping
        // in. Uses NSAnimationContext on setFrame; the visible content
        // animation is left to SwiftUI's built-in spring on the surface.
        performInitialSlideIn(window: w)
        w.orderFrontRegardless()

        self.window = w
        installResizeSubscriptions()
        installMouseMonitors()
        installScreenParametersObserver()
        publishDiagnostics()
    }

    func close() {
        removeMouseMonitors()
        removeScreenParametersObserver()
        window?.orderOut(nil)
        window = nil
        islandCancellable = nil
        sessionsCancellable = nil
        approvalsCancellable = nil
        // V10 I7: drop the customization subscription so we don't
        // leak a closure into ``TopSurfaceCustomizationStore`` after
        // the controller has gone away (e.g. the user dropped to
        // degradation level 5 and the panel was orderedOut).
        customizationUnsub?()
        customizationUnsub = nil
        // Drop the policy subscription as well so a fresh
        // ``install()`` rebuilds the chain instead of stacking
        // multiple sinks. ``installPolicySubscriptionIfNeeded`` is
        // idempotent on its own, but keeping the lifecycle
        // symmetric avoids surprises if the controller is reused.
        policyCancellable = nil
    }

    // MARK: - Degradation policy subscription

    private func installPolicySubscriptionIfNeeded() {
        guard policyCancellable == nil else { return }
        policyCancellable = runtime.$degradationPolicy
            .receive(on: DispatchQueue.main)
            .sink { [weak self] policy in
                self?.applyDegradation(policy)
            }
        // V10 I7: a hardwareNotchMode flip rebuilds the panel
        // frame so the user immediately sees the floating-bar /
        // notch transition without restarting the app.
        if customizationUnsub == nil {
            customizationUnsub = runtime.topSurfaceCustomization.subscribe { [weak self] _ in
                Task { @MainActor in self?.relayoutForCustomization() }
            }
        }
    }

    private func relayoutForCustomization() {
        guard window != nil else { return }
        // Use ``forceExpanded == nil`` so the controller reads the
        // runtime's current expanded state — same path the
        // session/approval subscriptions use.
        resizeForCurrentState()
    }

    private func installResizeSubscriptions() {
        guard islandCancellable == nil else { return }
        islandCancellable = runtime.$island
            .receive(on: DispatchQueue.main)
            .sink { [weak self] change in
                guard let self else { return }
                // R10: Emit feedback on notification_card entry
                if let change,
                   change.state.kind == .notificationCard,
                   change.transition == .slideIn || change.transition == .morph {
                    self.emitFeedback(priority: change.priority)
                }
                self.resizeForCurrentState()
            }
        sessionsCancellable = runtime.$sessions
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.resizeForCurrentState() }
        approvalsCancellable = runtime.$approvals
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.resizeForCurrentState() }
    }

    // MARK: - Feedback (R10)

    /// R10: Emit haptic and optional audio feedback for high-salience events.
    /// Only P0 and P1 notification_card entries trigger feedback; dismissals
    /// do not (R10.5). Skipped entirely when degradation >= 4 (R10.4).
    private func emitFeedback(priority: Priority) {
        // R10.4: No feedback when degradation >= 4
        guard runtime.degradationPolicy.level < DegradationPolicy.levelHideHUD else { return }
        // R10.1: Only P0 and P1 trigger feedback
        guard priority == .p0 || priority == .p1 else { return }

        // Haptic feedback (R10.1, R10.7: skip if hardware unavailable)
        NSHapticFeedbackManager.defaultPerformer.perform(
            .levelChange,
            performanceTime: .now
        )

        // Audio feedback (R10.2)
        let prefs = runtime.topSurfaceCustomization.current.feedback
        if prefs.audio {
            // V10 #9: when audio is enabled, fall back to "Tink" if
            // the user hasn't picked a specific sound. Tink is built
            // into macOS so the lookup is guaranteed to succeed.
            let name = prefs.audioName?.trimmingCharacters(in: .whitespaces).isEmpty == false
                ? prefs.audioName!
                : "Tink"
            NSSound(named: NSSound.Name(name))?.play()
        }
        // R10.6: If audioName doesn't resolve, haptic still fired above — no error surfaced
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
        let size = panelSize(for: screen, forceExpanded: runtime.isIslandExpanded)
        let maxWidth = panelSize(for: screen, forceExpanded: true).width
        let rect = NSRect(
            x: screen.frame.midX - maxWidth / 2,
            y: screen.frame.maxY - size.height,
            width: maxWidth,
            height: size.height
        )
        w.setFrame(rect, display: true)
    }

    /// V10 polish: NotchDrop-style slide-in. Off-screen above the
    /// notch, then animates down to the docked y position over 0.45s.
    /// Run once on initial install so users see the pill "drop"
    /// instead of pop. Uses NSAnimationContext directly because
    /// SwiftUI's animation only kicks in once the view is mounted.
    private func performInitialSlideIn(window w: NSPanel) {
        guard let screen = targetScreen() else { return }
        let finalFrame = w.frame
        // Start position: panel pushed up so it's just above the
        // visible top of the screen.
        let startFrame = NSRect(
            x: finalFrame.origin.x,
            y: screen.frame.maxY,
            width: finalFrame.width,
            height: finalFrame.height
        )
        w.setFrame(startFrame, display: false)
        DispatchQueue.main.async {
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.45
                ctx.timingFunction = CAMediaTimingFunction(
                    controlPoints: 0.16, 1.0, 0.3, 1.0
                ) // easeOutExpo — boring.notch fallback curve
                w.animator().setFrame(finalFrame, display: true)
            }
        }
    }

    private func resizeForCurrentState() {
        guard let w = window,
              w.contentView is IslandHostingView<IslandOverlay>
        else { return }

        let expanded = runtime.isIslandExpanded
        applyPanelFrame(forceExpanded: expanded, animated: true)
        w.ignoresMouseEvents = !expanded
        // V10 island polish #1 (MioIsland-inspired): switch window
        // level based on collapsed/opened. When collapsed we sit just
        // above the menu bar background but BELOW menu bar items so
        // status bar clicks reach the system. When opened we elevate
        // above everything so buttons inside the panel are reachable.
        w.level = expanded ? .popUpMenu : (.mainMenu + 3)
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
        // V10 I7 hardwareNotchMode:
        // - .automatic — prefer the built-in notched display, fall
        //   back to ``NSScreen.main`` (current default).
        // - .forceNotched — same as automatic; skipping the option
        //   merely keeps the auto path explicit when the user has
        //   pinned the choice.
        // - .forceFlat — pretend no notch is present; pick
        //   ``NSScreen.main`` so the island renders as a floating
        //   bar even on a MacBook with a real notch.
        let mode = runtime.topSurfaceCustomization.current.hardwareNotchMode
        if mode == .forceFlat {
            return NSScreen.main ?? NSScreen.screens.first
        }

        // R9.3: Check preferredScreenId first — if the user pinned
        // the island to a specific display via the settings sheet,
        // honour that choice as long as the display is connected.
        if let preferredId = runtime.topSurfaceCustomization.current.preferredScreenId,
           !preferredId.isEmpty {
            if let match = NSScreen.screens.first(where: { stableScreenId(for: $0) == preferredId }) {
                return match
            }
            // R9.4: Preferred screen not connected — fall through to
            // the existing notch → main → first fallback chain.
        }

        // Existing fallback: notched screen → main screen → first screen
        return NSScreen.screens.first(where: { $0.deskmateHasPhysicalNotch })
            ?? NSScreen.main
            ?? NSScreen.screens.first
    }

    /// Returns a hardware-stable screen identifier using
    /// ``CGDisplayCreateUUIDFromDisplayID``. This UUID survives
    /// hot-plug, sleep/wake cycles, and process relaunches — making
    /// it suitable for persisting a user's preferred target screen.
    /// R9.1, R9.3
    private func stableScreenId(for screen: NSScreen) -> String? {
        guard let screenNumber = screen.deviceDescription[
            NSDeviceDescriptionKey("NSScreenNumber")
        ] as? CGDirectDisplayID else {
            return nil
        }
        guard let uuid = CGDisplayCreateUUIDFromDisplayID(screenNumber) else {
            return nil
        }
        return CFUUIDCreateString(nil, uuid.takeUnretainedValue()) as String
    }

    private func panelSize(for screen: NSScreen, forceExpanded: Bool? = nil) -> CGSize {
        geometry(for: screen, forceExpanded: forceExpanded).panelSize
    }

    private func geometry(
        for screen: NSScreen,
        forceExpanded: Bool? = nil
    ) -> IslandInteractionGeometry {
        let mode = runtime.topSurfaceCustomization.current.hardwareNotchMode
        let notchSize: CGSize
        let hasNotch: Bool
        if mode == .forceFlat {
            // Pretend the screen is flat — render a floating bar
            // of fixed size centred at the top.
            notchSize = CGSize(width: 224, height: 28)
            hasNotch = false
        } else {
            notchSize = screen.deskmateNotchSize
            hasNotch = screen.deskmateHasPhysicalNotch
        }
        return IslandInteractionGeometry(input: IslandInteractionInput(
            screenFrame: screen.frame,
            notchSize: notchSize,
            hasPhysicalNotch: hasNotch,
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

    private func applyPanelFrame(forceExpanded: Bool, animated: Bool = true) {
        guard let w = window,
              let screen = targetScreen(),
              let host = w.contentView as? IslandHostingView<IslandOverlay>
        else { return }
        let size = panelSize(for: screen, forceExpanded: forceExpanded)
        // Pin panel x to the maximum (expanded) width so the panel
        // doesn't shift horizontally when reminder/notification
        // arrives. Panel width still follows state so AppKit only
        // animates dimensions that actually change.
        let maxWidth = panelSize(for: screen, forceExpanded: true).width
        let rect = NSRect(
            x: screen.frame.midX - maxWidth / 2,
            y: screen.frame.maxY - size.height,
            width: maxWidth,
            height: size.height
        )
        host.frame = NSRect(origin: .zero, size: rect.size)
        let duration = Self.tuning.panelFrameDuration(
            forceExpanded: forceExpanded, animated: animated
        )
        if animated && duration > 0 {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = duration
                context.timingFunction = CAMediaTimingFunction(name: .default)
                context.allowsImplicitAnimation = true
                w.animator().setFrame(rect, display: true)
            }
        } else {
            w.setFrame(rect, display: true)
        }
    }

    private func isExpandedPanel(window w: NSPanel) -> Bool {
        guard let screen = targetScreen() else { return false }
        let expanded = panelSize(for: screen, forceExpanded: true)
        return abs(w.frame.width - expanded.width) < 1
            && abs(w.frame.height - expanded.height) < 1
    }

    /// Diagnostic-only helper — exposed for tests so they can
    /// confirm the controller pinned the panel at the right size.
    var debugPanelSize: CGSize? {
        window?.frame.size
    }

    private func publishDiagnostics() {
        guard let screen = targetScreen() else {
            runtime.updateIslandDiagnostics("screen=<none>")
            return
        }
        let name = screen.localizedName
        runtime.updateIslandDiagnostics(geometry(for: screen).diagnostics(screenName: name))
    }

    // MARK: - Screen parameters

    /// Re-pick the target screen and re-layout the panel. Called by
    /// the screen-parameters notification listener and exposed for
    /// tests so they can drive the re-layout without spinning a
    /// real ``NSApplication`` notification.
    func noteScreenParametersChanged() {
        guard window != nil else { return }
        // Defer one runloop turn so that ``NSScreen.screens`` has
        // settled — macOS posts the notification *during* the
        // hardware switch and the array can momentarily contain a
        // stale display.
        DispatchQueue.main.async { [weak self] in
            guard let self, self.window != nil else { return }
            self.repositionForCurrentScreen()
        }
    }

    private func installScreenParametersObserver() {
        guard screenParametersObserver == nil else { return }
        screenParametersObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.noteScreenParametersChanged() }
        }
    }

    private func removeScreenParametersObserver() {
        if let screenParametersObserver {
            NotificationCenter.default.removeObserver(screenParametersObserver)
        }
        screenParametersObserver = nil
    }

    private func repositionForCurrentScreen() {
        guard let w = window,
              let host = w.contentView as? IslandHostingView<IslandOverlay>
        else { return }
        // Screen hot-plug must teleport instantly — animating from
        // the old display's coordinates to the new one would draw
        // a confusing mid-screen ghost frame.
        positionAtTopCenter(window: w)
        let expanded = runtime.isIslandExpanded
        applyPanelFrame(forceExpanded: expanded, animated: false)
        host.frame = panelFrame(forceExpanded: expanded)
        w.ignoresMouseEvents = !expanded
        w.level = expanded ? .popUpMenu : (.mainMenu + 3)
        publishDiagnostics()
    }

    // MARK: - Mouse monitors

    private func installMouseMonitors() {
        guard localMouseMonitor == nil, globalMouseMonitor == nil else { return }
        localMouseMonitor = NSEvent.addLocalMonitorForEvents(
            matching: [.mouseMoved, .leftMouseDown, .rightMouseDown, .otherMouseDown]
        ) { [weak self] event in
            guard let self else { return event }
            return self.handleLocalMouseEvent(event) ? nil : event
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
        hoverOpenCancelGrace?.cancel()
        hoverOpenWorkItem = nil
        hoverCloseWorkItem = nil
        hoverOpenCancelGrace = nil
        if let localMouseMonitor {
            NSEvent.removeMonitor(localMouseMonitor)
        }
        if let globalMouseMonitor {
            NSEvent.removeMonitor(globalMouseMonitor)
        }
        localMouseMonitor = nil
        globalMouseMonitor = nil
    }

    private func handleLocalMouseEvent(_ event: NSEvent) -> Bool {
        guard window != nil else { return false }
        if event.type == .mouseMoved {
            handleMouseEvent(event, isGlobal: false)
            return false
        }
        if consumeExpandedPassthroughClick(event) {
            return true
        }
        handleMouseEvent(event, isGlobal: false)
        return false
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

    private func consumeExpandedPassthroughClick(_ event: NSEvent) -> Bool {
        guard runtime.isIslandExpanded,
              let w = window,
              event.window === w,
              let localPoint = panelLocalPoint(for: NSEvent.mouseLocation, margin: 0),
              let geometry = geometryForCurrentWindow(),
              geometry.shouldPassthroughExpandedClick(localPoint: localPoint)
        else { return false }

        runtime.closeIslandSessionList(source: .island)
        w.ignoresMouseEvents = true
        w.level = .mainMenu + 3
        repostPassthroughClick(event)
        return true
    }

    private func repostPassthroughClick(_ event: NSEvent) {
        guard let cgEvent = event.cgEvent?.copy() else { return }
        DispatchQueue.main.async {
            cgEvent.post(tap: .cghidEventTap)
        }
    }

    private func updateHoverState(for screenPoint: NSPoint) {
        guard let localPoint = panelLocalPoint(for: screenPoint, margin: 32),
              let geometry = geometryForCurrentWindow()
        else {
            // Pointer left the panel altogether. Start the cancel
            // grace so a brief excursion doesn't kill an in-flight
            // open timer; if the cursor doesn't return within the
            // grace period we'll commit to closing.
            scheduleHoverOpenCancelGrace()
            scheduleHoverClose()
            return
        }
        let inside = geometry.hoverActivationRectInPanel.contains(localPoint)
        if inside {
            // Cancel any pending close + cancel-grace on re-entry.
            hoverCloseWorkItem?.cancel()
            hoverCloseWorkItem = nil
            hoverOpenCancelGrace?.cancel()
            hoverOpenCancelGrace = nil
            scheduleHoverOpen()
        } else {
            scheduleHoverOpenCancelGrace()
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
        // R5.5: If a SneakPeek/transient is active, promote immediately
        // on hover instead of scheduling the normal hover-open delay.
        if runtime.shell.islandSurface.isTransientActive {
            runtime.shell.islandSurface.promoteSneakPeek()
            runtime.openIslandSessionList()
            return
        }
        guard !runtime.isIslandExpanded else { return }
        // Idempotent: an in-flight timer is reused so the user
        // doesn't pay an extra 200 ms whenever the mouse twitches
        // back into the activation rect during the same open burst.
        guard hoverOpenWorkItem == nil else { return }
        let item = DispatchWorkItem { [weak self] in
            self?.hoverOpenWorkItem = nil
            self?.applyPanelFrame(forceExpanded: true)
            self?.runtime.openIslandSessionList()
        }
        hoverOpenWorkItem = item
        DispatchQueue.main.asyncAfter(
            deadline: .now() + hoverOpenDelay, execute: item
        )
    }

    /// Schedule the cancel-grace work item that, after its delay,
    /// kills any in-flight ``hoverOpenWorkItem``. Re-entering the
    /// hover region while the grace is pending revokes it without
    /// touching the open timer, so brief mouse jitter at the notch
    /// edge no longer restarts the 200 ms wait.
    private func scheduleHoverOpenCancelGrace() {
        guard hoverOpenWorkItem != nil else { return }
        guard hoverOpenCancelGrace == nil else { return }
        let grace = DispatchWorkItem { [weak self] in
            self?.hoverOpenWorkItem?.cancel()
            self?.hoverOpenWorkItem = nil
            self?.hoverOpenCancelGrace = nil
        }
        hoverOpenCancelGrace = grace
        DispatchQueue.main.asyncAfter(
            deadline: .now() + Self.tuning.hoverOpenCancelGrace,
            execute: grace
        )
    }

    private func scheduleHoverClose() {
        guard runtime.isIslandExpanded else { return }
        hoverCloseWorkItem?.cancel()
        let item = DispatchWorkItem { [weak self] in
            self?.hoverCloseWorkItem = nil
            self?.runtime.closeIslandSessionList(source: .island)
        }
        hoverCloseWorkItem = item
        DispatchQueue.main.asyncAfter(
            deadline: .now() + Self.tuning.hoverCloseDelay, execute: item
        )
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
        guard let runtime else { return nil }
        let rect: NSRect
        if runtime.isIslandExpanded {
            rect = interactionRect(for: runtime, in: bounds)
        } else {
            rect = IslandInteractionGeometry.collapsedHitBandRect(in: bounds)
        }
        guard rect.contains(point) else { return nil }
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
        let mode = runtime.topSurfaceCustomization.current.hardwareNotchMode
        let screen: NSScreen?
        let notchSize: CGSize
        let hasNotch: Bool
        if mode == .forceFlat {
            screen = NSScreen.main ?? NSScreen.screens.first
            notchSize = CGSize(width: 224, height: 28)
            hasNotch = false
        } else {
            screen = NSScreen.screens.first(where: { $0.deskmateHasPhysicalNotch })
                ?? NSScreen.main
            notchSize = screen?.deskmateNotchSize ?? CGSize(width: 224, height: 24)
            hasNotch = screen?.deskmateHasPhysicalNotch == true
        }
        let geometry = IslandInteractionGeometry(input: IslandInteractionInput(
            screenFrame: screen?.frame ?? CGRect(origin: .zero, size: bounds.size),
            notchSize: notchSize,
            hasPhysicalNotch: hasNotch,
            hasCompactPresence: hasCompactPresence(runtime),
            isExpanded: runtime.isIslandExpanded,
            activeCount: max(runtime.sessions.count, runtime.approvals.count)
        ))
        // Panel is always at expanded size. The surface rect must be
        // positioned relative to the expanded panel, not the compact one.
        let expandedPanelSize = geometry.panelSize
        let surfaceSize = geometry.surfaceSize
        // Surface is centered horizontally in the panel, pinned to top.
        let surfaceX = (expandedPanelSize.width - surfaceSize.width) / 2
        let surfaceY = expandedPanelSize.height - surfaceSize.height
        let surfaceRect = NSRect(
            x: surfaceX,
            y: surfaceY,
            width: surfaceSize.width,
            height: surfaceSize.height
        )
        // Expand the hit area slightly for easier targeting
        return surfaceRect.insetBy(dx: -18, dy: -8)
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
