import SwiftUI
import DeskmateCore
#if canImport(AppKit)
import AppKit
#endif

/// SwiftUI ``MenuBarExtra`` entry point (V10 Phase 11d-v).
///
/// Runs with ``setActivationPolicy(.accessory)`` so the binary appears
/// only as a menu-bar icon and doesn't take a Dock slot. The full
/// runtime (bridge + stores + perception sampler) lives in a
/// :class:`DeskmateMenuBarRuntime` that SwiftUI observes.
@main
struct DeskmateMenuBarApp: App {
    #if canImport(AppKit)
    @NSApplicationDelegateAdaptor(DeskmateAppDelegate.self)
    private var appDelegate
    #endif

    init() {
        #if canImport(AppKit)
        NSApplication.shared.setActivationPolicy(.accessory)
        #endif
    }

    var body: some Scene {
        MenuBarExtra(menuBarText, systemImage: menuBarIconName) {
            DeskmateMenuContent(runtime: runtime)
                .onAppear {
                    // V10 §3.1 row 6: latch wake-to-first-frame on
                    // the first SwiftUI render. After the initial
                    // launch this is a no-op (no pending wake);
                    // after a system sleep/wake cycle the menu bar
                    // re-renders and we capture the elapsed time.
                    runtime.markFirstFrame()
                }
        }
        .menuBarExtraStyle(.window)
    }

    private var runtime: DeskmateMenuBarRuntime {
        #if canImport(AppKit)
        appDelegate.runtime
        #else
        DeskmateMenuBarRuntime()
        #endif
    }

    /// Visible status-bar text — a short, always-rendering fallback so
    /// even if ``systemImage`` fails to resolve on the current macOS
    /// build, *something* is clickable. Also doubles as a connection
    /// health indicator.
    private var menuBarText: String {
        switch runtime.bridgeState {
        case .connected:
            let pending = runtime.approvals.count
            return pending > 0 ? "DM●\(pending)" : "DM"
        case .connecting, .waitingForRetry: return "DM…"
        case .stopped: return "DM!"
        }
    }

    /// SF Symbol name. Using ``circle.hexagongrid.fill`` because it
    /// ships with macOS 13+ and renders reliably in status bars.
    private var menuBarIconName: String {
        switch runtime.bridgeState {
        case .connected: return "circle.hexagongrid.fill"
        case .connecting, .waitingForRetry: return "circle.dotted"
        case .stopped: return "circle.slash"
        }
    }
}

#if canImport(AppKit)
@MainActor
final class DeskmateAppDelegate: NSObject, NSApplicationDelegate {
    let runtime = DeskmateMenuBarRuntime()
    private var islandController: IslandWindowController?
    private var petController: PetWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installOverlays()
    }

    private func installOverlays() {
        if islandController == nil {
            let ctrl = IslandWindowController(runtime: runtime)
            ctrl.install()
            islandController = ctrl
        }
        if petController == nil {
            let ctrl = PetWindowController(runtime: runtime)
            ctrl.install()
            petController = ctrl
        }
    }
}
#endif
