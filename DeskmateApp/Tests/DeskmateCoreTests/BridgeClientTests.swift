#if canImport(Darwin)
import Darwin
#endif
import XCTest
@testable import DeskmateCore

final class BridgeClientTests: XCTestCase {
    // MARK: - Helpers

    private func writeAll(fd: Int32, data: Data) {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var offset = 0
            while offset < data.count {
                #if canImport(Darwin)
                let n = Darwin.write(fd, base.advanced(by: offset), data.count - offset)
                #else
                let n = write(fd, base.advanced(by: offset), data.count - offset)
                #endif
                if n < 0 {
                    if errno == EINTR { continue }
                    return
                }
                offset += n
            }
        }
    }

    private func makeClient() -> (BridgeClient, agentFd: Int32) {
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let client = BridgeClient(callbackQueue: .main)
        try! client.start(preConnectedFd: clientFd)
        return (client, agentFd)
    }

    private func pingEnvelope(_ trace: String) -> Data {
        try! EnvelopeFraming.encode(BridgeEnvelope.of(.ping, traceId: trace))
    }

    // MARK: - Receive

    func testReceivesSingleEnvelope() {
        let (client, agentFd) = makeClient()
        defer { client.stop(); close(agentFd) }

        let exp = expectation(description: "got envelope")
        var received: BridgeEnvelope?
        client.onEnvelope { env in
            received = env
            exp.fulfill()
        }

        writeAll(fd: agentFd, data: pingEnvelope("t1"))
        wait(for: [exp], timeout: 2.0)

        XCTAssertEqual(received?.traceId, "t1")
        XCTAssertEqual(received?.type, .ping)
    }

    func testReceivesMultipleEnvelopesInOrder() {
        let (client, agentFd) = makeClient()
        defer { client.stop(); close(agentFd) }

        let exp = expectation(description: "three envelopes")
        exp.expectedFulfillmentCount = 3
        var traces: [String] = []
        client.onEnvelope { env in
            traces.append(env.traceId)
            exp.fulfill()
        }

        var chunk = Data()
        chunk.append(pingEnvelope("a"))
        chunk.append(pingEnvelope("b"))
        chunk.append(pingEnvelope("c"))
        writeAll(fd: agentFd, data: chunk)

        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(traces, ["a", "b", "c"])
    }

    func testReassemblesPartialWritesAcrossCallbacks() {
        let (client, agentFd) = makeClient()
        defer { client.stop(); close(agentFd) }

        let exp = expectation(description: "envelope eventually")
        var received: BridgeEnvelope?
        client.onEnvelope { env in
            received = env
            exp.fulfill()
        }

        let full = pingEnvelope("split")
        let half = full.count / 2
        writeAll(fd: agentFd, data: full.prefix(half))
        // Tiny sleep to let the half chunk arrive + not complete a line.
        Thread.sleep(forTimeInterval: 0.05)
        writeAll(fd: agentFd, data: full.suffix(from: half))

        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(received?.traceId, "split")
    }

    // MARK: - Send

    func testSendEncodesAndWritesToPeer() {
        let (client, agentFd) = makeClient()
        defer { client.stop(); close(agentFd) }

        let env = BridgeEnvelope.of(
            .userMessage,
            payload: ["text": .string("hi")],
            traceId: "send-trace"
        )
        XCTAssertNoThrow(try client.send(env))

        // Read from the peer end and assert the framed envelope arrives.
        var buffer = [UInt8](repeating: 0, count: 1024)
        let n: Int = buffer.withUnsafeMutableBufferPointer { ptr in
            #if canImport(Darwin)
            return Darwin.read(agentFd, ptr.baseAddress, ptr.count)
            #else
            return read(agentFd, ptr.baseAddress, ptr.count)
            #endif
        }
        XCTAssertGreaterThan(n, 0)
        let chunk = Data(bytes: buffer, count: n)
        XCTAssertEqual(chunk.last, EnvelopeFraming.separator)

        var framing = EnvelopeFraming()
        let envelopes = framing.feedEnvelopes(chunk)
        XCTAssertEqual(envelopes.first?.traceId, "send-trace")
        XCTAssertEqual(envelopes.first?.payload["text"], .string("hi"))
    }

    func testSendBeforeStartThrows() {
        let client = BridgeClient()
        let env = BridgeEnvelope.of(.ping)
        XCTAssertThrowsError(try client.send(env)) { error in
            guard case BridgeClient.Error.notConnected = error else {
                return XCTFail("expected .notConnected, got \(error)")
            }
        }
    }

    // MARK: - State machine

    func testStateIsConnectedAfterStart() {
        let (client, agentFd) = makeClient()
        defer { client.stop(); close(agentFd) }
        XCTAssertEqual(client.state, .connected)
    }

    func testPeerCloseTransitionsToDisconnected() {
        let (client, agentFd) = makeClient()
        defer { client.stop() }

        let exp = expectation(description: "state change")
        client.onStateChange { state in
            if state == .disconnected { exp.fulfill() }
        }
        close(agentFd)

        wait(for: [exp], timeout: 2.0)
        XCTAssertEqual(client.state, .disconnected)
    }

    func testStopIsIdempotent() {
        let (client, agentFd) = makeClient()
        defer { close(agentFd) }
        client.stop()
        client.stop()  // must not crash
        XCTAssertEqual(client.state, .disconnected)
    }

    func testDoubleStartThrows() {
        let (client, agentFd) = makeClient()
        defer { client.stop(); close(agentFd) }
        let (_, extraFd) = BridgeClient.makeTestSocketPair()
        defer { close(extraFd) }
        XCTAssertThrowsError(try client.start(preConnectedFd: extraFd)) { error in
            guard case BridgeClient.Error.alreadyStarted = error else {
                return XCTFail("expected .alreadyStarted, got \(error)")
            }
        }
    }

    // MARK: - Decode errors

    func testMalformedLineTriggersDecodeErrorHandler() {
        let (client, agentFd) = makeClient()
        defer { client.stop(); close(agentFd) }

        let exp = expectation(description: "decode error reported")
        client.onDecodeError { _ in exp.fulfill() }

        writeAll(fd: agentFd, data: Data("{not-json}\n".utf8))
        wait(for: [exp], timeout: 2.0)
    }
}
