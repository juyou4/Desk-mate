import SwiftUI
import DeskmateCore

/// Desktop pet sprite + floating bubble (V10 Phase 11d-vii / Phase 7).
///
/// The sprite is rendered by :class:`AvatarView` using a spec
/// produced by :class:`AvatarRenderer` — a pure function of
/// ``(style, mood, emotion, attentionLevel)``. Supported styles:
///
/// - ``pixel`` (default) — 8×8 pixel-art face coloured by mood.
/// - ``emoji`` — a single mood-driven emoji glyph.
///
/// Tapping fires a ``pet.interact`` action so the agent can react
/// to direct user attention (e.g. show a chat bubble with a
/// greeting).
struct PetOverlay: View {
    @ObservedObject var runtime: DeskmateMenuBarRuntime

    var body: some View {
        VStack(alignment: .trailing, spacing: 10) {
            if let bubble = runtime.currentBubble {
                BubbleView(bubble: bubble) { action in
                    runtime.sendBubbleAction(action, bubbleId: bubble.id)
                }
                .transition(
                    .scale(scale: 0.8, anchor: .bottomTrailing)
                        .combined(with: .opacity)
                )
            }
            petSprite
        }
        .padding(12)
        .animation(.spring(duration: 0.3), value: runtime.currentBubble?.id)
    }

    private var petSprite: some View {
        Button {
            runtime.clickPet()
        } label: {
            ZStack {
                Circle()
                    .fill(auraGradient)
                    .frame(width: 72, height: 72)
                    .opacity(auraOpacity)
                    .blur(radius: 10)
                AvatarView(spec: avatarSpec, size: 64)
                    .background(
                        Circle().fill(Color.white.opacity(0.0001))
                    )
            }
        }
        .buttonStyle(.plain)
        .help("Click to ping Deskmate")
    }

    // MARK: - Derived visuals

    /// The resolved avatar spec for the current frame.
    /// Style is sourced from the active manifest-backed character
    /// pack resolved by ``DeskmateMenuBarRuntime``. The legacy
    /// ``DESKMATE_AVATAR_STYLE`` env var is only used when no pack
    /// can be resolved.
    private var avatarSpec: AvatarSpec {
        let attention: Double = !runtime.approvals.isEmpty ? 1.0
            : (runtime.currentBubble != nil ? 0.6 : 0.2)
        return AvatarRenderer.resolve(
            style: runtime.activeAvatarStyle,
            mood: runtime.domain.agentMood,
            emotion: derivedEmotion,
            attentionLevel: attention
        )
    }

    /// Mirrors :class:`PetStateMachine.resolveEmotion` without
    /// dragging the whole reducer into the view — keep the overlay
    /// readable and the signal equivalent.
    private var derivedEmotion: String {
        if !runtime.approvals.isEmpty {
            return "concerned"
        }
        switch runtime.domain.currentPriority {
        case .p0: return "urgent"
        case .p1: return "focused"
        case .p2, .p3: break
        }
        switch runtime.domain.agentMood {
        case .alert: return "concerned"
        case .happy: return "cheerful"
        case .thinking: return "curious"
        case .working: return "focused"
        case .idle: return "neutral"
        }
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
