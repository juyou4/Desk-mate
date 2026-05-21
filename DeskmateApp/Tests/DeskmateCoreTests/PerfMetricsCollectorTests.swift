import XCTest
@testable import DeskmateCore

final class PerfMetricsCollectorTests: XCTestCase {

    // MARK: - Wake budget ---------------------------------------------

    func testFirstFrameWithoutWakeRecordsNothing() {
        let c = PerfMetricsCollector()
        c.markFirstFrame(at: 100.0)
        let snap = c.snapshot()
        XCTAssertNil(snap.lastWakeSeconds)
        XCTAssertFalse(c.isAwaitingFirstFrame)
    }

    func testWakeFollowedByFirstFrameLatchesElapsed() {
        let c = PerfMetricsCollector()
        c.recordWake(at: 100.0)
        XCTAssertTrue(c.isAwaitingFirstFrame)
        c.markFirstFrame(at: 100.42)
        let snap = c.snapshot()
        XCTAssertEqual(snap.lastWakeSeconds ?? -1, 0.42, accuracy: 1e-9)
        XCTAssertFalse(c.isAwaitingFirstFrame)
    }

    func testRepeatedWakesCollapseToLatestBeforeFrame() {
        let c = PerfMetricsCollector()
        c.recordWake(at: 50.0)
        c.recordWake(at: 60.0)  // overrides the earlier one
        c.markFirstFrame(at: 60.10)
        XCTAssertEqual(
            c.snapshot().lastWakeSeconds ?? -1,
            0.10,
            accuracy: 1e-9
        )
    }

    func testFrameAfterWakeClearsPendingState() {
        let c = PerfMetricsCollector()
        c.recordWake(at: 0)
        c.markFirstFrame(at: 0.3)
        XCTAssertFalse(c.isAwaitingFirstFrame)
        // A subsequent frame without a fresh wake must not overwrite
        // the latched value.
        c.markFirstFrame(at: 5.0)
        XCTAssertEqual(
            c.snapshot().lastWakeSeconds ?? -1,
            0.3,
            accuracy: 1e-9
        )
    }

    func testNegativeWakeIntervalClampsToZero() {
        let c = PerfMetricsCollector()
        c.recordWake(at: 100.0)
        // Clock skew or leap can make markFirstFrame land before
        // the wake; the budget is not negative.
        c.markFirstFrame(at: 99.9)
        XCTAssertEqual(c.snapshot().lastWakeSeconds, 0.0)
    }

    // MARK: - Frame drops ---------------------------------------------

    func testFirstTickDoesNotCountTowardTotal() {
        let c = PerfMetricsCollector()
        c.recordFrameTick(at: 0, expectedPeriod: 1.0/60.0)
        XCTAssertEqual(c.snapshot().totalFrames, 0)
        XCTAssertEqual(c.snapshot().droppedFrames, 0)
    }

    func testOnTimeTicksAreNotDropped() {
        let c = PerfMetricsCollector()
        let period = 1.0/60.0
        var t = 0.0
        for _ in 0..<60 {
            c.recordFrameTick(at: t, expectedPeriod: period)
            t += period
        }
        let snap = c.snapshot()
        XCTAssertEqual(snap.totalFrames, 59)  // first tick uncounted
        XCTAssertEqual(snap.droppedFrames, 0)
        XCTAssertEqual(snap.frameDropRatio, 0)
        XCTAssertEqual(snap.frameDropPct, 0)
    }

    func testIntervalOverToleranceCountsAsOneDrop() {
        let c = PerfMetricsCollector(dropTolerance: 1.5)
        let period = 1.0/60.0
        c.recordFrameTick(at: 0, expectedPeriod: period)
        // Next tick comes 2× period later → drop.
        c.recordFrameTick(at: period * 2.0, expectedPeriod: period)
        XCTAssertEqual(c.snapshot().droppedFrames, 1)
        XCTAssertEqual(c.snapshot().totalFrames, 1)
    }

    func testFrameDropRatioIsBounded() {
        let c = PerfMetricsCollector(dropTolerance: 1.5)
        let period = 1.0/60.0
        // 4 ticks: 1 baseline + 3 long gaps → 3/3 dropped.
        c.recordFrameTick(at: 0, expectedPeriod: period)
        for i in 1...3 {
            c.recordFrameTick(
                at: Double(i) * period * 2.0,
                expectedPeriod: period
            )
        }
        let snap = c.snapshot()
        XCTAssertEqual(snap.totalFrames, 3)
        XCTAssertEqual(snap.droppedFrames, 3)
        XCTAssertEqual(snap.frameDropRatio, 1.0)
        XCTAssertEqual(snap.frameDropPct, 100.0)
    }

    func testZeroExpectedPeriodIsIgnored() {
        let c = PerfMetricsCollector()
        c.recordFrameTick(at: 0, expectedPeriod: 0)
        c.recordFrameTick(at: 1.0, expectedPeriod: 0)
        XCTAssertEqual(c.snapshot().totalFrames, 0)
    }

    func testNonMonotonicTimestampIsIgnored() {
        let c = PerfMetricsCollector()
        let period = 1.0/60.0
        c.recordFrameTick(at: 1.0, expectedPeriod: period)
        // Clock went backwards; we just store and skip the count.
        c.recordFrameTick(at: 0.5, expectedPeriod: period)
        XCTAssertEqual(c.snapshot().totalFrames, 0)
    }

    // MARK: - Reset ----------------------------------------------------

    func testResetFrameStatsClearsCountersButPreservesWake() {
        let c = PerfMetricsCollector()
        c.recordWake(at: 0)
        c.markFirstFrame(at: 0.2)
        c.recordFrameTick(at: 0, expectedPeriod: 1.0/60.0)
        c.recordFrameTick(at: 1.0, expectedPeriod: 1.0/60.0)
        XCTAssertGreaterThan(c.snapshot().totalFrames, 0)
        c.resetFrameStats()
        let snap = c.snapshot()
        XCTAssertEqual(snap.totalFrames, 0)
        XCTAssertEqual(snap.droppedFrames, 0)
        XCTAssertEqual(snap.lastWakeSeconds ?? -1, 0.2, accuracy: 1e-9)
    }

    func testResetFrameStatsRestartsBaseline() {
        let c = PerfMetricsCollector()
        let period = 1.0/60.0
        c.recordFrameTick(at: 0, expectedPeriod: period)
        c.recordFrameTick(at: period, expectedPeriod: period)
        c.resetFrameStats()
        // First tick after reset is the new baseline; no count yet.
        c.recordFrameTick(at: 100.0, expectedPeriod: period)
        XCTAssertEqual(c.snapshot().totalFrames, 0)
        c.recordFrameTick(at: 100.0 + period, expectedPeriod: period)
        XCTAssertEqual(c.snapshot().totalFrames, 1)
    }

    // MARK: - Snapshot codable ----------------------------------------

    func testSnapshotRoundTripsThroughJSON() throws {
        let original = PerfMetricsSnapshot(
            lastWakeSeconds: 0.42,
            totalFrames: 100,
            droppedFrames: 3,
            frameDropRatio: 0.03
        )
        let data = try JSONEncoder().encode(original)
        let decoded = try JSONDecoder().decode(
            PerfMetricsSnapshot.self,
            from: data
        )
        XCTAssertEqual(decoded, original)
        XCTAssertEqual(decoded.frameDropPct, 3.0, accuracy: 1e-9)
    }
}
