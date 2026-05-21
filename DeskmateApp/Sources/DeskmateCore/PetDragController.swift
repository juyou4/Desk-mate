import Foundation
import CoreGraphics

/// Click-vs-drag discriminator for the pet window (V10 L2-#1).
///
/// Receives raw AppKit-level mouse events (reduced to plain points + ts) and
/// emits high-level outputs the window layer can act on. Pure value type —
/// zero dependency on AppKit so the discriminator is unit-testable.
public struct PetDragController: Equatable {
    public enum State: Equatable {
        case idle
        case pressing(startedAtMs: Int, startPoint: CGPoint)
        case dragging(startPoint: CGPoint, lastPoint: CGPoint)
    }

    public enum Event: Equatable {
        case mouseDown(point: CGPoint, tsMs: Int)
        case mouseDragged(point: CGPoint, tsMs: Int)
        case mouseUp(point: CGPoint, tsMs: Int)
        case cancelled
    }

    public enum Output: Equatable {
        case noop
        case beganDrag(from: CGPoint)
        case drag(delta: CGPoint)
        case endedDrag(to: CGPoint)
        case tap(at: CGPoint)
        case cancelled
    }

    public var state: State
    public var clickThresholdPx: CGFloat
    public var clickThresholdMs: Int

    public init(
        state: State = .idle,
        clickThresholdPx: CGFloat = 4,
        clickThresholdMs: Int = 500
    ) {
        self.state = state
        self.clickThresholdPx = clickThresholdPx
        self.clickThresholdMs = clickThresholdMs
    }

    /// Reduce an incoming event into a new state + a high-level output.
    public mutating func apply(_ event: Event) -> Output {
        switch (state, event) {
        case (.idle, .mouseDown(let p, let ts)):
            state = .pressing(startedAtMs: ts, startPoint: p)
            return .noop

        case (.pressing(_, let start), .mouseDragged(let p, _)):
            if distance(start, p) >= clickThresholdPx {
                state = .dragging(startPoint: start, lastPoint: p)
                return .beganDrag(from: start)
            }
            return .noop

        case (.pressing(let pressedAt, let start), .mouseUp(let p, let ts)):
            state = .idle
            if ts - pressedAt <= clickThresholdMs
                && distance(start, p) < clickThresholdPx
            {
                return .tap(at: p)
            }
            return .noop  // long press / drifted — absorb silently.

        case (.dragging(let start, let last), .mouseDragged(let p, _)):
            state = .dragging(startPoint: start, lastPoint: p)
            return .drag(delta: CGPoint(x: p.x - last.x, y: p.y - last.y))

        case (.dragging, .mouseUp(let p, _)):
            state = .idle
            return .endedDrag(to: p)

        case (_, .cancelled):
            state = .idle
            return .cancelled

        default:
            // Any stray event in the wrong state: absorb without changing.
            return .noop
        }
    }

    private func distance(_ a: CGPoint, _ b: CGPoint) -> CGFloat {
        let dx = a.x - b.x
        let dy = a.y - b.y
        return (dx * dx + dy * dy).squareRoot()
    }
}
