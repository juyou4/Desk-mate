import XCTest
@testable import DeskmateCore

final class LiveBubbleQueueTests: XCTestCase {
    private var clockNow = 0
    private lazy var clock: () -> Int = { self.clockNow }

    private func bubble(
        _ id: String, priority: Priority = .p2, ttlMs: Int? = nil
    ) -> BubbleSpec {
        BubbleSpec(id: id, ttlMs: ttlMs, priority: priority)
    }

    func testEnqueueNotifiesSubscribers() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        var received: [Int] = []
        let unsub = q.subscribe { received.append($0.count) }
        defer { unsub() }

        q.enqueue(bubble("a"))
        q.enqueue(bubble("b"))

        XCTAssertEqual(received, [1, 2])
        XCTAssertEqual(q.count, 2)
    }

    func testDismissMatchingIdFiresOneNotification() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        q.enqueue(bubble("a"))
        q.enqueue(bubble("b"))

        var events = 0
        let unsub = q.subscribe { _ in events += 1 }
        defer { unsub() }

        q.dismiss(id: "a")
        XCTAssertEqual(events, 1)
        XCTAssertEqual(q.count, 1)
    }

    func testDismissUnknownIdIsNoopAndDoesNotNotify() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        q.enqueue(bubble("a"))
        var events = 0
        let unsub = q.subscribe { _ in events += 1 }
        defer { unsub() }

        q.dismiss(id: "ghost")
        XCTAssertEqual(events, 0)
        XCTAssertEqual(q.count, 1)
    }

    func testDequeuePullsHighestPriorityFirst() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        q.enqueue(bubble("low", priority: .p3))
        q.enqueue(bubble("high", priority: .p0))
        q.enqueue(bubble("mid", priority: .p2))

        XCTAssertEqual(q.dequeue()?.id, "high")
        XCTAssertEqual(q.dequeue()?.id, "mid")
        XCTAssertEqual(q.dequeue()?.id, "low")
        XCTAssertNil(q.dequeue())
    }

    func testPeekDoesNotMutateOrNotify() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        q.enqueue(bubble("a"))
        var events = 0
        let unsub = q.subscribe { _ in events += 1 }
        defer { unsub() }

        _ = q.peek()
        XCTAssertEqual(events, 0)
        XCTAssertEqual(q.count, 1)
    }

    func testPruneRemovesExpiredAndFiresNotification() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        clockNow = 0
        q.enqueue(bubble("a", ttlMs: 1_000))
        q.enqueue(bubble("b", ttlMs: 10_000))
        var events = 0
        let unsub = q.subscribe { _ in events += 1 }
        defer { unsub() }

        clockNow = 2_000
        q.prune()
        XCTAssertEqual(events, 1)
        XCTAssertEqual(q.count, 1)  // only "b" survives
    }

    func testClearNoopWhenEmpty() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        var events = 0
        let unsub = q.subscribe { _ in events += 1 }
        defer { unsub() }

        q.clear()
        XCTAssertEqual(events, 0)
    }

    func testClearNonEmptyFiresOnce() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        q.enqueue(bubble("a"))
        q.enqueue(bubble("b"))
        var events = 0
        let unsub = q.subscribe { _ in events += 1 }
        defer { unsub() }

        q.clear()
        XCTAssertEqual(events, 1)
        XCTAssertTrue(q.isEmpty)
    }

    func testUnsubscribeStopsNotifications() {
        let q = LiveBubbleQueue(maxActive: 5, clock: clock)
        var events = 0
        let unsub = q.subscribe { _ in events += 1 }
        q.enqueue(bubble("a"))
        unsub()
        q.enqueue(bubble("b"))
        XCTAssertEqual(events, 1)
        XCTAssertEqual(q.subscriberCount, 0)
    }
}
