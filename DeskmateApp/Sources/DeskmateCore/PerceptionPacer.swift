import Foundation

/// V10 L3-B10 / E1 sampler pacer.
///
/// Pure value type that maps the user's current focus tier (and the
/// "system is asleep" override) to the next perception tick interval.
/// Plugs into :class:`PerceptionSampler` so the polling cadence
/// matches actual demand: tight when the user is interacting, looser
/// when they're idle, paused while the display sleeps.
///
/// Defaults match the V10 plan:
///
/// | Focus tier  | Default interval |
/// |-------------|------------------|
/// | ``focused`` | 1.0 s            |
/// | ``casual``  | 2.0 s            |
/// | ``idleBack``| 5.0 s            |
/// | sleeping    | paused (∞)       |
///
/// Custom values can be passed in for tests / future tuning, but the
/// monotonic ordering ``focused ≤ casual ≤ idleBack`` is enforced so
/// a misconfiguration can never make the sampler tighter on idle
/// than on focused.
public struct PerceptionPacer: Equatable, Sendable {
    public var focusedInterval: TimeInterval
    public var casualInterval: TimeInterval
    public var idleBackInterval: TimeInterval

    public init(
        focusedInterval: TimeInterval = 1.0,
        casualInterval: TimeInterval = 2.0,
        idleBackInterval: TimeInterval = 5.0
    ) {
        // Monotonic guard: ``focused`` is the tightest tier, so
        // every subsequent tier must be at least as long.
        let f = max(0.05, focusedInterval)
        let c = max(f, casualInterval)
        let i = max(c, idleBackInterval)
        self.focusedInterval = f
        self.casualInterval = c
        self.idleBackInterval = i
    }

    /// Return the next tick interval given the current ``focus``.
    /// ``isAsleep`` overrides the focus tier — pulling the sampler
    /// off the bus while the display is off.
    public func interval(
        for focus: UserFocus, isAsleep: Bool = false
    ) -> TimeInterval? {
        if isAsleep { return nil }
        switch focus {
        case .focused:
            return focusedInterval
        case .casual:
            return casualInterval
        case .idleBack:
            return idleBackInterval
        }
    }
}
