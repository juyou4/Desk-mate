#if canImport(Darwin)
import Darwin
#endif
import XCTest
@testable import DeskmateCore

final class SnapshotHydratorTests: XCTestCase {
    // MARK: - Unit

    func testHandleAppliesDomainStateToStore() {
        let store = LiveDomainStateStore()
        let hydrator = SnapshotHydrator(domainStore: store, callbackQueue: .main)

        let env = BridgeEnvelope.of(
            .stateSnapshot,
            payload: [
                "domain_state": .object([
                    "pending_approvals": .array([.string("ap-1")]),
                    "agent_mood": .string("alert"),
                ]),
                "active_sessions": .array([]),
            ]
        )
        hydrator.handle(env)
        XCTAssertEqual(store.current.pendingApprovals, ["ap-1"])
        XCTAssertEqual(store.current.agentMood, .alert)
    }

    func testHandleIgnoresNonSnapshotEnvelopes() {
        let store = LiveDomainStateStore()
        let hydrator = SnapshotHydrator(domainStore: store, callbackQueue: .main)

        let env = BridgeEnvelope.of(
            .intent,
            payload: [
                "kind": .string("update_domain_state"),
                "payload": .object(["domain_state": .object([
                    "pending_approvals": .array([.string("x")])
                ])]),
            ]
        )
        hydrator.handle(env)
        XCTAssertEqual(store.current.pendingApprovals, [])
    }

    func testHandleWithMissingDomainStateIsNoop() {
        let store = LiveDomainStateStore()
        let hydrator = SnapshotHydrator(domainStore: store, callbackQueue: .main)

        let env = BridgeEnvelope.of(
            .stateSnapshot,
            payload: ["active_sessions": .array([])]
        )
        hydrator.handle(env)
        XCTAssertEqual(store.current, DomainState())
    }

    func testHandleMalformedDomainStateFiresDecodeError() {
        let store = LiveDomainStateStore()
        let hydrator = SnapshotHydrator(domainStore: store, callbackQueue: .main)
        let exp = expectation(description: "decode error")
        hydrator.onDecodeError { _ in exp.fulfill() }

        let env = BridgeEnvelope.of(
            .stateSnapshot,
            payload: ["domain_state": .string("not-an-object")]
        )
        hydrator.handle(env)
        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(store.current, DomainState())
    }

    func testOnSnapshotSurfacesRawPayloadToSubscribers() {
        let store = LiveDomainStateStore()
        let hydrator = SnapshotHydrator(domainStore: store, callbackQueue: .main)
        let exp = expectation(description: "raw snapshot seen")
        var received: [String: AnyJSONValue] = [:]
        hydrator.onSnapshot { payload in
            received = payload
            exp.fulfill()
        }

        let env = BridgeEnvelope.of(
            .stateSnapshot,
            payload: [
                "domain_state": .object([:]),
                "active_sessions": .array([.object(["id": .string("s1")])]),
                "pending_approvals_detail": .array([]),
            ]
        )
        hydrator.handle(env)
        wait(for: [exp], timeout: 2.0)
        XCTAssertNotNil(received["active_sessions"])
    }

    // MARK: - Multi-subscriber on BridgeClient

    func testBridgeClientOnEnvelopeSupportsMultipleSubscribers() throws {
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let client = BridgeClient(callbackQueue: .main)
        try client.start(preConnectedFd: clientFd)
        defer { client.stop(); close(agentFd) }

        let exp = expectation(description: "both subs fire")
        exp.expectedFulfillmentCount = 2
        var aCount = 0
        var bCount = 0
        client.onEnvelope { _ in aCount += 1; exp.fulfill() }
        client.onEnvelope { _ in bCount += 1; exp.fulfill() }

        let data = try EnvelopeFraming.encode(
            BridgeEnvelope.of(.ping, traceId: "t")
        )
        data.withUnsafeBytes { raw in
            _ = Darwin.write(agentFd, raw.baseAddress, raw.count)
        }
        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(aCount, 1)
        XCTAssertEqual(bCount, 1)
    }

    func testBridgeClientOnEnvelopeUnsubStopsDeliveryToThatCallback() throws {
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let client = BridgeClient(callbackQueue: .main)
        try client.start(preConnectedFd: clientFd)
        defer { client.stop(); close(agentFd) }

        var keptCount = 0
        var droppedCount = 0
        let keptExp = expectation(description: "kept fires twice")
        keptExp.expectedFulfillmentCount = 2
        _ = client.onEnvelope { _ in keptCount += 1; keptExp.fulfill() }
        let unsub = client.onEnvelope { _ in droppedCount += 1 }

        // First envelope — both see it.
        let e1 = try EnvelopeFraming.encode(
            BridgeEnvelope.of(.ping, traceId: "t1")
        )
        e1.withUnsafeBytes { raw in
            _ = Darwin.write(agentFd, raw.baseAddress, raw.count)
        }
        // Wait for droppedCount to become 1 before unsubbing.
        let firstDrop = expectation(description: "first dropped seen")
        let poll = DispatchQueue(label: "poll")
        func waitDrop() {
            if droppedCount >= 1 { firstDrop.fulfill(); return }
            poll.asyncAfter(deadline: .now() + 0.01, execute: waitDrop)
        }
        waitDrop()
        wait(for: [firstDrop], timeout: 2.0)

        unsub()
        // unsub is async; settle the ioQueue before sending the next one.
        let settled = expectation(description: "unsub settled")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { settled.fulfill() }
        wait(for: [settled], timeout: 2.0)

        let e2 = try EnvelopeFraming.encode(
            BridgeEnvelope.of(.ping, traceId: "t2")
        )
        e2.withUnsafeBytes { raw in
            _ = Darwin.write(agentFd, raw.baseAddress, raw.count)
        }
        wait(for: [keptExp], timeout: 2.0)
        XCTAssertEqual(keptCount, 2)
        XCTAssertEqual(droppedCount, 1, "unsubbed handler must not see t2")
    }

    // MARK: - Shell integration

    func testShellSnapshotEnvelopeHydratesDomainState() throws {
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

        shell.start()
        let connected = expectation(description: "connected")
        shell.bridge.onStateChange { s in
            if s == .connected { connected.fulfill() }
        }
        wait(for: [connected], timeout: 2.0)

        let snapshot = BridgeEnvelope.of(
            .stateSnapshot,
            payload: [
                "domain_state": .object([
                    "pending_approvals": .array([.string("ap-X")]),
                    "agent_mood": .string("alert"),
                    "active_session_id": .string("s-1"),
                ]),
                "active_sessions": .array([]),
            ],
            traceId: "snap-1"
        )
        let data = try EnvelopeFraming.encode(snapshot)
        data.withUnsafeBytes { raw in
            _ = Darwin.write(harness.peerFds[0], raw.baseAddress, raw.count)
        }

        let hydrated = expectation(description: "domain hydrated")
        let unsub = shell.domainState.subscribe { state in
            if state.pendingApprovals == ["ap-X"]
                && state.agentMood == .alert
                && state.activeSessionId == "s-1" {
                hydrated.fulfill()
            }
        }
        defer { unsub() }
        wait(for: [hydrated], timeout: 2.0)
    }
}
