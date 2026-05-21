#if canImport(Darwin)
import Darwin
#endif
import XCTest
@testable import DeskmateCore

final class ReconnectingBridgeClientTests: XCTestCase {
    // MARK: - Factory harness

    /// Orchestrates a sequence of connect attempts: each call either
    /// raises a supplied error or hands out a fresh BridgeClient bound
    /// to a new ``socketpair``. Agent-side fds (the peer end) are kept
    /// for the test to write traffic into.
    final class Harness {
        var scripted: [() throws -> BridgeClient] = []
        var callCount = 0
        var peerFds: [Int32] = []

        func pushSuccess() {
            scripted.append { [weak self] in
                let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
                let client = BridgeClient(callbackQueue: .main)
                try client.start(preConnectedFd: clientFd)
                self?.peerFds.append(agentFd)
                return client
            }
        }

        func pushFailure(_ err: Error) {
            scripted.append { throw err }
        }

        func factory() throws -> BridgeClient {
            defer { callCount += 1 }
            guard callCount < scripted.count else {
                throw BridgeClient.Error.socketFailed("harness exhausted")
            }
            return try scripted[callCount]()
        }

        deinit {
            for fd in peerFds { close(fd) }
        }
    }

    private func wait(until condition: @escaping @autoclosure () -> Bool,
                      timeout: TimeInterval = 2.0,
                      description: String = "condition") {
        let exp = expectation(description: description)
        let queue = DispatchQueue(label: "wait")
        func poll() {
            if condition() { exp.fulfill(); return }
            queue.asyncAfter(deadline: .now() + 0.01, execute: poll)
        }
        poll()
        wait(for: [exp], timeout: timeout)
    }

    private func config(initialMs: Int = 10, maxMs: Int = 80) -> ReconnectingBridgeClient.Configuration {
        .init(
            initialBackoff: TimeInterval(initialMs) / 1000,
            maxBackoff: TimeInterval(maxMs) / 1000,
            multiplier: 2.0,
            jitterFraction: 0.0
        )
    }

    // MARK: - Tests

    func testInitialConnectSuccessReportsConnected() {
        let harness = Harness()
        harness.pushSuccess()
        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        rc.start()
        defer { rc.stop() }
        wait(until: rc.state == .connected)
        XCTAssertEqual(harness.callCount, 1)
    }

    func testInitialFailureRetriesThenConnects() {
        let harness = Harness()
        harness.pushFailure(BridgeClient.Error.connectFailed("no agent"))
        harness.pushSuccess()
        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        rc.start()
        defer { rc.stop() }
        wait(until: rc.state == .connected, timeout: 2.0)
        XCTAssertEqual(harness.callCount, 2)
    }

    func testPeerCloseTriggersReconnect() {
        let harness = Harness()
        harness.pushSuccess()
        harness.pushSuccess()  // second client for reconnect
        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        rc.start()
        defer { rc.stop() }
        wait(until: rc.state == .connected)
        XCTAssertEqual(harness.callCount, 1)

        // Kill the first peer end — client should detect EOF and
        // transition to waitingForRetry → attemptConnect → connected.
        close(harness.peerFds[0])
        harness.peerFds[0] = -1  // avoid double close

        wait(until: harness.callCount == 2, timeout: 2.0)
        wait(until: rc.state == .connected, timeout: 2.0)
    }

    func testStopCancelsPendingRetry() {
        let harness = Harness()
        // Long series of failures — we never want connect to succeed.
        for _ in 0..<10 {
            harness.pushFailure(BridgeClient.Error.connectFailed("never"))
        }
        let rc = ReconnectingBridgeClient(
            factory: harness.factory,
            configuration: .init(
                initialBackoff: 0.5, maxBackoff: 0.5,
                multiplier: 1.0, jitterFraction: 0.0
            )
        )
        rc.start()
        // Wait for the first failure to schedule a retry.
        wait(until: {
            if case .waitingForRetry = rc.state { return true }
            return false
        }())
        rc.stop()
        // Give the dispatch queue time; the scheduled work item
        // should have been cancelled and no additional factory calls
        // should have happened.
        let callCountAtStop = harness.callCount
        Thread.sleep(forTimeInterval: 0.6)  // > initialBackoff
        XCTAssertEqual(harness.callCount, callCountAtStop,
                       "pending retry must not fire after stop()")
        XCTAssertEqual(rc.state, .stopped)
    }

    func testStopOnConnectedClosesClient() {
        let harness = Harness()
        harness.pushSuccess()
        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        rc.start()
        wait(until: rc.state == .connected)
        rc.stop()
        XCTAssertEqual(rc.state, .stopped)
    }

    func testStartIsIdempotent() {
        let harness = Harness()
        harness.pushSuccess()
        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        rc.start()
        rc.start()  // second call must not re-enter
        defer { rc.stop() }
        wait(until: rc.state == .connected)
        XCTAssertEqual(harness.callCount, 1)
    }

    func testStopIsIdempotent() {
        let harness = Harness()
        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        rc.stop()
        rc.stop()
        XCTAssertEqual(rc.state, .stopped)
    }

    func testSendBeforeConnectedThrows() {
        let harness = Harness()
        harness.pushFailure(BridgeClient.Error.connectFailed("nope"))
        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        rc.start()
        defer { rc.stop() }
        // Wait for the client to enter waitingForRetry so we know
        // the initial attempt failed.
        wait(until: {
            if case .waitingForRetry = rc.state { return true }
            return false
        }())
        XCTAssertThrowsError(try rc.send(BridgeEnvelope.of(.ping))) { err in
            guard case BridgeClient.Error.notConnected = err else {
                return XCTFail("expected .notConnected, got \(err)")
            }
        }
    }

    func testBackoffDoublesThenCaps() {
        // Three failures at 10ms × 2^n capped at 40ms: 10 → 20 → 40 → 40.
        let harness = Harness()
        for _ in 0..<4 { harness.pushFailure(BridgeClient.Error.connectFailed("x")) }
        var observed: [Int] = []
        let rc = ReconnectingBridgeClient(
            factory: harness.factory,
            configuration: .init(
                initialBackoff: 0.010, maxBackoff: 0.040,
                multiplier: 2.0, jitterFraction: 0.0
            )
        )
        rc.onStateChange { state in
            if case .waitingForRetry(_, let delay) = state {
                observed.append(delay)
            }
        }
        rc.start()
        defer { rc.stop() }
        wait(until: observed.count >= 4, timeout: 2.0)
        XCTAssertEqual(observed.prefix(4).map { $0 }, [10, 20, 40, 40])
    }

    func testEnvelopeHandlerSurvivesReconnect() {
        let harness = Harness()
        harness.pushSuccess()
        harness.pushSuccess()

        let rc = ReconnectingBridgeClient(
            factory: harness.factory, configuration: config()
        )
        var received: [String] = []
        let lock = NSLock()
        rc.onEnvelope { env in
            lock.lock(); received.append(env.traceId); lock.unlock()
        }
        rc.start()
        defer { rc.stop() }
        wait(until: rc.state == .connected)

        // Send via first peer.
        func writeAll(_ fd: Int32, _ data: Data) {
            data.withUnsafeBytes { raw in
                _ = Darwin.write(fd, raw.baseAddress, raw.count)
            }
        }
        let env1 = try! EnvelopeFraming.encode(
            BridgeEnvelope.of(.ping, traceId: "t1")
        )
        writeAll(harness.peerFds[0], env1)
        wait(until: lock.withLock { received.contains("t1") })

        // Peer dies; wait for reconnect; send via the new peer.
        close(harness.peerFds[0])
        harness.peerFds[0] = -1
        wait(until: harness.peerFds.count == 2 && harness.peerFds[1] != -1)
        wait(until: rc.state == .connected)
        let env2 = try! EnvelopeFraming.encode(
            BridgeEnvelope.of(.ping, traceId: "t2")
        )
        writeAll(harness.peerFds[1], env2)
        wait(until: lock.withLock { received.contains("t2") }, timeout: 2.0)
    }
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock(); defer { unlock() }; return body()
    }
}
