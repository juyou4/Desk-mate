import XCTest
@testable import DeskmateCore

final class LiveDomainStateStoreTests: XCTestCase {
    func testInitialStateIsTheSuppliedDefault() {
        let s = LiveDomainStateStore()
        XCTAssertEqual(s.current.pendingApprovals, [])
        XCTAssertEqual(s.current.agentMood, .idle)
    }

    func testApplyNewStateReturnsTrueAndUpdatesCurrent() {
        let s = LiveDomainStateStore()
        let next = DomainState(pendingApprovals: ["ap-1"])
        let changed = s.apply(next)
        XCTAssertTrue(changed)
        XCTAssertEqual(s.current, next)
    }

    func testApplyIdenticalStateIsNoopAndReturnsFalse() {
        let s = LiveDomainStateStore(initial: DomainState(pendingApprovals: ["a"]))
        let changed = s.apply(DomainState(pendingApprovals: ["a"]))
        XCTAssertFalse(changed)
    }

    func testSubscribersReceiveEveryUniqueUpdate() {
        let s = LiveDomainStateStore()
        var received: [[String]] = []
        let unsub = s.subscribe { received.append($0.pendingApprovals) }
        defer { unsub() }

        s.apply(DomainState(pendingApprovals: ["a"]))
        s.apply(DomainState(pendingApprovals: ["a"]))  // dupe, ignored
        s.apply(DomainState(pendingApprovals: ["a", "b"]))

        XCTAssertEqual(received, [["a"], ["a", "b"]])
    }

    func testMultipleSubscribersAllFire() {
        let s = LiveDomainStateStore()
        var a = 0, b = 0
        let u1 = s.subscribe { _ in a += 1 }
        let u2 = s.subscribe { _ in b += 1 }
        defer { u1(); u2() }

        s.apply(DomainState(pendingApprovals: ["x"]))

        XCTAssertEqual(a, 1)
        XCTAssertEqual(b, 1)
    }

    func testUnsubscribeStopsCallbacks() {
        let s = LiveDomainStateStore()
        var fired = 0
        let unsub = s.subscribe { _ in fired += 1 }

        s.apply(DomainState(pendingApprovals: ["x"]))
        unsub()
        s.apply(DomainState(pendingApprovals: ["x", "y"]))

        XCTAssertEqual(fired, 1)
        XCTAssertEqual(s.subscriberCount, 0)
    }

    func testSubscribeDoesNotFireWithInitialState() {
        // Matches subscribe-then-observe semantics of most reactive
        // libs; the initial value is reachable via `current` instead.
        let s = LiveDomainStateStore(initial: DomainState(pendingApprovals: ["x"]))
        var fired = false
        let unsub = s.subscribe { _ in fired = true }
        defer { unsub() }
        XCTAssertFalse(fired)
    }
}
