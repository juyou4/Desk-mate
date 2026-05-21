import XCTest
@testable import DeskmateCore

final class PetFrameAnimatorTests: XCTestCase {
    func testFrameDurationDerivedFromFPS() {
        let a = PetFrameAnimator(fps: 4, frameCount: 8)
        XCTAssertEqual(a.frameDurationMs, 250)
    }

    func testFrameDurationHasMinimumOfOneMs() {
        let a = PetFrameAnimator(fps: 10_000, frameCount: 2)
        XCTAssertGreaterThanOrEqual(a.frameDurationMs, 1)
    }

    func testFrameIndexLoopsAcrossFrameCount() {
        let a = PetFrameAnimator(fps: 4, frameCount: 3, loops: true)  // 250ms/frame
        XCTAssertEqual(a.frameIndex(elapsedMs: 0), 0)
        XCTAssertEqual(a.frameIndex(elapsedMs: 125), 0)
        XCTAssertEqual(a.frameIndex(elapsedMs: 250), 1)
        XCTAssertEqual(a.frameIndex(elapsedMs: 500), 2)
        // Wraps back to frame 0 at index 3.
        XCTAssertEqual(a.frameIndex(elapsedMs: 750), 0)
        XCTAssertEqual(a.frameIndex(elapsedMs: 1_000), 1)
    }

    func testNonLoopingClampsAtLastFrame() {
        let a = PetFrameAnimator(fps: 4, frameCount: 3, loops: false)
        XCTAssertEqual(a.frameIndex(elapsedMs: 2_000), 2)
        XCTAssertEqual(a.frameIndex(elapsedMs: 10_000), 2)
    }

    func testTotalDurationMsIsNilForLoops() {
        XCTAssertNil(PetFrameAnimator(fps: 4, frameCount: 3, loops: true).totalDurationMs)
    }

    func testTotalDurationMsForNonLoops() {
        let a = PetFrameAnimator(fps: 4, frameCount: 3, loops: false)
        XCTAssertEqual(a.totalDurationMs, 750)
    }

    func testIsFinishedOnlyForNonLoopingClips() {
        let looping = PetFrameAnimator(fps: 4, frameCount: 3, loops: true)
        XCTAssertFalse(looping.isFinished(elapsedMs: 10_000))

        let oneShot = PetFrameAnimator(fps: 4, frameCount: 3, loops: false)
        XCTAssertFalse(oneShot.isFinished(elapsedMs: 500))
        XCTAssertTrue(oneShot.isFinished(elapsedMs: 750))
        XCTAssertTrue(oneShot.isFinished(elapsedMs: 10_000))
    }

    func testNegativeElapsedReturnsFrameZero() {
        let a = PetFrameAnimator(fps: 4, frameCount: 3)
        XCTAssertEqual(a.frameIndex(elapsedMs: -100), 0)
    }
}
