import Foundation

/// Pure avatar resolver (V10 Phase 7).
///
/// Given the user-facing *style* name and the agent's current mood /
/// emotion, produce an :class:`AvatarSpec` describing **what** the UI
/// layer should draw. The actual drawing lives in
/// ``DeskmateMenuBarApp/AvatarView`` so ``DeskmateCore`` stays free
/// of SwiftUI / AppKit.
///
/// Two built-in styles are supported:
///
/// - ``.pixel`` — an 8×8 pixel-art face composed of two colour
///   layers (body + accent). Fills the entire sprite.
/// - ``.emoji`` — a single mood-driven emoji glyph.
///
/// Unknown / missing style names fall back to ``.pixel`` so a
/// misspelled character pack manifest still renders *something*.

public enum AvatarStyleKind: String, Sendable, Equatable, CaseIterable {
    case pixel
    case emoji

    /// Forgiving parser used by :func:`AvatarRenderer.resolve`. Unknown
    /// names collapse to ``.pixel`` — the plan's default style.
    public init(rawStyle: String) {
        switch rawStyle.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        {
        case "pixel":
            self = .pixel
        case "emoji":
            self = .emoji
        default:
            self = .pixel
        }
    }
}


/// Three-channel colour value clamped to ``0...255``. We keep this a
/// plain struct rather than a ``(Int,Int,Int)`` tuple so it's
/// ``Equatable`` / ``Hashable`` without bespoke synthesis.
public struct AvatarRgbColor: Sendable, Equatable, Hashable {
    public let r: Int
    public let g: Int
    public let b: Int

    public init(r: Int, g: Int, b: Int) {
        self.r = Self.clamp(r)
        self.g = Self.clamp(g)
        self.b = Self.clamp(b)
    }

    private static func clamp(_ v: Int) -> Int {
        max(0, min(255, v))
    }

    // Named mood palette — kept internal to the resolver. These RGB
    // values were eyeballed to match the prior SF-Symbol gradients
    // so users don't see a jarring visual shift when Phase 7 ships.
    public static let grayBody    = AvatarRgbColor(r: 170, g: 172, b: 178)
    public static let grayAccent  = AvatarRgbColor(r: 120, g: 124, b: 132)
    public static let blueBody    = AvatarRgbColor(r: 66,  g: 133, b: 244)
    public static let cyanAccent  = AvatarRgbColor(r: 14,  g: 182, b: 210)
    public static let purpleBody  = AvatarRgbColor(r: 138, g: 94,  b: 228)
    public static let indigoAccent = AvatarRgbColor(r: 84, g: 58,  b: 186)
    public static let pinkBody    = AvatarRgbColor(r: 234, g: 120, b: 180)
    public static let orangeAccent = AvatarRgbColor(r: 246, g: 156, b: 64)
    public static let orangeBody  = AvatarRgbColor(r: 246, g: 156, b: 64)
    public static let redAccent   = AvatarRgbColor(r: 226, g: 74,  b: 74)
}


/// Renderable description of an avatar frame.
public struct AvatarSpec: Sendable, Equatable {
    public let style: AvatarStyleKind
    /// Populated for every style so an emoji fallback is always
    /// available (e.g. accessibility or command-line previews).
    public let emoji: String
    public let primary: AvatarRgbColor
    public let accent: AvatarRgbColor
    public let aura: AvatarRgbColor
    /// 0 (calm) … 1 (urgent). The UI layer uses this to pulse / glow.
    public let glow: Double

    public init(
        style: AvatarStyleKind,
        emoji: String,
        primary: AvatarRgbColor,
        accent: AvatarRgbColor,
        aura: AvatarRgbColor,
        glow: Double
    ) {
        self.style = style
        self.emoji = emoji
        self.primary = primary
        self.accent = accent
        self.aura = aura
        self.glow = max(0.0, min(1.0, glow))
    }
}


public enum AvatarRenderer {

    /// Resolve a style name + mood + emotion + attention into a
    /// renderable :class:`AvatarSpec`. Pure — safe to call from any
    /// thread and from unit tests.
    public static func resolve(
        style: String,
        mood: AgentMood,
        emotion: String = "neutral",
        attentionLevel: Double = 0.0
    ) -> AvatarSpec {
        let resolvedStyle = AvatarStyleKind(rawStyle: style)
        let (primary, accent) = colorPair(for: mood)
        return AvatarSpec(
            style: resolvedStyle,
            emoji: emojiFor(mood: mood, emotion: emotion),
            primary: primary,
            accent: accent,
            aura: primary,
            glow: attentionLevel
        )
    }

    /// Emoji glyph for a given ``(mood, emotion)``. Emotion is checked
    /// first because ``PetStateMachine`` already encodes the most
    /// urgent signal into it — e.g. ``concerned`` when an approval is
    /// pending, or ``urgent`` for P0.
    public static func emojiFor(mood: AgentMood, emotion: String) -> String {
        switch emotion.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        {
        case "urgent":    return "🚨"
        case "concerned": return "😟"
        case "cheerful":  return "🎉"
        case "focused":   return "🧠"
        case "curious":   return "🤔"
        default:          break
        }
        switch mood {
        case .idle:     return "🙂"
        case .working:  return "🧠"
        case .thinking: return "🤔"
        case .happy:    return "🎉"
        case .alert:    return "⚠️"
        }
    }

    /// The authoritative 8×8 cat-face mask used by ``.pixel`` style.
    /// Values:
    ///
    /// - ``0`` — empty (transparent)
    /// - ``1`` — body colour
    /// - ``2`` — accent colour (eyes / mouth)
    ///
    /// Kept as a function (not a stored static) so test assertions
    /// on the return value always see a fresh array — callers that
    /// mutate defensively don't leak into other tests.
    public static func pixelMask() -> [[Int]] {
        return [
            [0, 1, 0, 0, 0, 0, 1, 0],
            [1, 1, 1, 0, 0, 1, 1, 1],
            [1, 1, 1, 1, 1, 1, 1, 1],
            [1, 2, 1, 1, 1, 1, 2, 1],
            [1, 1, 1, 1, 1, 1, 1, 1],
            [1, 1, 1, 2, 2, 1, 1, 1],
            [0, 1, 1, 1, 1, 1, 1, 0],
            [0, 0, 1, 1, 1, 1, 0, 0],
        ]
    }

    // MARK: - Internals

    private static func colorPair(
        for mood: AgentMood
    ) -> (AvatarRgbColor, AvatarRgbColor) {
        switch mood {
        case .idle:     return (.grayBody,   .grayAccent)
        case .working:  return (.blueBody,   .cyanAccent)
        case .thinking: return (.purpleBody, .indigoAccent)
        case .happy:    return (.pinkBody,   .orangeAccent)
        case .alert:    return (.orangeBody, .redAccent)
        }
    }
}
