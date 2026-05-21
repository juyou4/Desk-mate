import XCTest
@testable import DeskmateCore

final class PerceptionSamplerTests: XCTestCase {
    // MARK: - Fakes

    final class RecordingSender: EnvelopeSender {
        var envelopes: [BridgeEnvelope] = []
        func send(_ envelope: BridgeEnvelope) throws {
            envelopes.append(envelope)
        }
        var perceptions: [PerceptionSnapshot] {
            envelopes.compactMap { env in
                guard env.type == .perception else { return nil }
                guard let data = try? JSONEncoder().encode(env.payload)
                else { return nil }
                return try? JSONDecoder().decode(
                    PerceptionSnapshot.self, from: data
                )
            }
        }
    }

    final class Scenario {
        var idle: TimeInterval = 0
        var app: PerceptionSampler.FrontmostApp = .init(
            bundleId: "com.apple.Terminal", title: "bash"
        )
        var nowMs: Int = 0
    }

    private func buildSampler(
        scenario: Scenario,
        sender: RecordingSender,
        heartbeatInterval: TimeInterval = 30
    ) -> PerceptionSampler {
        let config = PerceptionSampler.Configuration(
            tickInterval: 60,  // effectively disabled; tests drive via .tick()
            heartbeatInterval: heartbeatInterval,
            idleSecondsForIdleState: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            focusInferrer: PerceptionSampler.defaultFocusInferrer,
            clock: { scenario.nowMs }
        )
        return PerceptionSampler(sender: sender, configuration: config)
    }

    // MARK: - Tests

    func testInitialTickSendsFirstSnapshot() {
        let sender = RecordingSender()
        let scenario = Scenario()
        let s = buildSampler(scenario: scenario, sender: sender)

        s.tick()
        XCTAssertEqual(sender.perceptions.count, 1)
        XCTAssertEqual(sender.perceptions[0].app, "com.apple.Terminal")
        XCTAssertEqual(sender.perceptions[0].focus, .focused)
        XCTAssertEqual(sender.perceptions[0].userState, "active")
    }

    func testSecondTickWithSameStateIsDeduped() {
        let sender = RecordingSender()
        let scenario = Scenario()
        let s = buildSampler(scenario: scenario, sender: sender)

        s.tick()
        scenario.nowMs += 500  // below heartbeat
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 1)
    }

    func testAppChangeReSendsSnapshot() {
        let sender = RecordingSender()
        let scenario = Scenario()
        let s = buildSampler(scenario: scenario, sender: sender)

        s.tick()
        scenario.app = .init(bundleId: "com.apple.Safari", title: "apple.com")
        scenario.nowMs += 1_000
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 2)
        XCTAssertEqual(sender.perceptions.last?.app, "com.apple.Safari")
    }

    func testFocusTransitionFocusedToCasualReSends() {
        let sender = RecordingSender()
        let scenario = Scenario()  // idle=0 → focused
        let s = buildSampler(scenario: scenario, sender: sender)

        s.tick()
        scenario.idle = 11  // focus becomes .casual (>=10s)
        scenario.nowMs += 1_000
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 2)
        XCTAssertEqual(sender.perceptions.last?.focus, .casual)
    }

    func testUserStateTransitionReSends() {
        let sender = RecordingSender()
        let scenario = Scenario()
        let s = buildSampler(scenario: scenario, sender: sender)

        s.tick()
        scenario.idle = 31  // crosses 30s threshold → "idle"
        scenario.nowMs += 1_000
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 2)
        XCTAssertEqual(sender.perceptions.last?.userState, "idle")
    }

    func testHeartbeatSendsEvenWithoutChange() {
        let sender = RecordingSender()
        let scenario = Scenario()
        let s = buildSampler(
            scenario: scenario, sender: sender, heartbeatInterval: 5
        )

        s.tick()
        scenario.nowMs += 4_000  // below heartbeat
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 1)

        scenario.nowMs += 2_000  // cumulatively above heartbeat
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 2)
    }

    func testSnapshotWireKeysAreSnakeCase() {
        let sender = RecordingSender()
        let scenario = Scenario()
        let s = buildSampler(scenario: scenario, sender: sender)
        s.tick()

        let payload = sender.envelopes[0].payload
        XCTAssertNotNil(payload["user_state"])
        XCTAssertNotNil(payload["idle_ms"])
        XCTAssertNil(payload["userState"], "must not emit camelCase")
    }

    func testSendThrowingDoesNotAdvanceDedupeCursor() {
        // When the transport hiccups, the next tick should retry
        // delivering the same state instead of silently swallowing.
        final class FlakySender: EnvelopeSender {
            var calls = 0
            var succeedOn: Int
            init(succeedOn: Int) { self.succeedOn = succeedOn }
            func send(_ envelope: BridgeEnvelope) throws {
                calls += 1
                if calls < succeedOn {
                    throw BridgeClient.Error.notConnected
                }
            }
        }
        let sender = FlakySender(succeedOn: 2)
        let scenario = Scenario()
        let config = PerceptionSampler.Configuration(
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app }
        )
        let s = PerceptionSampler(sender: sender, configuration: config)
        s.tick()  // throws
        XCTAssertEqual(s.sentCount, 0)
        s.tick()  // retries delivering same snapshot → succeeds
        XCTAssertEqual(s.sentCount, 1)
    }

    func testPausedSamplerDoesNotPollOrSendUntilResumed() {
        let sender = RecordingSender()
        let scenario = Scenario()
        let s = buildSampler(scenario: scenario, sender: sender)

        s.setPaused(true)
        XCTAssertTrue(s.isPaused)
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 0)

        s.setPaused(false)
        XCTAssertFalse(s.isPaused)
        s.tick()
        XCTAssertEqual(sender.perceptions.count, 1)
    }

    func testDefaultFocusInferrerBands() {
        XCTAssertEqual(
            PerceptionSampler.defaultFocusInferrer(idleSeconds: 0), .focused
        )
        XCTAssertEqual(
            PerceptionSampler.defaultFocusInferrer(idleSeconds: 30), .casual
        )
        XCTAssertEqual(
            PerceptionSampler.defaultFocusInferrer(idleSeconds: 300), .idleBack
        )
    }

    func testCachedFrontmostAppProviderRefreshesAfterTtl() {
        var now: TimeInterval = 0
        var calls = 0
        let cached = CachedFrontmostAppProvider(
            ttl: 1.0,
            clock: { now },
            provider: {
                calls += 1
                return .init(bundleId: "app.\(calls)", title: "App \(calls)")
            }
        )

        XCTAssertEqual(cached().bundleId, "app.1")
        now = 0.5
        XCTAssertEqual(cached().bundleId, "app.1")
        XCTAssertEqual(calls, 1)

        now = 1.1
        XCTAssertEqual(cached().bundleId, "app.2")
        XCTAssertEqual(calls, 2)

        cached.invalidate()
        XCTAssertEqual(cached().bundleId, "app.3")
    }

    func testDefaultSocketPathEndsWithIpcSock() {
        let path = DefaultSocketPath.current()
        XCTAssertTrue(path.hasSuffix("Deskmate/ipc.sock"),
                      "unexpected path: \(path)")
    }
}
