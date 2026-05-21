import Foundation

/// Swift mirror of Python's ``DegradationController`` derived
/// policies (V10 Phase 9 · §4).
///
/// The Python side owns the *level* (an integer 0...6 shipped on
/// every ``DomainState`` snapshot via ``degradation_level``); Swift
/// owns the *interpretation* of that level into UI / animation
/// policies. Every derived field is a pure function of the level
/// so callers can treat ``DegradationPolicy`` as a value type and
/// recompute it on every state change without coordinating with
/// Python.
///
/// Step mapping (kept aligned with ``deskmate_agent/degradation.py``):
///
/// | Level | Step | Effect                                           |
/// |-------|------|--------------------------------------------------|
/// | 0     | —    | Normal operation                                 |
/// | 1     | A3   | Pet animation FPS down a tier                    |
/// | 2     | —    | Proactive interval ×2 + threshold engine (Py)    |
/// | 3     | D1   | Perception diff threshold widened (Py)           |
/// | 4     | —    | Hide SneakPeek HUD + matchedGeometryEffect       |
/// | 5     | A4   | ``IslandSurface = .empty`` + ``orderOut``        |
/// | 6     | —    | Camera observer disabled (V0.2 future)           |
///
/// All policies are monotonic: entering level ``K`` implies levels
/// ``1..K`` are all in effect.
public struct DegradationPolicy: Equatable, Sendable {
    /// Bridge-spec'd integer level. Always clamped to ``0...6``.
    public let level: Int

    public init(level: Int = 0) {
        // Defensive clamp: a malformed ``DomainState`` decode pre-
        // clamps too, but the policy is the last line that downstream
        // animation / window code reads, so re-clamp here as well.
        self.level = max(0, min(Self.maxLevel, level))
    }

    // MARK: - Level constants (mirror Python)

    public static let levelNormal: Int = 0
    public static let levelFpsDown: Int = 1
    public static let levelProactiveX2: Int = 2
    public static let levelPerceptionWide: Int = 3
    public static let levelHideHUD: Int = 4
    public static let levelIslandOff: Int = 5
    public static let levelCameraOff: Int = 6
    public static let maxLevel: Int = 6

    public static func combined(
        domainLevel: Int,
        localLevel: Int
    ) -> DegradationPolicy {
        DegradationPolicy(level: max(domainLevel, localLevel))
    }

    // MARK: - Derived policies

    /// Step 1 — translate a renderer's *base* fps into the
    /// degradation-adjusted fps. The current heuristic is "halve
    /// once at level ≥ 1" which gets the pet from a typical 12 fps
    /// down to 6 fps without losing animation cadence entirely.
    /// Always returns at least ``1`` so callers can divide safely.
    public func effectiveFPS(base: Int) -> Int {
        precondition(base > 0, "effectiveFPS base must be positive")
        if level >= Self.levelFpsDown {
            return max(1, base / 2)
        }
        return base
    }

    /// Step 4 — the SneakPeek HUD and its
    /// ``matchedGeometryEffect`` should not animate at level ≥ 4.
    /// UI code reads this and skips the heavier render path.
    public var hideHUD: Bool { level >= Self.levelHideHUD }

    /// Step 5 — the island window should be ``orderOut(_:)``'d and
    /// its surface forced to ``.empty`` at level ≥ 5. The window
    /// controller reads this and short-circuits any present /
    /// update calls.
    public var islandOrderOut: Bool { level >= Self.levelIslandOff }

    /// Step 6 — the (V0.2 future) camera observer should be
    /// disabled at level ≥ 6. Reading this lets shipping shells
    /// declare the policy before the observer code lands.
    public var cameraOff: Bool { level >= Self.levelCameraOff }

    /// Convenience: any non-zero level. Useful for menu-bar badges
    /// or status displays that just want "are we degrading?".
    public var isDegraded: Bool { level > 0 }

    /// Step 1 helper — apply the FPS-tier policy to an existing
    /// :type:`PetFrameAnimator`. Returns a new animator with the
    /// same ``frameCount`` / ``loops`` but the fps run through
    /// :meth:`effectiveFPS(base:)`. Callers wiring a real
    /// SwiftUI / AppKit animation loop construct one base animator
    /// from the character pack and then ``policy.apply(to:)`` it on
    /// every relevant change so the displayed cadence tracks the
    /// degradation level without a separate channel.
    public func apply(to animator: PetFrameAnimator) -> PetFrameAnimator {
        let target = effectiveFPS(base: animator.fps)
        if target == animator.fps {
            // Cheap fast-path: identity → no allocation churn for
            // the common ``level == 0`` case, where every input
            // already passes through unchanged.
            return animator
        }
        return PetFrameAnimator(
            fps: target,
            frameCount: animator.frameCount,
            loops: animator.loops
        )
    }
}

/// Local power-state trigger for L3-A8.
///
/// Python still owns the bridged ``DomainState.degradationLevel``.
/// Swift can additionally raise a local minimum level when macOS
/// enters Low Power Mode or a battery provider reports a low charge;
/// the runtime combines the two levels with ``max`` so local power
/// constraints never lower an agent-requested degradation level.
public struct PowerDegradationTrigger: Equatable, Sendable {
    public var lowBatteryThreshold: Double

    public init(lowBatteryThreshold: Double = 0.20) {
        self.lowBatteryThreshold = min(1.0, max(0.0, lowBatteryThreshold))
    }

    public func level(
        isLowPowerModeEnabled: Bool,
        batteryFraction: Double? = nil
    ) -> Int {
        if isLowPowerModeEnabled {
            return DegradationPolicy.levelFpsDown
        }
        guard let batteryFraction else { return DegradationPolicy.levelNormal }
        return batteryFraction <= lowBatteryThreshold
            ? DegradationPolicy.levelFpsDown
            : DegradationPolicy.levelNormal
    }
}
