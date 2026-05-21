#if canImport(Darwin)
import Darwin
#endif
import XCTest
@testable import DeskmateCore

final class DeskmateShellTests: XCTestCase {
    // MARK: - Harness

    final class Harness {
        var peerFds: [Int32] = []
        var callCount = 0

        func factory() throws -> BridgeClient {
            callCount += 1
            let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
            let client = BridgeClient(callbackQueue: .main)
            try client.start(preConnectedFd: clientFd)
            peerFds.append(agentFd)
            return client
        }

        func pushFromAgent(_ bytes: Data, index: Int = 0) {
            bytes.withUnsafeBytes { raw in
                _ = Darwin.write(peerFds[index], raw.baseAddress, raw.count)
            }
        }

        deinit {
            for fd in peerFds where fd > 0 { close(fd) }
        }
    }

    private func makeShell(harness: Harness) -> DeskmateShell {
        var config = DeskmateShell.Configuration(
            bridgeBackoff: .init(
                initialBackoff: 0.01, maxBackoff: 0.05,
                multiplier: 2.0, jitterFraction: 0.0
            )
        )
        config.clientFactory = harness.factory
        return DeskmateShell(configuration: config, callbackQueue: .main)
    }

    private func wait(until condition: @escaping @autoclosure () -> Bool,
                      timeout: TimeInterval = 2.0,
                      description: String = "condition") {
        let exp = expectation(description: description)
        let queue = DispatchQueue(label: "shell-wait")
        func poll() {
            if condition() { exp.fulfill(); return }
            queue.asyncAfter(deadline: .now() + 0.01, execute: poll)
        }
        poll()
        wait(for: [exp], timeout: timeout)
    }

    // MARK: - Tests

    func testShellExposesAllLiveStoresWiredToDispatcher() {
        let harness = Harness()
        let shell = makeShell(harness: harness)
        defer { shell.stop() }

        // Before start() the bridge is .stopped so sends must fail.
        XCTAssertThrowsError(try shell.sendUserMessage("x"))

        XCTAssertTrue(shell.dispatcher.hasHandler(for: .updateDomainState))
        XCTAssertTrue(shell.dispatcher.hasHandler(for: .showPetBubble))
        XCTAssertTrue(shell.dispatcher.hasHandler(for: .dismissPetBubble))
        XCTAssertTrue(shell.dispatcher.hasHandler(for: .presentIsland))
    }

    func testStartBringsBridgeOnline() {
        let harness = Harness()
        let shell = makeShell(harness: harness)
        defer { shell.stop() }

        shell.start()
        wait(until: shell.bridge.state == .connected)
        XCTAssertEqual(harness.callCount, 1)
    }

    func testIncomingIntentEnvelopeDrivesDomainStore() {
        let harness = Harness()
        let shell = makeShell(harness: harness)
        defer { shell.stop() }

        shell.start()
        wait(until: shell.bridge.state == .connected)

        let wire = #"""
        {"spec_version":1,"type":"intent","trace_id":"t1","payload":{"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"],"agent_mood":"alert"}}}}

        """#
        harness.pushFromAgent(Data(wire.utf8))

        wait(until: shell.domainState.current.pendingApprovals == ["ap-1"])
        XCTAssertEqual(shell.domainState.current.agentMood, .alert)
    }

    func testIncomingShowPetBubbleEnqueuesOnBubbleQueue() {
        let harness = Harness()
        let shell = makeShell(harness: harness)
        defer { shell.stop() }

        shell.start()
        wait(until: shell.bridge.state == .connected)

        let wire = #"""
        {"spec_version":1,"type":"intent","trace_id":"t2","payload":{"kind":"show_pet_bubble","payload":{"bubble":{"id":"approval-a1","kind":"approval_hint","text":"Allow?","ttl_ms":null,"priority":"P1","actions":[]}}}}

        """#
        harness.pushFromAgent(Data(wire.utf8))

        wait(until: shell.bubbleQueue.count == 1)
        XCTAssertEqual(shell.bubbleQueue.peek()?.id, "approval-a1")
    }

    func testIncomingPresentIslandDrivesIslandSurface() {
        let harness = Harness()
        let shell = makeShell(harness: harness)
        defer { shell.stop() }

        shell.start()
        wait(until: shell.bridge.state == .connected)

        let wire = #"""
        {"spec_version":1,"type":"intent","trace_id":"t3","payload":{"kind":"present_island","payload":{"surface":"session_list","priority":"P2"}}}

        """#
        harness.pushFromAgent(Data(wire.utf8))

        wait(until: shell.islandSurface.surface.kind == .sessionList)
    }

    func testSendActionLandsOnPeer() {
        let harness = Harness()
        let shell = makeShell(harness: harness)
        defer { shell.stop() }

        shell.start()
        wait(until: shell.bridge.state == .connected)

        let action = InteractionAction(
            source: .pet, target: .bubble, kind: .permissionResolve,
            payload: ["approval_id": .string("ap-1"), "allow": .bool(true)]
        )
        XCTAssertNoThrow(try shell.send(action: action, traceId: "shell-trace"))

        // Drain from peer end.
        var out = Data()
        let deadline = Date().addingTimeInterval(2.0)
        while Date() < deadline {
            var buf = [UInt8](repeating: 0, count: 1024)
            let n: Int = buf.withUnsafeMutableBufferPointer { ptr in
                Darwin.read(harness.peerFds[0], ptr.baseAddress, ptr.count)
            }
            if n > 0 {
                out.append(buf, count: n)
                if out.contains(EnvelopeFraming.separator) { break }
            } else if n == 0 {
                break
            } else {
                if errno == EINTR { continue }
                Thread.sleep(forTimeInterval: 0.01)
            }
        }
        var framing = EnvelopeFraming()
        let env = framing.feedEnvelopes(out).first
        XCTAssertEqual(env?.type, .interaction)
        XCTAssertEqual(env?.traceId, "shell-trace")
        XCTAssertEqual(env?.payload["kind"], .string("permission.resolve"))
    }

    func testStopIsIdempotent() {
        let harness = Harness()
        let shell = makeShell(harness: harness)
        shell.start()
        wait(until: shell.bridge.state == .connected)
        shell.stop()
        shell.stop()  // must not crash
        XCTAssertEqual(shell.bridge.state, .stopped)
    }

    func testProductionFactoryWithoutSocketPathThrows() {
        // Exercise the default factory path (no clientFactory override).
        let shell = DeskmateShell(
            configuration: .init(),
            callbackQueue: .main
        )
        defer { shell.stop() }

        shell.start()
        // With no socket path, the factory throws; the reconnecting
        // bridge enters waitingForRetry and never progresses.
        wait(until: {
            if case .waitingForRetry = shell.bridge.state { return true }
            return false
        }())
    }
}
