#if canImport(Darwin)
import Darwin
#endif
import XCTest
@testable import DeskmateCore

final class EnvelopeSenderTests: XCTestCase {
    private func drain(fd: Int32, timeout: TimeInterval = 2.0) -> Data {
        var out = Data()
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            var buf = [UInt8](repeating: 0, count: 2048)
            let n: Int = buf.withUnsafeMutableBufferPointer { ptr in
                #if canImport(Darwin)
                return Darwin.read(fd, ptr.baseAddress, ptr.count)
                #else
                return read(fd, ptr.baseAddress, ptr.count)
                #endif
            }
            if n > 0 {
                out.append(buf, count: n)
                // If we already have an NL, we likely have a full envelope.
                if out.contains(EnvelopeFraming.separator) { return out }
            } else if n == 0 {
                return out
            } else {
                if errno == EINTR { continue }
                Thread.sleep(forTimeInterval: 0.01)
            }
        }
        return out
    }

    private func makePair() -> (BridgeClient, agentFd: Int32) {
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let client = BridgeClient(callbackQueue: .main)
        try! client.start(preConnectedFd: clientFd)
        return (client, agentFd)
    }

    // MARK: - Tests

    func testSendActionProducesInteractionEnvelope() throws {
        let (client, agentFd) = makePair()
        defer { client.stop(); close(agentFd) }

        let action = InteractionAction(
            source: .pet,
            target: .bubble,
            kind: .permissionResolve,
            payload: [
                "approval_id": .string("ap-1"),
                "allow": .bool(true),
            ]
        )
        try client.send(action: action, traceId: "t-action")

        let bytes = drain(fd: agentFd)
        XCTAssertEqual(bytes.last, EnvelopeFraming.separator)
        var framing = EnvelopeFraming()
        let env = framing.feedEnvelopes(bytes).first
        XCTAssertEqual(env?.type, .interaction)
        XCTAssertEqual(env?.traceId, "t-action")
        XCTAssertEqual(env?.payload["kind"], .string("permission.resolve"))
        XCTAssertEqual(env?.payload["source"], .string("pet"))
        XCTAssertEqual(env?.payload["target"], .string("bubble"))
        if case .object(let inner) = env?.payload["payload"] ?? .null {
            XCTAssertEqual(inner["approval_id"], .string("ap-1"))
            XCTAssertEqual(inner["allow"], .bool(true))
        } else {
            XCTFail("inner payload missing / wrong shape")
        }
    }

    func testSendUserMessageProducesTextPayload() throws {
        let (client, agentFd) = makePair()
        defer { client.stop(); close(agentFd) }

        try client.sendUserMessage("hello", traceId: "t-msg")

        let bytes = drain(fd: agentFd)
        var framing = EnvelopeFraming()
        let env = framing.feedEnvelopes(bytes).first
        XCTAssertEqual(env?.type, .userMessage)
        XCTAssertEqual(env?.traceId, "t-msg")
        XCTAssertEqual(env?.payload["text"], .string("hello"))
    }

    func testSendUserClickPetProducesEmptyPayload() throws {
        let (client, agentFd) = makePair()
        defer { client.stop(); close(agentFd) }

        try client.sendUserClickPet(traceId: "t-click")

        let bytes = drain(fd: agentFd)
        var framing = EnvelopeFraming()
        let env = framing.feedEnvelopes(bytes).first
        XCTAssertEqual(env?.type, .userClickPet)
        XCTAssertEqual(env?.traceId, "t-click")
        XCTAssertEqual(env?.payload.count, 0)
    }

    func testSendPerceptionSnakeCaseKeys() throws {
        let (client, agentFd) = makePair()
        defer { client.stop(); close(agentFd) }

        let snap = PerceptionSnapshot(
            userState: "active",
            focus: .focused,
            app: "com.apple.Terminal",
            title: "bash",
            idleMs: 1_500
        )
        try client.sendPerception(snap, traceId: "t-perc")

        let bytes = drain(fd: agentFd)
        var framing = EnvelopeFraming()
        let env = framing.feedEnvelopes(bytes).first
        XCTAssertEqual(env?.type, .perception)
        XCTAssertEqual(env?.traceId, "t-perc")
        XCTAssertEqual(env?.payload["user_state"], .string("active"))
        XCTAssertEqual(env?.payload["focus"], .string("focused"))
        XCTAssertEqual(env?.payload["app"], .string("com.apple.Terminal"))
        XCTAssertEqual(env?.payload["title"], .string("bash"))
        XCTAssertEqual(env?.payload["idle_ms"], .int(1_500))
    }

    func testReconnectingClientAdoptsEnvelopeSender() throws {
        // The helpers must work the same through the reconnecting
        // wrapper — that's why they live on the protocol.
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let inner = BridgeClient(callbackQueue: .main)
        try inner.start(preConnectedFd: clientFd)
        let rc = ReconnectingBridgeClient(factory: { inner })
        defer { inner.stop(); close(agentFd) }

        // Stamp the rc into .connected by driving a manual connect path
        // using the inner client directly — we bypass the factory loop
        // by calling start() and waiting.
        rc.start()
        let exp = expectation(description: "connected")
        rc.onStateChange { s in if s == .connected { exp.fulfill() } }
        wait(for: [exp], timeout: 2.0)

        try rc.sendUserMessage("hi", traceId: "t-rc")
        let bytes = drain(fd: agentFd)
        var framing = EnvelopeFraming()
        let env = framing.feedEnvelopes(bytes).first
        XCTAssertEqual(env?.type, .userMessage)
        XCTAssertEqual(env?.traceId, "t-rc")

        rc.stop()
    }
}
