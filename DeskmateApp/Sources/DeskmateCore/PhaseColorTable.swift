import SwiftUI

/// Single source of truth for phase-derived colours used across the
/// island's status dot, pill background, pill stroke, chip foreground,
/// and progress capsule. See R7 of `island-polish-enhancements`:
///
/// - **R7.1**: one table covering every ``SessionRow/Phase`` value
///   returning a triple of `(foreground, background, stroke)`.
/// - **R7.3**: the `switch` is exhaustive with no `default` clause so
///   adding a new ``SessionRow/Phase`` case anywhere in the codebase
///   produces a compile-time error here, not a silent runtime fallback.
/// - **R7.4**: ``PhaseColorTable/resolve(_:scheme:)`` is a deterministic
///   pure function — no global / static / instance mutable state is
///   read or written.
/// - **R7.5**: the resolver accepts a ``ColorScheme`` argument so a
///   later polish pass can return scheme-specific tunings without
///   changing the call sites. For now both schemes return the same
///   triple; see the TODO below.
///
/// The actual phase → colour mapping replicates the existing
/// `phaseColor(for:)` switch in `IslandOverlay.swift` exactly:
/// foreground colours are unchanged, background uses 0.14 alpha and
/// stroke uses 0.22 alpha.

/// Three SwiftUI ``Color`` values representing the foreground (chip
/// text / status dot / progress capsule fill), the background (pill
/// fill at low opacity), and the stroke (pill border) for a given
/// ``SessionRow/Phase``.
public struct PhaseColorTriple: Equatable, Sendable {
    public let foreground: Color
    public let background: Color
    public let stroke: Color

    public init(foreground: Color, background: Color, stroke: Color) {
        self.foreground = foreground
        self.background = background
        self.stroke = stroke
    }
}

/// Resolver enum that owns the phase → ``PhaseColorTriple`` mapping.
/// The single static method ``resolve(_:scheme:)`` is the only entry
/// point; callers MUST NOT define ad-hoc colour literals for any of
/// the surfaces listed in R7.2.
public enum PhaseColorTable {
    /// Resolve the colour triple for the given phase.
    ///
    /// - Parameters:
    ///   - phase: the session phase to render.
    ///   - scheme: the active SwiftUI ``ColorScheme``. Currently both
    ///     schemes return the same triple; scheme-aware tunings are a
    ///     follow-up polish (see TODO below).
    /// - Returns: the foreground / background / stroke colours.
    ///
    /// - Note: This function is intentionally **pure**: the output
    ///   depends only on its arguments, and it reads from no mutable
    ///   state. Repeated calls with identical arguments return
    ///   value-equal triples for the lifetime of the process (R7.4).
    ///
    /// - Important: The `switch` is exhaustive with no `default`
    ///   clause so adding a new ``SessionRow/Phase`` case anywhere
    ///   produces a compile-time error here (R7.3).
    public static func resolve(
        _ phase: SessionRow.Phase,
        scheme: ColorScheme = .dark
    ) -> PhaseColorTriple {
        // TODO(island-polish-enhancements R7.5/R7.6): return
        // light-scheme-specific triples when `scheme == .light`. For
        // now the dark-tuned palette is used in both schemes; the
        // call sites already invoke `resolve` on every snapshot so
        // wiring up scheme-aware variants is a localised follow-up.
        _ = scheme
        switch phase {
        case .waitingForApproval:
            return PhaseColorTriple(
                foreground: .orange,
                background: .orange.opacity(0.14),
                stroke: .orange.opacity(0.22)
            )
        case .waitingForAnswer:
            return PhaseColorTriple(
                foreground: .yellow,
                background: .yellow.opacity(0.14),
                stroke: .yellow.opacity(0.22)
            )
        case .thinking:
            return PhaseColorTriple(
                foreground: .purple,
                background: .purple.opacity(0.14),
                stroke: .purple.opacity(0.22)
            )
        case .editing:
            return PhaseColorTriple(
                foreground: .blue,
                background: .blue.opacity(0.14),
                stroke: .blue.opacity(0.22)
            )
        case .runningTool:
            return PhaseColorTriple(
                foreground: .cyan,
                background: .cyan.opacity(0.14),
                stroke: .cyan.opacity(0.22)
            )
        case .testing:
            return PhaseColorTriple(
                foreground: .mint,
                background: .mint.opacity(0.14),
                stroke: .mint.opacity(0.22)
            )
        case .failed:
            return PhaseColorTriple(
                foreground: .red,
                background: .red.opacity(0.14),
                stroke: .red.opacity(0.22)
            )
        case .completed:
            return PhaseColorTriple(
                foreground: .green,
                background: .green.opacity(0.14),
                stroke: .green.opacity(0.22)
            )
        case .running, .unknown:
            let base = Color(red: 0.29, green: 0.86, blue: 0.46)
            return PhaseColorTriple(
                foreground: base,
                background: base.opacity(0.14),
                stroke: base.opacity(0.22)
            )
        }
    }
}
