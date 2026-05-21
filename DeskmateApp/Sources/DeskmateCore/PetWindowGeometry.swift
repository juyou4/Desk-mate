import Foundation
import CoreGraphics

/// Pure multi-screen clamping for the pet window (V10 L2-#1).
///
/// Kept fully deterministic so we can unit-test multi-display edge cases
/// without driving an AppKit runtime. The window layer feeds in the list of
/// ``PetScreen`` snapshots it captured from ``NSScreen.screens``.
public struct PetScreen: Equatable, Sendable {
    public var id: Int
    public var visibleFrame: CGRect

    public init(id: Int, visibleFrame: CGRect) {
        self.id = id
        self.visibleFrame = visibleFrame
    }
}

public struct PetWindowGeometry: Equatable, Sendable {
    public var screens: [PetScreen]
    public var petSize: CGSize
    public var edgeMargin: CGFloat

    public init(screens: [PetScreen], petSize: CGSize, edgeMargin: CGFloat = 8) {
        self.screens = screens
        self.petSize = petSize
        self.edgeMargin = edgeMargin
    }

    /// Result of ``clamp``: the corrected origin + which screen it landed on.
    public struct Resolved: Equatable, Sendable {
        public var origin: CGPoint
        public var screenId: Int
        public var didClamp: Bool
    }

    /// Clamp ``requested`` so the pet fits inside one screen's
    /// ``visibleFrame`` with at least ``edgeMargin`` of slack.
    ///
    /// - Picks the screen whose ``visibleFrame`` contains the pet's *centre*.
    /// - Falls back to the nearest screen by centre-to-centre distance when
    ///   no screen contains the centre (e.g. user dragged the pet onto a
    ///   removed external display).
    /// - Returns ``nil`` when ``screens`` is empty.
    public func clamp(requested: CGPoint) -> Resolved? {
        guard !screens.isEmpty else { return nil }
        let centre = CGPoint(
            x: requested.x + petSize.width / 2,
            y: requested.y + petSize.height / 2
        )

        let containing = screens.first { $0.visibleFrame.contains(centre) }
        let target = containing ?? nearestScreen(to: centre)
        guard let target else { return nil }

        let inset = insetFrame(target.visibleFrame)
        let maxX = max(inset.minX, inset.maxX - petSize.width)
        let maxY = max(inset.minY, inset.maxY - petSize.height)
        let clampedX = min(max(requested.x, inset.minX), maxX)
        let clampedY = min(max(requested.y, inset.minY), maxY)
        let clampedOrigin = CGPoint(x: clampedX, y: clampedY)
        return Resolved(
            origin: clampedOrigin,
            screenId: target.id,
            didClamp: clampedOrigin != requested
        )
    }

    /// Compute a reasonable default spawn origin when no persisted position
    /// exists yet: bottom-right of the *first* screen with the standard
    /// margin, which avoids overlapping with the menu bar or the Dock.
    public func defaultOrigin() -> Resolved? {
        guard let first = screens.first else { return nil }
        let inset = insetFrame(first.visibleFrame)
        let origin = CGPoint(
            x: max(inset.minX, inset.maxX - petSize.width),
            y: inset.minY
        )
        return Resolved(origin: origin, screenId: first.id, didClamp: false)
    }

    // MARK: - Internals

    private func insetFrame(_ rect: CGRect) -> CGRect {
        rect.insetBy(dx: edgeMargin, dy: edgeMargin)
    }

    private func nearestScreen(to point: CGPoint) -> PetScreen? {
        var best: PetScreen?
        var bestDist = CGFloat.infinity
        for screen in screens {
            let dx = point.x - screen.visibleFrame.midX
            let dy = point.y - screen.visibleFrame.midY
            let d = (dx * dx + dy * dy).squareRoot()
            if d < bestDist {
                bestDist = d
                best = screen
            }
        }
        return best
    }
}
