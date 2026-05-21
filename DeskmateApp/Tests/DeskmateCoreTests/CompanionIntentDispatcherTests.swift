import XCTest
@testable import DeskmateCore

final class CompanionIntentDispatcherTests: XCTestCase {
    // ------------------------------------------------------------------
    // Basic routing
    // ------------------------------------------------------------------

    func testDispatchCallsRegisteredHandler() {
        let dispatcher = CompanionIntentDispatcher()
        var captured: CompanionIntent?
        dispatcher.register(kind: .showPetBubble) { captured = $0 }

        let intent = CompanionIntent(kind: .showPetBubble)
        let result = dispatcher.dispatch(intent)

        XCTAssertEqual(result, .handled(.showPetBubble))
        XCTAssertEqual(captured, intent)
    }

    func testDispatchUnknownKindIsDropped() {
        let dispatcher = CompanionIntentDispatcher()
        var fired = false
        // Registration of .unknown is rejected.
        dispatcher.register(kind: .unknown) { _ in fired = true }
        XCTAssertFalse(dispatcher.hasHandler(for: .unknown))

        let intent = CompanionIntent(kind: .unknown)
        let result = dispatcher.dispatch(intent)

        XCTAssertEqual(result, .droppedUnknown)
        XCTAssertFalse(fired)
    }

    func testDispatchNoHandlerReturnsNoHandler() {
        let dispatcher = CompanionIntentDispatcher()
        let result = dispatcher.dispatch(CompanionIntent(kind: .setAvatarMood))
        XCTAssertEqual(result, .noHandler(.setAvatarMood))
    }

    func testRegisterReplacesPriorHandler() {
        let dispatcher = CompanionIntentDispatcher()
        var firstCount = 0
        var secondCount = 0
        dispatcher.register(kind: .showPetBubble) { _ in firstCount += 1 }
        dispatcher.register(kind: .showPetBubble) { _ in secondCount += 1 }
        dispatcher.dispatch(CompanionIntent(kind: .showPetBubble))
        XCTAssertEqual(firstCount, 0)
        XCTAssertEqual(secondCount, 1)
    }

    func testUnregisterRemovesHandler() {
        let dispatcher = CompanionIntentDispatcher()
        dispatcher.register(kind: .showPetBubble) { _ in }
        XCTAssertTrue(dispatcher.hasHandler(for: .showPetBubble))
        dispatcher.unregister(kind: .showPetBubble)
        XCTAssertFalse(dispatcher.hasHandler(for: .showPetBubble))
    }

    // ------------------------------------------------------------------
    // bindDomainState / decodeDomainState
    // ------------------------------------------------------------------

    func testDecodeDomainStatePullsNestedDomainStateField() throws {
        let raw = #"""
        {
          "kind": "update_domain_state",
          "payload": {
            "domain_state": {
              "pending_approvals": ["ap-1"],
              "active_session_id": "s-7",
              "agent_mood": "alert"
            }
          }
        }
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        let state = try CompanionIntentDispatcher.decodeDomainState(from: intent)
        XCTAssertEqual(state.pendingApprovals, ["ap-1"])
        XCTAssertEqual(state.activeSessionId, "s-7")
        XCTAssertEqual(state.agentMood, .alert)
    }

    func testDecodeDomainStateThrowsWhenFieldMissing() {
        let intent = CompanionIntent(kind: .updateDomainState)
        XCTAssertThrowsError(
            try CompanionIntentDispatcher.decodeDomainState(from: intent)
        )
    }

    func testBindDomainStateAppliesIncomingUpdate() throws {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveDomainStateStore()
        dispatcher.bindDomainState(to: store)

        let raw = #"""
        {"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"]}}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(store.current.pendingApprovals, ["ap-1"])
    }

    func testBindDomainStateForwardsDecodeErrorToHook() {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveDomainStateStore()
        var errors: [Error] = []
        dispatcher.bindDomainState(to: store) { errors.append($0) }

        let malformed = CompanionIntent(kind: .updateDomainState, payload: [:])
        dispatcher.dispatch(malformed)

        XCTAssertEqual(errors.count, 1)
        XCTAssertEqual(store.current, DomainState())  // untouched
    }

    func testBindDomainStateReleasesStoreWeakly() throws {
        // If the store is deallocated, the dispatcher must not crash and
        // must simply no-op.
        let dispatcher = CompanionIntentDispatcher()
        var store: LiveDomainStateStore? = LiveDomainStateStore()
        dispatcher.bindDomainState(to: store!)
        store = nil

        let raw = #"""
        {"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["x"]}}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)  // must not crash
    }

    // ------------------------------------------------------------------
    // bindBubbleQueue
    // ------------------------------------------------------------------

    func testBindBubbleQueueShowIntentEnqueuesSpec() throws {
        let dispatcher = CompanionIntentDispatcher()
        var now = 0
        let queue = LiveBubbleQueue(maxActive: 5) { now }
        dispatcher.bindBubbleQueue(to: queue)

        let raw = #"""
        {
          "kind": "show_pet_bubble",
          "payload": {
            "bubble": {
              "id": "approval-a1",
              "kind": "approval_hint",
              "text": "Allow?",
              "ttl_ms": null,
              "priority": "P1",
              "actions": []
            },
            "approval_id": "a1"
          }
        }
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(queue.count, 1)
        let spec = queue.peek()
        XCTAssertEqual(spec?.id, "approval-a1")
        XCTAssertEqual(spec?.kind, .approvalHint)
        XCTAssertNil(spec?.ttlMs)
        XCTAssertEqual(spec?.priority, .p1)
    }

    func testBindBubbleQueueDismissIntentRemovesById() throws {
        let dispatcher = CompanionIntentDispatcher()
        let queue = LiveBubbleQueue(maxActive: 5) { 0 }
        dispatcher.bindBubbleQueue(to: queue)

        queue.enqueue(BubbleSpec(id: "approval-a1", ttlMs: nil))
        queue.enqueue(BubbleSpec(id: "approval-a2", ttlMs: nil))
        XCTAssertEqual(queue.count, 2)

        let raw = #"""
        {"kind":"dismiss_pet_bubble","payload":{"bubble_id":"approval-a1"}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(queue.count, 1)
        XCTAssertEqual(queue.peek()?.id, "approval-a2")
    }

    func testBindBubbleQueueShowMissingBubbleFieldReportsError() {
        let dispatcher = CompanionIntentDispatcher()
        let queue = LiveBubbleQueue(maxActive: 5) { 0 }
        var errors: [Error] = []
        dispatcher.bindBubbleQueue(to: queue) { errors.append($0) }

        let malformed = CompanionIntent(kind: .showPetBubble, payload: [:])
        dispatcher.dispatch(malformed)

        XCTAssertEqual(errors.count, 1)
        XCTAssertEqual(queue.count, 0)
    }

    func testBindBubbleQueueDismissMissingBubbleIdReportsError() {
        let dispatcher = CompanionIntentDispatcher()
        let queue = LiveBubbleQueue(maxActive: 5) { 0 }
        var errors: [Error] = []
        dispatcher.bindBubbleQueue(to: queue) { errors.append($0) }

        let malformed = CompanionIntent(kind: .dismissPetBubble, payload: [:])
        dispatcher.dispatch(malformed)

        XCTAssertEqual(errors.count, 1)
    }

    func testBindBubbleQueueReleasesQueueWeakly() throws {
        let dispatcher = CompanionIntentDispatcher()
        var queue: LiveBubbleQueue? = LiveBubbleQueue(maxActive: 5) { 0 }
        dispatcher.bindBubbleQueue(to: queue!)
        queue = nil

        let raw = #"""
        {"kind":"show_pet_bubble","payload":{"bubble":{"id":"x","ttl_ms":null}}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)  // must not crash
    }

    // ------------------------------------------------------------------
    // bindIslandSurface
    // ------------------------------------------------------------------

    func testBindIslandSurfacePresent() throws {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveIslandSurfaceStore { 1_000 }
        dispatcher.bindIslandSurface(to: store)

        let raw = #"""
        {
          "kind": "present_island",
          "payload": {
            "surface": "notification_card",
            "session_id": "abc",
            "priority": "P1"
          }
        }
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(store.surface.kind, .notificationCard)
        XCTAssertEqual(store.surface.sessionId, "abc")
        XCTAssertEqual(store.priority, .p1)
    }

    func testBindIslandSurfacePresentDefaultsToP2() throws {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveIslandSurfaceStore { 0 }
        dispatcher.bindIslandSurface(to: store)

        let raw = #"""
        {"kind":"present_island","payload":{"surface":"session_list"}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(store.surface.kind, .sessionList)
        XCTAssertEqual(store.priority, .p2)
    }

    func testBindIslandSurfacePresentRejectsUnknownSurface() {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveIslandSurfaceStore { 0 }
        var errors: [Error] = []
        dispatcher.bindIslandSurface(to: store) { errors.append($0) }

        dispatcher.dispatch(CompanionIntent(
            kind: .presentIsland,
            payload: ["surface": .string("teleport")]
        ))
        XCTAssertEqual(errors.count, 1)
        XCTAssertEqual(store.surface.kind, .empty)
    }

    func testBindIslandSurfaceUpdateRequiresActivityId() {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveIslandSurfaceStore { 0 }
        var errors: [Error] = []
        dispatcher.bindIslandSurface(to: store) { errors.append($0) }

        dispatcher.dispatch(CompanionIntent(kind: .updateIsland, payload: [:]))
        XCTAssertEqual(errors.count, 1)
    }

    func testBindIslandSurfaceDismissNoPayloadClearsSurface() throws {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveIslandSurfaceStore { 0 }
        dispatcher.bindIslandSurface(to: store)

        store.present(kind: .notificationCard, sessionId: "s1", priority: .p2)

        let raw = #"""
        {"kind":"dismiss_island","payload":{}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(store.surface.kind, .empty)
    }

    func testBindIslandSurfaceDismissWithMatchingSessionId() throws {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveIslandSurfaceStore { 0 }
        dispatcher.bindIslandSurface(to: store)

        store.present(kind: .sessionList, sessionId: "s1", priority: .p2)

        let raw = #"""
        {"kind":"dismiss_island","payload":{"id":"s1"}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(store.surface.kind, .empty)
    }

    func testBindIslandSurfaceDismissNonMatchingIdIsNoop() throws {
        let dispatcher = CompanionIntentDispatcher()
        let store = LiveIslandSurfaceStore { 0 }
        dispatcher.bindIslandSurface(to: store)

        store.present(kind: .notificationCard, sessionId: "s1", priority: .p2)

        let raw = #"""
        {"kind":"dismiss_island","payload":{"id":"different"}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)

        XCTAssertEqual(store.surface.kind, .notificationCard)  // unchanged
    }

    func testBindIslandSurfaceReleasesStoreWeakly() throws {
        let dispatcher = CompanionIntentDispatcher()
        var store: LiveIslandSurfaceStore? = LiveIslandSurfaceStore { 0 }
        dispatcher.bindIslandSurface(to: store!)
        store = nil

        let raw = #"""
        {"kind":"present_island","payload":{"surface":"notification_card"}}
        """#.data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
        dispatcher.dispatch(intent)  // must not crash
    }

    // ------------------------------------------------------------------
    // bind(bridge:)
    // ------------------------------------------------------------------

    func testDecodeCompanionIntentFromEnvelope() throws {
        // The envelope's payload *is* a serialized CompanionIntent.
        let env = BridgeEnvelope.of(
            .intent,
            payload: [
                "kind": .string("show_pet_bubble"),
                "payload": .object([
                    "bubble": .object([
                        "id": .string("b1"),
                        "text": .string("hi"),
                    ])
                ]),
            ]
        )
        let intent = try CompanionIntentDispatcher.decodeCompanionIntent(from: env)
        XCTAssertEqual(intent.kind, .showPetBubble)
        guard case .object(let bubble) = intent.payload["bubble"] ?? .null else {
            return XCTFail("bubble missing")
        }
        XCTAssertEqual(bubble["id"], .string("b1"))
    }

    func testBindBridgeDispatchesIncomingIntentEnvelope() {
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let bridge = BridgeClient(callbackQueue: .main)
        XCTAssertNoThrow(try bridge.start(preConnectedFd: clientFd))
        defer { bridge.stop(); close(agentFd) }

        let dispatcher = CompanionIntentDispatcher()
        dispatcher.bind(bridge: bridge)

        let exp = expectation(description: "handler invoked")
        var capturedKind: IntentKind?
        dispatcher.register(kind: .updateDomainState) { intent in
            capturedKind = intent.kind
            exp.fulfill()
        }

        // Inject a Python-shaped intent envelope from the peer end.
        let wire = #"""
        {"spec_version":1,"type":"intent","trace_id":"t1","payload":{"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"]}}}}
        """#
        var bytes = Data(wire.utf8)
        bytes.append(EnvelopeFraming.separator)
        bytes.withUnsafeBytes { raw in
            _ = Darwin.write(agentFd, raw.baseAddress, raw.count)
        }

        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(capturedKind, .updateDomainState)
    }

    func testBindBridgeIgnoresNonIntentEnvelopes() {
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let bridge = BridgeClient(callbackQueue: .main)
        XCTAssertNoThrow(try bridge.start(preConnectedFd: clientFd))
        defer { bridge.stop(); close(agentFd) }

        let dispatcher = CompanionIntentDispatcher()
        var dispatchCount = 0
        dispatcher.bind(bridge: bridge)
        dispatcher.register(kind: .showPetBubble) { _ in dispatchCount += 1 }

        // Send a snapshot envelope — not an intent.
        let snapshot = BridgeEnvelope.of(
            .stateSnapshot,
            payload: ["domain_state": .object([:])],
            traceId: "snap-1"
        )
        let encoded = try! EnvelopeFraming.encode(snapshot)
        encoded.withUnsafeBytes { raw in
            _ = Darwin.write(agentFd, raw.baseAddress, raw.count)
        }

        // Also send a real intent so we can observe the ignore.
        let intentRaw = #"""
        {"spec_version":1,"type":"intent","trace_id":"t2","payload":{"kind":"show_pet_bubble","payload":{"bubble":{"id":"b","ttl_ms":null}}}}

        """#
        var bytes = Data(intentRaw.utf8)
        bytes.withUnsafeBytes { raw in
            _ = Darwin.write(agentFd, raw.baseAddress, raw.count)
        }

        let exp = expectation(description: "intent handler fires once")
        let poll = DispatchQueue(label: "poll")
        poll.asyncAfter(deadline: .now() + 0.2) { exp.fulfill() }
        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(dispatchCount, 1, "only the intent envelope should dispatch")
    }

    func testBindBridgeForwardsMalformedIntentToErrorHook() {
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let bridge = BridgeClient(callbackQueue: .main)
        XCTAssertNoThrow(try bridge.start(preConnectedFd: clientFd))
        defer { bridge.stop(); close(agentFd) }

        let dispatcher = CompanionIntentDispatcher()
        let exp = expectation(description: "decode error")
        dispatcher.bind(bridge: bridge) { _ in exp.fulfill() }

        // Intent envelope whose payload.kind is missing (required field).
        let malformed = #"""
        {"spec_version":1,"type":"intent","trace_id":"t3","payload":{"payload":{}}}

        """#
        Data(malformed.utf8).withUnsafeBytes { raw in
            _ = Darwin.write(agentFd, raw.baseAddress, raw.count)
        }
        wait(for: [exp], timeout: 2.0)
    }
}
