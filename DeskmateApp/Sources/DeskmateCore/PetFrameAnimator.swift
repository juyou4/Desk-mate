import Foundation

/// Deterministic frame scheduler used by the pet renderer (V10 L2-#1).
///
/// The animator is a plain value type: given a reference start time and the
/// current wall-clock reading it returns the index into ``frames``. The
/// renderer owns the actual time source, so tests can feed synthetic times
/// without any async timer.
public struct PetFrameAnimator: Equatable, Sendable {
    public var fps: Int
    public var frameCount: Int
    public var loops: Bool

    public init(fps: Int, frameCount: Int, loops: Bool = true) {
        precondition(fps > 0, "fps must be positive")
        precondition(frameCount > 0, "frameCount must be positive")
        self.fps = fps
        self.frameCount = frameCount
        self.loops = loops
    }

    /// Duration of a single frame in milliseconds (>= 1).
    public var frameDurationMs: Int {
        max(1, 1000 / fps)
    }

    /// ``nil`` for looping animations; otherwise the total clip length.
    public var totalDurationMs: Int? {
        loops ? nil : frameDurationMs * frameCount
    }

    /// Return the index to display at ``elapsedMs`` since the clip started.
    /// Negative values clamp to 0.
    public func frameIndex(elapsedMs: Int) -> Int {
        guard elapsedMs > 0 else { return 0 }
        let raw = elapsedMs / frameDurationMs
        if loops {
            return raw % frameCount
        }
        return min(raw, frameCount - 1)
    }

    /// True once a non-looping animation has reached its last frame.
    public func isFinished(elapsedMs: Int) -> Bool {
        guard !loops, let total = totalDurationMs else { return false }
        return elapsedMs >= total
    }
}
