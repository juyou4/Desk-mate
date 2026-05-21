import XCTest
@testable import DeskmateCore

final class BubbleQueueTests: XCTestCase {
    private func bubble(_ id: String, priority: Priority = .p2, ttl: Int? = 8000) -> BubbleSpec {
        BubbleSpec(id: id, kind: .chat, text: id, ttlMs: ttl, priority: priority)
    }

    // MARK: - Ordering

    func testFIFOWithinSamePriority() {
        var q = BubbleQueue(maxActive: 10)
        q.enqueue(bubble("a"), nowMs: 100)
        q.enqueue(bubble("b"), nowMs: 200)
        q.enqueue(bubble("c"), nowMs: 300)
        XCTAssertEqual(q.dequeue(nowMs: 400)?.id, "a")
        XCTAssertEqual(q.dequeue(nowMs: 401)?.id, "b")
        XCTAssertEqual(q.dequeue(nowMs: 402)?.id, "c")
    }

    func testHigherPriorityJumpsQueue() {
        var q = BubbleQueue(maxActive: 10)
        q.enqueue(bubble("p2a", priority: .p2), nowMs: 100)
        q.enqueue(bubble("p0",  priority: .p0), nowMs: 200)
        q.enqueue(bubble("p2b", priority: .p2), nowMs: 300)
        XCTAssertEqual(q.dequeue(nowMs: 400)?.id, "p0")
        XCTAssertEqual(q.dequeue(nowMs: 401)?.id, "p2a")
        XCTAssertEqual(q.dequeue(nowMs: 402)?.id, "p2b")
    }

    // MARK: - TTL

    func testExpiredEntriesArePrunedOnDequeue() {
        var q = BubbleQueue(maxActive: 10)
        q.enqueue(bubble("short", ttl: 100), nowMs: 0)
        q.enqueue(bubble("long",  ttl: 10_000), nowMs: 0)
        XCTAssertEqual(q.dequeue(nowMs: 500)?.id, "long")
    }

    func testPeekDoesNotConsumeEntries() {
        var q = BubbleQueue(maxActive: 10)
        q.enqueue(bubble("a"), nowMs: 0)
        XCTAssertEqual(q.peek(nowMs: 1)?.id, "a")
        XCTAssertEqual(q.peek(nowMs: 1)?.id, "a")
        XCTAssertEqual(q.count, 1)
    }

    func testPruneRemovesExpiredInPlace() {
        var q = BubbleQueue(maxActive: 10)
        q.enqueue(bubble("a", ttl: 100), nowMs: 0)
        q.enqueue(bubble("b", ttl: 10_000), nowMs: 0)
        q.prune(nowMs: 500)
        XCTAssertEqual(q.count, 1)
        XCTAssertEqual(q.allEntries.first?.spec.id, "b")
    }

    func testNilTTLMeansPersistent() {
        var q = BubbleQueue(maxActive: 10)
        q.enqueue(bubble("sticky", ttl: nil), nowMs: 0)
        XCTAssertEqual(q.peek(nowMs: 1_000_000_000)?.id, "sticky")
    }

    // MARK: - Overflow

    func testOverflowEvictsLowestPriorityOldest() {
        var q = BubbleQueue(maxActive: 3)
        q.enqueue(bubble("p3a", priority: .p3), nowMs: 100)
        q.enqueue(bubble("p2a", priority: .p2), nowMs: 200)
        q.enqueue(bubble("p2b", priority: .p2), nowMs: 300)
        // p3a is lowest priority and oldest → evicted first.
        q.enqueue(bubble("p1",  priority: .p1), nowMs: 400)
        XCTAssertEqual(q.count, 3)
        let ids = q.allEntries.map(\.spec.id)
        XCTAssertFalse(ids.contains("p3a"))
        XCTAssertTrue(ids.contains("p1"))
    }

    // MARK: - Mutation API

    func testRemoveById() {
        var q = BubbleQueue()
        q.enqueue(bubble("a"), nowMs: 0)
        q.enqueue(bubble("b"), nowMs: 0)
        q.remove(id: "a")
        XCTAssertEqual(q.count, 1)
        XCTAssertEqual(q.allEntries.first?.spec.id, "b")
    }

    func testClear() {
        var q = BubbleQueue()
        q.enqueue(bubble("a"), nowMs: 0)
        q.enqueue(bubble("b"), nowMs: 0)
        q.clear()
        XCTAssertTrue(q.isEmpty)
    }
}
