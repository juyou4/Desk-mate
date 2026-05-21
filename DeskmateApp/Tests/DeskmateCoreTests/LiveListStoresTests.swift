#if canImport(Darwin)
import Darwin
#endif
import XCTest
@testable import DeskmateCore

final class LiveListStoresTests: XCTestCase {
    // MARK: - LiveListStore<Row>

    func testGenericStoreFiresInitialValueOnSubscribe() {
        let store = LiveListStore<Int>(initial: [1, 2, 3])
        var seen: [Int] = []
        _ = store.subscribe { seen = $0 }
        XCTAssertEqual(seen, [1, 2, 3])
    }

    func testGenericStoreApplyNotifiesOnChangeOnly() {
        let store = LiveListStore<Int>()
        var hits = 0
        _ = store.subscribe { _ in hits += 1 }
        XCTAssertEqual(hits, 1, "initial fire")
        XCTAssertTrue(store.apply([1, 2]))
        XCTAssertEqual(hits, 2)
        XCTAssertFalse(store.apply([1, 2]), "dedup")
        XCTAssertEqual(hits, 2)
        XCTAssertTrue(store.apply([1, 2, 3]))
        XCTAssertEqual(hits, 3)
    }

    func testGenericStoreUnsubStopsCallbacks() {
        let store = LiveListStore<Int>()
        var hits = 0
        let unsub = store.subscribe { _ in hits += 1 }
        XCTAssertEqual(hits, 1)
        unsub()
        let settle = expectation(description: "unsub settled")
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) {
            settle.fulfill()
        }
        wait(for: [settle], timeout: 2.0)
        store.apply([9])
        XCTAssertEqual(hits, 1, "unsubbed callback must not see [9]")
    }

    // MARK: - Concrete wrappers

    func testLiveSessionListStoreApplyAndSubscribe() {
        let store = LiveSessionListStore()
        var last: [SessionRow] = []
        _ = store.subscribe { last = $0 }
        let row = SessionRow(sessionId: "s1", title: "Design review")
        store.apply([row])
        XCTAssertEqual(last, [row])
        XCTAssertEqual(store.current, [row])
    }

    func testLivePendingRemindersStoreApplyAndSubscribe() {
        let store = LivePendingRemindersStore()
        var last: [ReminderRow] = []
        _ = store.subscribe { last = $0 }
        let row = ReminderRow(reminderId: "r1", text: "stand up")
        store.apply([row])
        XCTAssertEqual(last, [row])
    }

    func testLivePendingApprovalsStoreApplyAndSubscribe() {
        let store = LivePendingApprovalsStore()
        var last: [ApprovalRow] = []
        _ = store.subscribe { last = $0 }
        let row = ApprovalRow(approvalId: "a1", prompt: "read clipboard?")
        store.apply([row])
        XCTAssertEqual(last, [row])
    }

    // MARK: - Hydrator integration

    func testHydratorPopulatesAllThreeListStores() {
        let domain = LiveDomainStateStore()
        let sessions = LiveSessionListStore()
        let reminders = LivePendingRemindersStore()
        let approvals = LivePendingApprovalsStore()
        let h = SnapshotHydrator(
            domainStore: domain,
            sessionStore: sessions,
            reminderStore: reminders,
            approvalStore: approvals,
            callbackQueue: .main
        )
        let env = BridgeEnvelope.of(
            .stateSnapshot,
            payload: [
                "domain_state": .object([:]),
                "active_sessions": .array([
                    .object([
                        "session_id": .string("s-1"),
                        "title": .string("Design review"),
                        "state": .string("active"),
                        "priority": .string("P1"),
                        "created_at_ms": .int(1_000),
                        "updated_at_ms": .int(2_000),
                    ])
                ]),
                "pending_reminders": .array([
                    .object([
                        "reminder_id": .string("r-1"),
                        "text": .string("stand up"),
                        "due_at_ms": .int(5_000),
                        "status": .string("pending"),
                    ])
                ]),
                "pending_approvals_detail": .array([
                    .object([
                        "approval_id": .string("ap-1"),
                        "prompt": .string("allow clipboard?"),
                        "status": .string("pending"),
                    ])
                ]),
            ]
        )
        h.handle(env)
        XCTAssertEqual(sessions.current.map(\.sessionId), ["s-1"])
        XCTAssertEqual(sessions.current.first?.title, "Design review")
        XCTAssertEqual(reminders.current.map(\.reminderId), ["r-1"])
        XCTAssertEqual(approvals.current.map(\.approvalId), ["ap-1"])
    }

    func testHydratorEmptyListFieldClearsAStore() {
        let store = LivePendingApprovalsStore()
        store.apply([ApprovalRow(approvalId: "stale")])
        XCTAssertEqual(store.current.count, 1)
        let h = SnapshotHydrator(
            domainStore: LiveDomainStateStore(),
            approvalStore: store,
            callbackQueue: .main
        )
        let env = BridgeEnvelope.of(
            .stateSnapshot,
            payload: [
                "domain_state": .object([:]),
                "pending_approvals_detail": .array([]),
            ]
        )
        h.handle(env)
        XCTAssertEqual(store.current, [], "empty snapshot list must clear store")
    }

    func testHydratorMalformedListFiresDecodeErrorAndKeepsStore() {
        let store = LiveSessionListStore()
        let existing = SessionRow(sessionId: "keep")
        store.apply([existing])
        let h = SnapshotHydrator(
            domainStore: LiveDomainStateStore(),
            sessionStore: store,
            callbackQueue: .main
        )
        let errExp = expectation(description: "decode error")
        h.onDecodeError { _ in errExp.fulfill() }
        let env = BridgeEnvelope.of(
            .stateSnapshot,
            payload: [
                "domain_state": .object([:]),
                "active_sessions": .string("not-a-list"),
            ]
        )
        h.handle(env)
        wait(for: [errExp], timeout: 2.0)
        XCTAssertEqual(store.current, [existing], "store must be untouched")
    }

    // MARK: - Shell integration

    func testShellExposesTheThreeListStoresAndHydratesThem() throws {
        final class Harness {
            var peerFds: [Int32] = []
            func factory() throws -> BridgeClient {
                let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
                let client = BridgeClient(callbackQueue: .main)
                try client.start(preConnectedFd: clientFd)
                peerFds.append(agentFd)
                return client
            }
        }
        let harness = Harness()
        var config = DeskmateShell.Configuration(
            bridgeBackoff: .init(
                initialBackoff: 0.01, maxBackoff: 0.05,
                multiplier: 2.0, jitterFraction: 0
            )
        )
        config.clientFactory = harness.factory
        let shell = DeskmateShell(configuration: config, callbackQueue: .main)
        defer {
            shell.stop()
            for fd in harness.peerFds { close(fd) }
        }
        let connected = expectation(description: "connected")
        shell.bridge.onStateChange { s in if s == .connected { connected.fulfill() } }
        shell.start()
        wait(for: [connected], timeout: 2.0)

        let snap = BridgeEnvelope.of(
            .stateSnapshot,
            payload: [
                "domain_state": .object([:]),
                "active_sessions": .array([.object([
                    "session_id": .string("s-42")
                ])]),
                "pending_reminders": .array([.object([
                    "reminder_id": .string("r-42")
                ])]),
                "pending_approvals_detail": .array([.object([
                    "approval_id": .string("ap-42")
                ])]),
            ]
        )
        let data = try EnvelopeFraming.encode(snap)
        data.withUnsafeBytes { raw in
            _ = Darwin.write(harness.peerFds[0], raw.baseAddress, raw.count)
        }

        let allHydrated = expectation(description: "all three stores hydrated")
        allHydrated.expectedFulfillmentCount = 3
        let us1 = shell.sessionList.subscribe { rows in
            if rows.first?.sessionId == "s-42" { allHydrated.fulfill() }
        }
        let us2 = shell.pendingReminders.subscribe { rows in
            if rows.first?.reminderId == "r-42" { allHydrated.fulfill() }
        }
        let us3 = shell.pendingApprovals.subscribe { rows in
            if rows.first?.approvalId == "ap-42" { allHydrated.fulfill() }
        }
        defer { us1(); us2(); us3() }
        wait(for: [allHydrated], timeout: 2.0)
    }
}
