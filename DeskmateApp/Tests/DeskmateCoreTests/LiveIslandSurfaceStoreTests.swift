import XCTest
@testable import DeskmateCore

final class LiveIslandSurfaceStoreTests: XCTestCase {
    private var clockNow = 0
    private lazy var clock: () -> Int = { self.clockNow }

    private func newStore() -> LiveIslandSurfaceStore {
        LiveIslandSurfaceStore(clock: clock)
    }

    func testInitialSurfaceIsEmpty() {
        let s = newStore()
        XCTAssertEqual(s.surface.kind, .empty)
        XCTAssertEqual(s.priority, .p3)
    }

    func testPresentChangesSurfaceAndNotifies() {
        let s = newStore()
        var events: [LiveIslandSurfaceStore.ChangeEvent] = []
        let unsub = s.subscribe { events.append($0) }
        defer { unsub() }

        clockNow = 1_000
        s.present(kind: .notificationCard, sessionId: "s1", priority: .p1)

        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].state.kind, .notificationCard)
        XCTAssertEqual(events[0].state.sessionId, "s1")
        XCTAssertEqual(events[0].priority, .p1)
        XCTAssertEqual(events[0].transition, .slideIn)
        XCTAssertEqual(s.lastTouchedMs, 1_000)
    }

    func testLowerPriorityCannotPreempt() {
        let s = newStore()
        s.present(kind: .notificationCard, priority: .p0)
        var events = 0
        let unsub = s.subscribe { _ in events += 1 }
        defer { unsub() }

        s.present(kind: .sessionList, priority: .p3)  // lower-rank event
        XCTAssertEqual(events, 0)
        XCTAssertEqual(s.surface.kind, .notificationCard)
        XCTAssertEqual(s.priority, .p0)
    }

    func testDismissReturnsToEmpty() {
        let s = newStore()
        s.present(kind: .sessionList, sessionId: "s1", priority: .p2)
        var events: [LiveIslandSurfaceStore.ChangeEvent] = []
        let unsub = s.subscribe { events.append($0) }
        defer { unsub() }

        s.dismiss()

        XCTAssertEqual(s.surface.kind, .empty)
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].transition, .slideOut)
    }

    func testUpdateDoesNotNotifyBecauseNoVisibleChange() {
        let s = newStore()
        s.present(kind: .liveActivity, activityId: "act-1", priority: .p2)
        var events = 0
        let unsub = s.subscribe { _ in events += 1 }
        defer { unsub() }

        s.update(activityId: "act-1")
        XCTAssertEqual(events, 0)  // reducer says changed=false
        XCTAssertEqual(s.surface.activityId, "act-1")
    }

    func testUserInteractUpdatesLastTouchedButDoesNotNotify() {
        let s = newStore()
        s.present(kind: .notificationCard, priority: .p1)
        var events = 0
        let unsub = s.subscribe { _ in events += 1 }
        defer { unsub() }

        clockNow = 5_000
        s.noteUserInteract()
        XCTAssertEqual(events, 0)
        XCTAssertEqual(s.lastTouchedMs, 5_000)
    }

    func testTickAutoDismissesLowPrioritySurface() {
        let s = newStore()
        s.present(kind: .liveActivity, priority: .p2)
        let startMs = s.lastTouchedMs
        var events: [LiveIslandSurfaceStore.ChangeEvent] = []
        let unsub = s.subscribe { events.append($0) }
        defer { unsub() }

        clockNow = startMs + 15_000  // > autoDismissMs default 10s
        s.tick()

        XCTAssertEqual(s.surface.kind, .empty)
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].transition, .slideOut)
    }

    func testTransientRestoresSteadySurfaceAfterTtl() {
        let s = newStore()
        clockNow = 1_000
        s.present(kind: .liveActivity, activityId: "steady", priority: .p1)
        clockNow = 1_100
        s.present(
            kind: .notificationCard,
            sessionId: "peek",
            activityId: "tool-running",
            priority: .p1
        )
        XCTAssertTrue(s.isTransientActive)
        XCTAssertEqual(s.surface.activityId, "tool-running")

        var events: [LiveIslandSurfaceStore.ChangeEvent] = []
        let unsub = s.subscribe { events.append($0) }
        defer { unsub() }

        clockNow = 4_000
        s.tick()

        XCTAssertFalse(s.isTransientActive)
        XCTAssertEqual(s.surface.kind, .liveActivity)
        XCTAssertEqual(s.surface.activityId, "steady")
        XCTAssertEqual(s.priority, .p1)
        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].transition, .morph)
    }

    func testTransientCanCoverExpandedSessionList() {
        let s = newStore()
        clockNow = 1_000
        s.present(kind: .sessionList, priority: .p1)

        clockNow = 1_100
        s.present(kind: .notificationCard, activityId: "reminder", priority: .p1)

        XCTAssertTrue(s.isTransientActive)
        XCTAssertEqual(s.surface.kind, .notificationCard)
        XCTAssertEqual(s.surface.activityId, "reminder")

        clockNow = 4_000
        s.tick()

        XCTAssertFalse(s.isTransientActive)
        XCTAssertEqual(s.surface.kind, .sessionList)
    }

    func testTransientQueueDrainsByPriorityThenFifo() {
        let s = newStore()
        clockNow = 1_000
        s.present(kind: .liveActivity, activityId: "steady", priority: .p2)
        clockNow = 1_100
        s.present(kind: .notificationCard, activityId: "first", priority: .p2)
        clockNow = 1_200
        s.present(kind: .notificationCard, activityId: "urgent", priority: .p1)
        clockNow = 1_300
        s.present(kind: .notificationCard, activityId: "second", priority: .p2)
        XCTAssertEqual(s.surface.activityId, "first")

        clockNow = 3_600
        s.tick()
        XCTAssertTrue(s.isTransientActive)
        XCTAssertEqual(s.surface.activityId, "urgent")

        clockNow = 6_500
        s.tick()
        XCTAssertTrue(s.isTransientActive)
        XCTAssertEqual(s.surface.activityId, "second")

        clockNow = 9_000
        s.tick()
        XCTAssertFalse(s.isTransientActive)
        XCTAssertEqual(s.surface.activityId, "steady")
    }

    func testSteadyUpdateDuringTransientIsRestored() {
        let s = newStore()
        clockNow = 1_000
        s.present(kind: .liveActivity, activityId: "steady", detail: "old", priority: .p2)
        clockNow = 1_100
        s.present(kind: .notificationCard, activityId: "peek", priority: .p2)
        clockNow = 1_200
        s.update(activityId: "steady", detail: "new")

        clockNow = 3_600
        s.tick()

        XCTAssertFalse(s.isTransientActive)
        XCTAssertEqual(s.surface.activityId, "steady")
        XCTAssertEqual(s.surface.detail, "new")
    }

    func testTransientFromIdleExpiresToEmpty() {
        let s = newStore()
        clockNow = 1_000
        s.present(kind: .notificationCard, activityId: "done", priority: .p2)
        XCTAssertTrue(s.isTransientActive)
        XCTAssertEqual(s.surface.activityId, "done")

        clockNow = 3_500
        s.tick()

        XCTAssertFalse(s.isTransientActive)
        XCTAssertEqual(s.surface.kind, .empty)
    }

    func testP0NotificationIsPinnedNotTransient() {
        let s = newStore()
        clockNow = 1_000
        s.present(kind: .notificationCard, activityId: "approval", priority: .p0)
        XCTAssertFalse(s.isTransientActive)

        clockNow = 10_000
        s.tick()

        XCTAssertEqual(s.surface.activityId, "approval")
        XCTAssertEqual(s.priority, .p0)
    }

    func testTransientCannotReplacePinnedP0Approval() {
        let s = newStore()
        clockNow = 1_000
        s.present(kind: .notificationCard, activityId: "approval", priority: .p0)

        clockNow = 1_100
        s.present(kind: .notificationCard, activityId: "tool", priority: .p1)

        XCTAssertFalse(s.isTransientActive)
        XCTAssertEqual(s.surface.activityId, "approval")
        XCTAssertEqual(s.priority, .p0)
    }

    func testUnsubscribeStopsCallbacks() {
        let s = newStore()
        var events = 0
        let unsub = s.subscribe { _ in events += 1 }
        s.present(kind: .notificationCard, priority: .p1)
        unsub()
        s.dismiss()
        XCTAssertEqual(events, 1)
        XCTAssertEqual(s.subscriberCount, 0)
    }
}
