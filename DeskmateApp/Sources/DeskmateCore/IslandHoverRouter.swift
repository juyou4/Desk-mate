import Foundation

/// Translates raw hover + tap events into ``IslandSurfaceKind`` transitions
/// (V10 L2-#8 / L3-A10).
///
/// The router is a *pure decision function* — it never mutates the visible
/// state itself. The caller applies the returned :type:`Decision` to the
/// :class:`IslandStateMachine` so there is a single source of truth for
/// "what surface is currently shown".
///
/// Event sources live in the window layer's :class:`NSTrackingArea` /
/// :class:`NSView.mouseDown` callbacks. V10 L3-A10 specifically forbids
/// ``NSEvent.addGlobalMonitor`` for these signals — we want local-only
/// tracking for battery + permission reasons.
public struct IslandHoverRouter: Equatable, Sendable {
    public enum Event: Equatable, Sendable {
        case enter(tsMs: Int)
        case leave(tsMs: Int)
        case tap(tsMs: Int)
    }

    public enum Decision: Equatable, Sendable {
        case noop
        case promote(to: IslandSurfaceKind)
        case dismiss
    }

    public var hoverPromotesToSessionList: Bool
    public var hoverLeaveReturnsToCompact: Bool
    public var tapPromotesToSessionList: Bool
    public var tapOnSessionListDismisses: Bool

    public init(
        hoverPromotesToSessionList: Bool = true,
        hoverLeaveReturnsToCompact: Bool = true,
        tapPromotesToSessionList: Bool = true,
        tapOnSessionListDismisses: Bool = true
    ) {
        self.hoverPromotesToSessionList = hoverPromotesToSessionList
        self.hoverLeaveReturnsToCompact = hoverLeaveReturnsToCompact
        self.tapPromotesToSessionList = tapPromotesToSessionList
        self.tapOnSessionListDismisses = tapOnSessionListDismisses
    }

    public func decide(event: Event, current: IslandSurfaceKind) -> Decision {
        switch event {
        case .enter:
            if current == .compact && hoverPromotesToSessionList {
                return .promote(to: .sessionList)
            }
            return .noop

        case .leave:
            if current == .sessionList && hoverLeaveReturnsToCompact {
                return .promote(to: .compact)
            }
            return .noop

        case .tap:
            if current == .compact && tapPromotesToSessionList {
                return .promote(to: .sessionList)
            }
            if current == .sessionList && tapOnSessionListDismisses {
                return .dismiss
            }
            return .noop
        }
    }
}
