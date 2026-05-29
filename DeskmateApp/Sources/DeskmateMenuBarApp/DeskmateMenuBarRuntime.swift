import Foundation
import DeskmateCore
#if canImport(AppKit)
import AppKit
#endif

/// ObservableObject that owns the full ``DeskmateShell`` +
/// ``PerceptionSampler`` stack and mirrors its four stores into
/// ``@Published`` properties so SwiftUI views can react (V10 Phase
/// 11d-v).
///
/// All store subscriptions are installed on a serial dispatch queue
/// and hopped to the main queue before publishing — SwiftUI refuses
/// to observe off-main changes.
@MainActor
public final class DeskmateMenuBarRuntime: ObservableObject {
    @Published public var bridgeState: ReconnectingBridgeClient.State =
        .stopped
    @Published public var domain: DomainState = .init()
    @Published public var sessions: [SessionRow] = []
    @Published public var reminders: [ReminderRow] = []
    @Published public var approvals: [ApprovalRow] = []
    @Published public var island: LiveIslandSurfaceStore.ChangeEvent? = nil
    @Published public var currentBubble: BubbleSpec? = nil
    @Published public var chatHistory: [ChatEntry] = []
    @Published public var activeAvatarStyle: String = "pixel"
    @Published public var islandDiagnostics: String = "screen=<pending>"
    @Published public var islandSurfaceDiagnostics: String = "surface=<pending>"

    /// V10 island polish #10: live mutable registry of compact
    /// island modules. Default modules ship from
    /// ``IslandModuleRegistry.deskmateDefaultModules``; external
    /// agents can append additional modules via the
    /// ``register_module`` companion intent. Re-registering an id
    /// replaces the previous module so a hook can update its own
    /// template at runtime without leaking duplicates.
    @Published public var islandModules: IslandModuleRegistry =
        IslandModuleRegistry.deskmateDefaultModules()
    /// V10 Phase 9 · §4 — derived policy mirroring Python's
    /// ``DegradationController``. Recomputed every time
    /// ``DomainState.degradationLevel`` changes, so SwiftUI views /
    /// window controllers can read ``policy.hideHUD`` /
    /// ``policy.islandOrderOut`` / ``policy.effectiveFPS(base:)``
    /// without juggling the raw integer.
    @Published public var degradationPolicy: DegradationPolicy = .init()
    /// V10 I7: top-surface customization (theme, font scale, hover
    /// speed, hardware-notch mode). Subscribed by
    /// :class:`IslandWindowController` so toggling
    /// ``hardwareNotchMode`` to ``.forceVirtual`` repositions the
    /// island as a floating bar even on a real notched MBP. Updated
    /// by Settings UI / future bridge intents.
    public let topSurfaceCustomization = TopSurfaceCustomizationStore()

    public let shell: DeskmateShell
    public let sampler: PerceptionSampler
    /// V10 §3.1 row 6 + row 8 — observes ``NSWorkspace.didWakeNotification``
    /// and periodically pushes a ``perf.metrics`` envelope so the
    /// Python agent can log Swift-side hard budget readings.
    public let perfMetrics: PerfMetricsBinding

    private let callbackQueue = DispatchQueue(
        label: "deskmate.menubar.cb", qos: .userInitiated
    )
    private let historyBuffer = ChatHistoryBuffer(maxEntries: 20)
    private let islandHoverRouter = IslandHoverRouter()
    private let powerDegradationTrigger = PowerDegradationTrigger()
    private var domainDegradationLevel: Int = 0
    private var powerDegradationLevel: Int = 0
    private var islandTickTimer: Timer?
    private var powerObserver: NSObjectProtocol?
    /// V10 L3-C2 — held strongly so we can ``invalidate()`` on a
    /// frontmost-app switch (V10 L3-E2).
    private let frontmostProvider: CachedFrontmostAppProvider
    #if canImport(AppKit)
    private var sleepObserver: NSObjectProtocol?
    private var wakeObserver: NSObjectProtocol?
    private var didActivateObserver: NSObjectProtocol?
    #endif

    public init() {
        let shell = DeskmateShell(
            configuration: .init(
                socketPath: DefaultSocketPath.current(),
                bridgeBackoff: .init(
                    initialBackoff: 0.5,
                    maxBackoff: 10.0,
                    multiplier: 2.0,
                    jitterFraction: 0.2
                )
            ),
            callbackQueue: callbackQueue
        )
        self.shell = shell
        let frontmostProvider = CachedFrontmostAppProvider(
            provider: DefaultPerceptionProviders.frontmostApp
        )
        self.frontmostProvider = frontmostProvider
        self.sampler = PerceptionSampler(
            sender: shell,
            configuration: .init(
                tickInterval: 2.0,
                heartbeatInterval: 30.0,
                idleSecondsForIdleState: 30.0,
                idleProvider: DefaultPerceptionProviders.idleSeconds,
                frontmostAppProvider: { frontmostProvider() },
                pacer: PerceptionPacer()
            )
        )
        #if canImport(AppKit)
        self.perfMetrics = PerfMetricsBinding(
            center: NSWorkspace.shared.notificationCenter,
            wakeNotificationName: NSWorkspace.didWakeNotification,
            sender: { [weak shell] in shell },
            frameTickerSource: CVDisplayLinkFrameTicker()
        )
        #else
        self.perfMetrics = PerfMetricsBinding(
            sender: { [weak shell] in shell }
        )
        #endif

        self.activeAvatarStyle = Self.resolveActiveAvatarStyle()
        installSubscriptions()
        refreshIslandSurfaceDiagnostics()
        installSubscriptions()
        installLifecycleObservers()
        updatePowerDegradation()
        // V10 island polish #10: route register_module intents into
        // our published registry. Capture self weakly so the closure
        // doesn't keep the runtime alive.
        shell.dispatcher.bindModuleRegistration(
            apply: { [weak self] module in
                DispatchQueue.main.async {
                    self?.islandModules.register(module)
                }
            },
            onDecodeError: nil
        )
        shell.start()
        sampler.start()
        // 5 s push cadence keeps log noise low while still giving a
        // contributor near-real-time visibility into the Swift-side
        // budgets.
        perfMetrics.start(pushInterval: 5.0)
        startIslandTickTimer()
    }

    /// Latch the wake-to-first-frame budget. Call from the first
    /// SwiftUI render after the menu bar app boots and after each
    /// system wake. Idempotent when no wake is pending.
    public func markFirstFrame() {
        perfMetrics.markFirstFrame()
    }

    // MARK: - Subscriptions

    private func installSubscriptions() {
        shell.bridge.onStateChange { [weak self] s in
            DispatchQueue.main.async { self?.bridgeState = s }
        }
        shell.domainState.subscribe { [weak self] d in
            // Derive the policy on the callback queue so the
            // ``DispatchQueue.main`` hop carries both fields in a
            // single update — SwiftUI views observing ``domain`` and
            // ``degradationPolicy`` therefore never see a torn pair.
            let policy = DegradationPolicy(level: d.degradationLevel)
            DispatchQueue.main.async {
                guard let self else { return }
                self.domain = d
                self.domainDegradationLevel = policy.level
                self.updateDegradationPolicy()
            }
        }
        shell.sessionList.subscribe { [weak self] r in
            DispatchQueue.main.async { self?.sessions = r }
        }
        shell.pendingReminders.subscribe { [weak self] r in
            DispatchQueue.main.async { self?.reminders = r }
        }
        shell.pendingApprovals.subscribe { [weak self] r in
            DispatchQueue.main.async {
                guard let self else { return }
                self.approvals = r
                if !r.isEmpty {
                    self.openIslandSessionList()
                }
            }
        }
        shell.islandSurface.subscribe { [weak self] change in
            DispatchQueue.main.async {
                guard let self else { return }
                self.island = change
                self.refreshIslandSurfaceDiagnostics()
            }
        }
        shell.bubbleQueue.subscribe { [weak self] queue in
            let nowMs = Int(Date().timeIntervalSince1970 * 1000)
            let bubble = queue.peek(nowMs: nowMs)
            DispatchQueue.main.async {
                guard let self else { return }
                self.currentBubble = bubble
                if let b = bubble,
                   self.historyBuffer.recordBubbleIfChatLike(
                       b, at: nowMs
                   )
                {
                    self.chatHistory = self.historyBuffer.entries
                }
            }
        }
    }

    // MARK: - User-triggered actions

    /// Allow or deny an approval. Produces a typed
    /// :class:`InteractionAction` of kind ``permission.resolve`` with
    /// ``source: .menuBar`` so the agent can attribute the decision.
    public func resolveApproval(
        id: String, allow: Bool, source: ActionSource = .menuBar
    ) {
        let action = InteractionActionFactory.resolveApproval(
            id: id, allow: allow, source: source
        )
        try? shell.send(action: action)
    }

    /// Jump to a session in whatever surface the user wants to
    /// restore.
    public func jumpToSession(_ id: String, source: ActionSource = .menuBar) {
        try? shell.send(
            action: InteractionActionFactory.jumpToSession(id: id, source: source)
        )
    }

    public func answerQuestion(
        sessionId: String,
        answer: String,
        source: ActionSource = .island
    ) {
        try? shell.send(
            action: InteractionActionFactory.answerQuestion(
                sessionId: sessionId,
                answer: answer,
                source: source
            )
        )
    }

    public func triggerDemo(_ scenario: String) {
        try? shell.send(
            action: InteractionActionFactory.demoTrigger(scenario: scenario)
        )
    }

    public func handleIslandHover(_ event: IslandHoverRouter.Event) {
        let current = island?.state.kind ?? shell.islandSurface.surface.kind
        if case .tap = event {
            if current == .sessionList {
                shell.islandSurface.dismiss()
                try? shell.send(
                    action: InteractionActionFactory.dismissSurface(
                        surface: current,
                        source: .island
                    )
                )
            } else {
                openIslandSessionList()
            }
            return
        }

        if case .enter = event, current != .sessionList {
            openIslandSessionList()
            return
        }

        if case .leave = event {
            shell.islandSurface.noteUserInteract()
            return
        }

        switch islandHoverRouter.decide(event: event, current: current) {
        case .noop:
            shell.islandSurface.noteUserInteract()
        case .promote(let kind):
            shell.islandSurface.present(kind: kind, priority: .p0)
        case .dismiss:
            shell.islandSurface.dismiss()
            try? shell.send(
                action: InteractionActionFactory.dismissSurface(
                    surface: current,
                    source: .island
                )
            )
        }
    }

    public func openIslandSessionList() {
        shell.islandSurface.present(kind: .sessionList, priority: .p1)
    }

    public var isIslandExpanded: Bool {
        island?.state.kind == .sessionList
            || shell.islandSurface.surface.kind == .sessionList
    }

    public func closeIslandSessionList(source: ActionSource = .island) {
        let current = island?.state.kind ?? shell.islandSurface.surface.kind
        guard current == .sessionList else { return }
        shell.islandSurface.dismiss()
        try? shell.send(
            action: InteractionActionFactory.dismissSurface(
                surface: current,
                source: source
            )
        )
    }

    public func updateIslandDiagnostics(_ diagnostics: String) {
        guard islandDiagnostics != diagnostics else { return }
        islandDiagnostics = diagnostics
    }

    public var combinedIslandDiagnostics: String {
        islandSurfaceDiagnostics + "\n" + islandDiagnostics
    }

    private func refreshIslandSurfaceDiagnostics() {
        islandSurfaceDiagnostics = shell.islandSurface.debugSummary
    }

    /// Forward a Python-declared :class:`BubbleAction` to the wire.
    /// Unknown ``interactionKind`` strings are silently dropped so a
    /// newer agent shipping actions an older shell doesn't understand
    /// degrades gracefully.
    public func sendBubbleAction(
        _ action: BubbleAction, bubbleId: String
    ) {
        guard let interaction = InteractionActionFactory.bubbleAction(
            action, bubbleId: bubbleId
        ) else { return }
        try? shell.send(action: interaction)
    }

    /// Fire a ``user.click_pet`` envelope — the reactive chain on the
    /// Python side decodes it and emits a SHOW_PET_BUBBLE intent that
    /// hydrates back into ``currentBubble``. Shared by both the Pet
    /// overlay tap and the menu-bar "Poke pet" button so they have
    /// identical semantics.
    public func clickPet() {
        try? shell.sendUserClickPet()
    }

    /// Send a free-form user message and mirror it into the local
    /// chat history ribbon so the ribbon always leads with the most
    /// recent user turn (the pet's reply lands via the bubble
    /// subscription).
    public func sendUserMessage(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        try? shell.sendUserMessage(trimmed)
        let nowMs = Int(Date().timeIntervalSince1970 * 1000)
        historyBuffer.recordUserMessage(trimmed, at: nowMs)
        chatHistory = historyBuffer.entries
    }

    /// Wipe the local chat history ribbon — agent state is untouched.
    public func clearChatHistory() {
        historyBuffer.clear()
        chatHistory = []
    }

    /// Graceful teardown — call from the Quit menu item before exit.
    public func quit() {
        removeLifecycleObservers()
        stopIslandTickTimer()
        perfMetrics.stop()
        sampler.stop()
        shell.stop()
        #if canImport(AppKit)
        NSApp.terminate(nil)
        #else
        exit(0)
        #endif
    }

    // MARK: - OS lifecycle / power

    private func installLifecycleObservers() {
        powerObserver = NotificationCenter.default.addObserver(
            forName: Notification.Name("NSProcessInfoPowerStateDidChange"),
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.updatePowerDegradation() }
        }
        #if canImport(AppKit)
        sleepObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.willSleepNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.sampler.setPaused(true) }
        }
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.sampler.setPaused(false)
                self.sampler.tick()
            }
        }
        // V10 L3-E2: passive frontmost listener — every app switch
        // invalidates the cached provider and immediately pushes
        // a fresh perception so Python sees the new app within
        // milliseconds rather than waiting for the next paced tick.
        didActivateObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.frontmostProvider.invalidate()
                self.sampler.noteFrontmostChanged()
            }
        }
        #endif
    }

    private func removeLifecycleObservers() {
        if let powerObserver {
            NotificationCenter.default.removeObserver(powerObserver)
        }
        powerObserver = nil
        #if canImport(AppKit)
        if let sleepObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(sleepObserver)
        }
        if let wakeObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(wakeObserver)
        }
        if let didActivateObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(didActivateObserver)
        }
        sleepObserver = nil
        wakeObserver = nil
        didActivateObserver = nil
        #endif
    }

    private func startIslandTickTimer() {
        guard islandTickTimer == nil else { return }
        islandTickTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) {
            [weak self] _ in
            Task { @MainActor in
                self?.shell.islandSurface.tick()
            }
        }
    }

    private func stopIslandTickTimer() {
        islandTickTimer?.invalidate()
        islandTickTimer = nil
    }

    private func updatePowerDegradation() {
        powerDegradationLevel = powerDegradationTrigger.level(
            isLowPowerModeEnabled: ProcessInfo.processInfo.isLowPowerModeEnabled
        )
        updateDegradationPolicy()
    }

    private func updateDegradationPolicy() {
        let policy = DegradationPolicy.combined(
            domainLevel: domainDegradationLevel,
            localLevel: powerDegradationLevel
        )
        if degradationPolicy != policy {
            degradationPolicy = policy
        }
    }

    // MARK: - Character packs

    private static func resolveActiveAvatarStyle(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> String {
        let registry = CharacterPackActivation.buildDefaultRegistry(
            extraRoots: bundledPackRoots(
                environment: environment,
                fileManager: fileManager
            ),
            environment: environment,
            fileManager: fileManager
        )
        return CharacterPackActivation.resolveAvatarStyle(
            in: registry,
            environment: environment
        )
    }

    private static func bundledPackRoots(
        environment: [String: String],
        fileManager: FileManager
    ) -> [URL] {
        if let raw = environment[CharacterPackEnv.bundledPacksDirVar],
           !raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return [URL(fileURLWithPath: (raw as NSString).expandingTildeInPath)]
        }

        let cwd = URL(fileURLWithPath: fileManager.currentDirectoryPath)
        let candidates = [
            cwd.appendingPathComponent("assets/packs"),
            cwd.appendingPathComponent("../assets/packs"),
            Bundle.main.bundleURL
                .appendingPathComponent("Contents/Resources/packs"),
            Bundle.main.bundleURL.appendingPathComponent("assets/packs"),
        ].map { $0.standardizedFileURL }

        return candidates.filter { url in
            var isDir: ObjCBool = false
            return fileManager.fileExists(atPath: url.path, isDirectory: &isDir)
                && isDir.boolValue
        }
    }
}
