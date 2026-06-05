import SwiftUI

/// V10 island polish #6 (boring.notch-inspired): single source of
/// truth for every Animation used by the island UI.
///
/// Why centralize:
/// - Consistency. The notch shape transition, content fade, hover
///   width tweak, progress capsule update and carousel rotation all
///   used to be defined inline in different files. Tuning one
///   without touching the others led to subtle desyncs (e.g. close
///   spring landed before content fade finished).
/// - Testability. The smoke binary can lock these values without
///   importing the AppKit menu bar module.
/// - Theming. A future `IslandAnimations.snappy` / `.gentle` preset
///   becomes a one-line swap.
///
/// All values are ``Animation`` so they can be passed straight into
/// ``.animation(_:value:)`` or ``withAnimation(_:)``. The ``response``
/// / ``dampingFraction`` numbers are calibrated against
/// `boring.notch/boringNotch/ContentView.swift:123-124` and
/// `MioIsland/UI/Views/NotchView.swift:215-217`.
public struct IslandAnimations {

    // MARK: - Notch surface (open / close / pop)

    /// Open: snappy bouncy spring (boring.notch's signature 1-2 px
    /// overshoot at the corners).
    public static let open: Animation = .spring(
        response: 0.42, dampingFraction: 0.8, blendDuration: 0
    )

    /// Close: critically-damped smooth ease so the close lands
    /// without bounce (open-vibe-island style).
    public static let close: Animation = .smooth(duration: 0.3)

    /// Sneak-peek / attention pulse: fast underdamped spring.
    public static let pop: Animation = .spring(
        response: 0.3, dampingFraction: 0.5, blendDuration: 0
    )

    // MARK: - Hover and interaction

    /// Hover-induced size tweaks: interactive spring so the surface
    /// follows the cursor's "intent" naturally.
    public static let hover: Animation = .interactiveSpring(
        response: 0.38, dampingFraction: 0.8, blendDuration: 0
    )

    /// Tap activation: quick easeOut so press → release feels snappy.
    public static let tap: Animation = .easeOut(duration: 0.18)

    // MARK: - Content transitions

    /// Compact ↔ expanded content fade. Decoupled from the notch
    /// surface animation so a session arriving while the user is
    /// already hovering doesn't restart the open spring.
    public static let contentFade: Animation = .easeOut(duration: 0.18)

    /// Multi-session glyph stack entry / reordering.
    public static let glyphStack: Animation = .spring(
        response: 0.2, dampingFraction: 0.8, blendDuration: 0
    )

    // MARK: - Progress and value updates

    /// Progress capsule width update. Spring with a soft response
    /// avoids the bar feeling "rubbery" when progress reports come
    /// in 100 ms apart.
    public static let progress: Animation = .smooth(duration: 0.3)

    /// Build-done state morph (running progress → ✓/✗ banner).
    public static let buildDone: Animation = .spring(
        response: 0.32, dampingFraction: 0.86, blendDuration: 0
    )

    // MARK: - Carousel

    /// Trailing-module fact rotation (#2). Subtle ease-in-out so
    /// the user doesn't perceive it as a state change.
    public static let carousel: Animation = .easeInOut(duration: 0.4)

    // MARK: - Miscellaneous

    /// SwiftUI `.smooth` placeholder kept here so callers can
    /// reference one symbol for "default smooth" without importing
    /// `Animation` from SwiftUI directly.
    public static let smooth: Animation = .smooth
}
