import Foundation

/// Pure reducer: (``DomainState`` + incoming signals) → ``PetPresentationState``.
///
/// V10 L2-#4 mandates that the pet Surface is a *derivation* of the Domain
/// state. The reducer encodes that mapping so unit tests can pin it without
/// spinning up any AppKit window. Side-effects (animation restart, bubble
/// bookkeeping) live in the window layer.
public enum PetStateMachine {
    public static let dozingThresholdMs = 20_000
    public static let sleepingThresholdMs = 60_000

    /// Authoritative base mapping from ``AgentMood`` to a requested universal
    /// animation name. Older packs remain supported through
    /// ``CharacterPackManifest.fallbacks`` (for example `running` -> `working`).
    public static let moodAnimation: [AgentMood: String] = [
        .idle: "idle",
        .working: "running",
        .thinking: "review",
        .alert: "waiting",
        .happy: "jumping",
    ]

    public struct Input: Equatable, Sendable {
        public var domain: DomainState
        /// ``idle_ms`` forwarded from the latest ``perception`` envelope.
        public var idleMs: Int
        /// External override (e.g. from `set_pet_animation` intent). Wins
        /// over the mood mapping but still walks the manifest fallbacks.
        public var animationOverride: String?
        /// Whether the user is currently interacting (drag / click). Blocks
        /// any state change while true so visual feedback stays stable.
        public var isUserInteracting: Bool
        /// Whether the pet is currently anchored to the nest.
        public var isNesting: Bool

        public init(
            domain: DomainState,
            idleMs: Int = 0,
            animationOverride: String? = nil,
            isUserInteracting: Bool = false,
            isNesting: Bool = false
        ) {
            self.domain = domain
            self.idleMs = idleMs
            self.animationOverride = animationOverride
            self.isUserInteracting = isUserInteracting
            self.isNesting = isNesting
        }
    }

    /// Produce the next presentation state. Given the same input this always
    /// returns the same output — safe to call from any thread.
    public static func reduce(
        _ input: Input,
        manifest: CharacterPackManifest,
        previous: PetPresentationState = PetPresentationState()
    ) -> PetPresentationState {
        // 1. Decide the *requested* animation, then walk manifest fallbacks
        //    so we never hand the renderer a state that has no frames.
        let requested = requestedAnimation(input: input)
        let resolved = manifest.resolveState(requested)
            ?? manifest.resolveState("idle")
            ?? previous.animationState

        // 2. Emotion is a soft signal mainly for avatar overlays. Urgent
        //    approvals override any mood-derived emotion.
        let emotion = resolveEmotion(input: input)

        // 3. Attention level — how visible / animated the pet should feel.
        let attentionLevel = resolveAttentionLevel(input: input)

        // 4. Anchor / interactivity. While the user is dragging we lock
        //    everything so the drag feels "direct".
        let anchor: PetAnchorKind = input.isNesting ? .nest : .desktop
        let isInteractive = !input.isUserInteracting

        // 5. Avatar style comes from the manifest (currently "pixel"), but
        //    the manifest may offer alternates via ``supportedStyles``.
        let avatarStyle = manifest.avatar.defaultStyle

        // 6. Preserve the bubble id from the previous frame — the bubble
        //    queue owns that lifecycle. Clearing it here would race.
        return PetPresentationState(
            animationState: resolved,
            emotion: emotion,
            attentionLevel: attentionLevel,
            anchorKind: anchor,
            isInteractive: isInteractive,
            bubbleId: previous.bubbleId,
            avatarStyle: avatarStyle
        )
    }

    // MARK: - Internals

    private static func requestedAnimation(input: Input) -> String {
        if let override = input.animationOverride, !override.isEmpty {
            return override
        }
        // Approvals trump all moods — we want the user to *see* them.
        if !input.domain.pendingApprovals.isEmpty {
            return moodAnimation[.alert] ?? "alert"
        }
        if input.isNesting {
            return "nesting"
        }
        if canAutoRest(input),
           input.idleMs >= sleepingThresholdMs {
            return "sleeping"
        }
        if canAutoRest(input),
           input.idleMs >= dozingThresholdMs {
            return "dozing"
        }
        if input.domain.userFocus == .focused && input.domain.agentMood == .idle {
            // Stay out of the way: use ``idle`` but never ``happy``/``alert``.
            return "idle"
        }
        return moodAnimation[input.domain.agentMood] ?? "idle"
    }

    private static func canAutoRest(_ input: Input) -> Bool {
        input.domain.agentMood == .idle
            && input.domain.currentPriority == .p3
            && input.domain.pendingApprovals.isEmpty
            && !input.isUserInteracting
    }

    private static func resolveEmotion(input: Input) -> String {
        if !input.domain.pendingApprovals.isEmpty {
            return "concerned"
        }
        switch input.domain.currentPriority {
        case .p0: return "urgent"
        case .p1: return "focused"
        case .p2, .p3: break
        }
        switch input.domain.agentMood {
        case .alert: return "concerned"
        case .happy: return "cheerful"
        case .thinking: return "curious"
        case .working: return "focused"
        case .idle: return "neutral"
        }
    }

    private static func resolveAttentionLevel(input: Input) -> Double {
        if !input.domain.pendingApprovals.isEmpty {
            return 1.0
        }
        if canAutoRest(input), input.idleMs >= sleepingThresholdMs {
            return 0.05
        }
        if canAutoRest(input), input.idleMs >= dozingThresholdMs {
            return 0.12
        }
        switch input.domain.currentPriority {
        case .p0: return 1.0
        case .p1: return 0.8
        case .p2: return 0.5
        case .p3: return input.domain.userFocus == .focused ? 0.1 : 0.3
        }
    }
}
