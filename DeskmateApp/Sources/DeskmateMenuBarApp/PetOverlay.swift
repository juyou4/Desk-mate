import SwiftUI
import DeskmateCore
#if canImport(AppKit)
import AppKit
#endif

/// Desktop pet sprite + floating bubble (V10 Phase 11d-vii / Phase 7).
///
/// The sprite is rendered from the active manifest-backed character
/// pack when frame assets are available. ``AvatarView`` remains the
/// fallback, using a spec produced by :class:`AvatarRenderer`.
/// Supported fallback styles:
///
/// - ``pixel`` (default) — 8×8 pixel-art face coloured by mood.
/// - ``emoji`` — a single mood-driven emoji glyph.
///
/// Tapping fires a ``pet.interact`` action so the agent can react
/// to direct user attention (e.g. show a chat bubble with a
/// greeting).
struct PetOverlay: View {
    @ObservedObject var runtime: DeskmateMenuBarRuntime
    let onPetDragBegan: () -> Void
    let onPetDragChanged: (CGSize) -> Void
    let onPetDragEnded: () -> Void

    @State private var now = Date()
    @State private var lastUserInteractionAt = Date()
    @State private var localAnimationOverride: String? = nil
    @State private var localOverrideExpiresAt: Date? = nil
    @State private var isUserInteracting = false
    @State private var dragFeedbackOffset: CGSize = .zero

    private let localTick = Timer.publish(
        every: 1,
        on: .main,
        in: .common
    ).autoconnect()

    init(
        runtime: DeskmateMenuBarRuntime,
        onPetDragBegan: @escaping () -> Void = {},
        onPetDragChanged: @escaping (CGSize) -> Void = { _ in },
        onPetDragEnded: @escaping () -> Void = {}
    ) {
        self.runtime = runtime
        self.onPetDragBegan = onPetDragBegan
        self.onPetDragChanged = onPetDragChanged
        self.onPetDragEnded = onPetDragEnded
    }

    var body: some View {
        VStack(alignment: .trailing, spacing: 10) {
            if let bubble = runtime.currentBubble {
                BubbleView(
                    bubble: bubble,
                    onAction: { action in
                        noteUserInteraction()
                        runtime.sendBubbleAction(action, bubbleId: bubble.id)
                    },
                    onMessage: { message in
                        noteUserInteraction()
                        runtime.sendUserMessage(message)
                    }
                )
                .transition(
                    .scale(scale: 0.8, anchor: .bottomTrailing)
                        .combined(with: .opacity)
                )
            }
            petSprite
        }
        .padding(12)
        .animation(.spring(duration: 0.3), value: runtime.currentBubble?.id)
        .onReceive(localTick) { tick in
            now = tick
        }
    }

    private var petSprite: some View {
        ZStack {
            Circle()
                .fill(auraGradient)
                .frame(width: 112, height: 112)
                .opacity(auraOpacity)
                .blur(radius: 14)
            ManifestPetSpriteView(
                pack: runtime.activeCharacterPack,
                presentation: presentationState,
                fallbackSpec: avatarSpec,
                size: 88
            )
                .background(
                    Circle().fill(Color.white.opacity(0.0001))
                )
        }
        .offset(dragFeedbackOffset)
        .scaleEffect(isUserInteracting ? 1.05 : 1.0)
        .contentShape(Circle())
        .onTapGesture(perform: handlePetTap)
        .simultaneousGesture(
            DragGesture(minimumDistance: 4)
                .onChanged(handlePetDragChanged(_:))
                .onEnded(handlePetDragEnded(_:))
        )
        .accessibilityLabel("Deskmate pet")
        .accessibilityAddTraits(.isButton)
        .accessibilityAction(named: Text("Ping")) {
            handlePetTap()
        }
        .help("Click to ping Deskmate")
        .animation(.spring(response: 0.2, dampingFraction: 0.72), value: isUserInteracting)
        .animation(.spring(response: 0.22, dampingFraction: 0.7), value: dragFeedbackOffset)
    }

    // MARK: - Derived visuals

    /// The resolved avatar spec for the current frame.
    /// Style is sourced from the active manifest-backed character
    /// pack resolved by ``DeskmateMenuBarRuntime``. The legacy
    /// ``DESKMATE_AVATAR_STYLE`` env var is only used when no pack
    /// can be resolved.
    private var avatarSpec: AvatarSpec {
        return AvatarRenderer.resolve(
            style: presentationState.avatarStyle,
            mood: runtime.domain.agentMood,
            emotion: presentationState.emotion,
            attentionLevel: presentationState.attentionLevel
        )
    }

    private var presentationState: PetPresentationState {
        guard let manifest = runtime.activeCharacterPack?.manifest else {
            return PetPresentationState(
                animationState: "idle",
                emotion: derivedFallbackEmotion,
                attentionLevel: fallbackAttentionLevel,
                avatarStyle: runtime.activeAvatarStyle
            )
        }

        var domain = runtime.domain
        if domain.pendingApprovals.isEmpty && !runtime.approvals.isEmpty {
            domain.pendingApprovals = runtime.approvals.map(\.approvalId)
        }
        return PetStateMachine.reduce(
            PetStateMachine.Input(
                domain: domain,
                idleMs: localIdleMs,
                animationOverride: activeAnimationOverride,
                isUserInteracting: isUserInteracting
            ),
            manifest: manifest
        )
    }

    private var activeAnimationOverride: String? {
        if isUserInteracting {
            return "drag"
        }
        if let localAnimationOverride,
           let expiresAt = localOverrideExpiresAt,
           expiresAt > now {
            return localAnimationOverride
        }
        return runtime.petAnimationOverride
    }

    private var localIdleMs: Int {
        max(0, Int(now.timeIntervalSince(lastUserInteractionAt) * 1000))
    }

    private var isAutoResting: Bool {
        runtime.domain.agentMood == .idle
            && runtime.domain.currentPriority == .p3
            && runtime.approvals.isEmpty
            && localIdleMs >= PetStateMachine.dozingThresholdMs
    }

    private func noteUserInteraction() {
        now = Date()
        lastUserInteractionAt = now
    }

    private func handlePetTap() {
        let wasAutoResting = isAutoResting
        noteUserInteraction()
        runtime.clickPet()
        setLocalAnimationOverride(
            wasAutoResting ? "waking" : "react-click",
            duration: wasAutoResting ? 1.4 : 1.8
        )
    }

    private func handlePetDragChanged(_ value: DragGesture.Value) {
        noteUserInteraction()
        if !isUserInteracting {
            onPetDragBegan()
        }
        isUserInteracting = true
        onPetDragChanged(value.translation)
        dragFeedbackOffset = CGSize(
            width: max(-12, min(12, value.translation.width * 0.16)),
            height: max(-10, min(10, value.translation.height * 0.16))
        )
    }

    private func handlePetDragEnded(_ value: DragGesture.Value) {
        noteUserInteraction()
        let wasInteracting = isUserInteracting
        isUserInteracting = false
        dragFeedbackOffset = .zero
        if wasInteracting {
            onPetDragEnded()
        }
        setLocalAnimationOverride("waking", duration: 0.9)
    }

    private func setLocalAnimationOverride(_ state: String, duration: TimeInterval) {
        localAnimationOverride = state
        localOverrideExpiresAt = Date().addingTimeInterval(duration)
    }

    private var derivedFallbackEmotion: String {
        switch runtime.domain.agentMood {
        case .alert: return runtime.approvals.isEmpty ? "concerned" : "urgent"
        case .happy: return "cheerful"
        case .thinking: return "curious"
        case .working: return "focused"
        case .idle: return runtime.approvals.isEmpty ? "neutral" : "concerned"
        }
    }

    private var fallbackAttentionLevel: Double {
        if !runtime.approvals.isEmpty {
            return 1.0
        }
        if runtime.currentBubble != nil {
            return 0.6
        }
        return 0.2
    }

    private var auraGradient: RadialGradient {
        RadialGradient(
            colors: [
                auraColor.opacity(0.8),
                auraColor.opacity(0.0),
            ],
            center: .center,
            startRadius: 2,
            endRadius: 40
        )
    }

    /// Reuses the resolved avatar palette so the sprite and the
    /// aura never get out of sync — previously they were maintained
    /// in two parallel switch statements.
    private var auraColor: Color {
        let rgb = avatarSpec.aura
        return Color(
            .sRGB,
            red: Double(rgb.r) / 255.0,
            green: Double(rgb.g) / 255.0,
            blue: Double(rgb.b) / 255.0,
            opacity: 1.0
        )
    }

    private var auraOpacity: Double {
        // Pulse gently when there's something the user should notice.
        runtime.currentBubble != nil || runtime.approvals.count > 0
            ? 0.9 : 0.3
    }
}

#if canImport(AppKit)
private struct ManifestPetSpriteView: View {
    let pack: CharacterPackLoader.LoadedPack?
    let presentation: PetPresentationState
    let fallbackSpec: AvatarSpec
    let size: CGFloat

    var body: some View {
        if let clip = resolvedClip {
            TimelineView(.animation(minimumInterval: frameInterval(for: clip))) { context in
                frameBody(for: clip, at: context.date)
            }
        } else {
            fallback
        }
    }

    private var fallback: some View {
        AvatarView(spec: fallbackSpec, size: min(size, 72))
    }

    private var resolvedClip: PetSpriteClip? {
        guard
            let pack,
            let stateName = pack.manifest.resolveState(presentation.animationState),
            let state = pack.manifest.states[stateName],
            !state.frames.isEmpty
        else {
            return nil
        }
        return PetSpriteClip(packRoot: pack.rootURL, state: state)
    }

    private func frameInterval(for clip: PetSpriteClip) -> TimeInterval {
        1.0 / Double(max(1, clip.state.fps))
    }

    @ViewBuilder
    private func frameBody(for clip: PetSpriteClip, at date: Date) -> some View {
        let animator = PetFrameAnimator(
            fps: max(1, clip.state.fps),
            frameCount: clip.state.frames.count
        )
        let elapsedMs = Int(date.timeIntervalSinceReferenceDate * 1000)
        let frame = clip.state.frames[animator.frameIndex(elapsedMs: elapsedMs)]
        let url = clip.packRoot.appendingPathComponent(frame)
        if let image = NSImage(contentsOf: url) {
            Image(nsImage: image)
                .interpolation(.none)
                .resizable()
                .scaledToFit()
                .frame(width: size, height: size)
                .accessibilityLabel("Deskmate \(presentation.animationState) pet")
        } else {
            fallback
        }
    }
}

private struct PetSpriteClip {
    let packRoot: URL
    let state: StateFrames
}
#else
private struct ManifestPetSpriteView: View {
    let pack: CharacterPackLoader.LoadedPack?
    let presentation: PetPresentationState
    let fallbackSpec: AvatarSpec
    let size: CGFloat

    var body: some View {
        AvatarView(spec: fallbackSpec, size: min(size, 72))
    }
}
#endif
