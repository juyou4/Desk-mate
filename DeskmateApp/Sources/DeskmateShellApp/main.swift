//  DeskmateShellApp/main.swift
//
//  Minimal daemon-style entry point that boots the full Swift runtime
//  (DeskmateShell + PerceptionSampler) against the real Unix domain
//  socket at ~/Library/Application Support/Deskmate/ipc.sock. V10
//  Phase 11d-ii.
//
//  This is intentionally CLI-shaped, not a SwiftUI .app:
//
//  * Run with ``swift run DeskmateShellApp`` from the DeskmateApp
//    directory.
//  * If the Python agent is running, you'll see bridge transition
//    from ``.connecting`` to ``.connected`` and live DomainState
//    deltas printed as they arrive.
//  * If the Python agent is **not** running, the ReconnectingBridge
//    settles into ``.waitingForRetry`` and retries with exponential
//    backoff — a good demo of the self-healing logic.
//
//  Ctrl-C to stop. No menu bar UI, no pet overlay; those arrive in a
//  subsequent phase.
import Foundation
import DeskmateCore

#if canImport(AppKit)
import AppKit
#endif

let log = DateFormatter()
log.dateFormat = "HH:mm:ss.SSS"
func stamp() -> String { log.string(from: Date()) }

print("[\(stamp())] DeskmateShellApp boot")

let socketPath = DefaultSocketPath.current()
print("[\(stamp())] socket: \(socketPath)")

// Dedicated *serial* callback queue — using ``.global`` here would
// let state transitions and store fan-outs reorder under load
// (observed as "connected" landing before "connecting" in the live
// logs). A single serial queue keeps every observer seeing events
// in causal order.
let callbackQueue = DispatchQueue(
    label: "deskmate.shell.callbacks", qos: .userInitiated
)

let shell = DeskmateShell(
    configuration: .init(
        socketPath: socketPath,
        bridgeBackoff: .init(
            initialBackoff: 0.5,
            maxBackoff: 10.0,
            multiplier: 2.0,
            jitterFraction: 0.2
        )
    ),
    callbackQueue: callbackQueue
)

let frontmostProvider = CachedFrontmostAppProvider(
    provider: DefaultPerceptionProviders.frontmostApp
)
let sampler = PerceptionSampler(
    sender: shell,
    configuration: .init(
        tickInterval: 2.0,
        heartbeatInterval: 30.0,
        idleSecondsForIdleState: 30.0,
        idleProvider: DefaultPerceptionProviders.idleSeconds,
        frontmostAppProvider: { frontmostProvider() }
    )
)

// V10 §3.1 row 6 + row 8 — observe wake notifications + drive a
// CVDisplayLink so the agent gets real perf.metrics envelopes
// even from this daemon-shaped binary. Same wiring as the menu
// bar app, so a contributor can sanity-check the budget pipeline
// without a SwiftUI run.
#if canImport(AppKit)
let perfMetrics = PerfMetricsBinding(
    center: NSWorkspace.shared.notificationCenter,
    wakeNotificationName: NSWorkspace.didWakeNotification,
    sender: { [weak shell] in shell },
    frameTickerSource: CVDisplayLinkFrameTicker()
)
#else
let perfMetrics = PerfMetricsBinding(
    sender: { [weak shell] in shell },
    frameTickerSource: CVDisplayLinkFrameTicker()
)
#endif

// --- Observers --------------------------------------------------------------

shell.bridge.onStateChange { state in
    print("[\(stamp())] bridge → \(state)")
}

shell.domainState.subscribe { s in
    print(
        "[\(stamp())] domain pending=\(s.pendingApprovals)"
            + " mood=\(s.agentMood.rawValue)"
            + " session=\(s.activeSessionId ?? "-")"
    )
}

shell.bubbleQueue.subscribe { q in
    if let current = q.peek(nowMs: Int(Date().timeIntervalSince1970 * 1000)) {
        print(
            "[\(stamp())] bubble → id=\(current.id)"
                + " kind=\(current.kind.rawValue)"
                + " text=\"\(current.text)\""
        )
    } else {
        print("[\(stamp())] bubble → (empty)")
    }
}

shell.islandSurface.subscribe { change in
    print(
        "[\(stamp())] island → \(change.state.kind.rawValue)"
            + " prio=\(change.priority.rawValue)"
            + " via=\(change.transition.rawValue)"
    )
}

shell.sessionList.subscribe { rows in
    let ids = rows.map(\.sessionId)
    print("[\(stamp())] sessions[\(rows.count)]=\(ids)")
}
shell.pendingReminders.subscribe { rows in
    let ids = rows.map(\.reminderId)
    print("[\(stamp())] reminders[\(rows.count)]=\(ids)")
}
shell.pendingApprovals.subscribe { rows in
    let brief = rows.map { "\($0.approvalId)(\($0.prompt.prefix(20)))" }
    print("[\(stamp())] approvals[\(rows.count)]=\(brief)")
}

// --- Lifecycle --------------------------------------------------------------

#if canImport(AppKit)
// Hide the Dock icon so this daemon-shaped binary doesn't pollute the
// user's Dock when running under ``swift run``. ``NSApplication.shared``
// materializes the singleton (``NSApp``) if it hasn't been touched yet;
// raw ``NSApp`` reads crash on a plain command-line process.
_ = NSApplication.shared.setActivationPolicy(.accessory)
#endif

shell.start()
sampler.start()
perfMetrics.start(pushInterval: 5.0)
// Daemon has no SwiftUI scene to call markFirstFrame from, so we
// latch the budget once after a tiny boot delay. The first
// snapshot still ends up with last_wake_seconds == nil unless an
// actual wake fires while the daemon runs.
DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
    perfMetrics.markFirstFrame()
}

// SIGINT → graceful shutdown.
let sigSource = DispatchSource.makeSignalSource(
    signal: SIGINT, queue: .main
)
sigSource.setEventHandler {
    print("\n[\(stamp())] shutting down")
    perfMetrics.stop()
    sampler.stop()
    shell.stop()
    exit(0)
}
sigSource.resume()
signal(SIGINT, SIG_IGN)  // DispatchSource takes over delivery

print("[\(stamp())] ready. Ctrl-C to stop.")

// Drive the main queue; GCD callbacks installed above will fire here
// alongside the SIGINT source.
dispatchMain()
