import XCTest
@testable import DeskmateCore

final class BridgeEnvelopeTests: XCTestCase {
    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = []
        return e
    }()
    private let decoder = JSONDecoder()

    func testRoundTripPreservesTraceId() throws {
        let env = BridgeEnvelope.of(.userMessage, payload: ["text": .string("hi")])
        let data = try encoder.encode(env)
        let restored = try decoder.decode(BridgeEnvelope.self, from: data)
        XCTAssertEqual(restored.traceId, env.traceId)
        XCTAssertEqual(restored.type, .userMessage)
        XCTAssertEqual(restored.payload["text"], .string("hi"))
    }

    func testForwardCompatiblePayloadKeysSurvive() throws {
        let json = """
        {
          "spec_version": 1,
          "type": "user.message",
          "trace_id": "\(BridgeEnvelope.newTraceId())",
          "payload": { "text": "hi", "future_hint": [1, 2, 3] }
        }
        """.data(using: .utf8)!
        let env = try decoder.decode(BridgeEnvelope.self, from: json)
        XCTAssertEqual(env.payload["future_hint"], .array([.int(1), .int(2), .int(3)]))

        let roundTripped = try decoder.decode(
            BridgeEnvelope.self, from: try encoder.encode(env)
        )
        XCTAssertEqual(roundTripped.payload["future_hint"], env.payload["future_hint"])
    }

    func testTraceIdIsHex32Lowercase() {
        let id = BridgeEnvelope.newTraceId()
        XCTAssertEqual(id.count, 32)
        XCTAssertTrue(id.allSatisfy { "0123456789abcdef".contains($0) })
    }

    func testEnvelopeTypeRawValuesMatchProtocol() {
        // Guardrail against accidental enum renames. The raw values are the
        // wire format and MUST stay in sync with shared/protocol.md.
        let expected: [EnvelopeType: String] = [
            .perception: "perception",
            .userMessage: "user.message",
            .userClickPet: "user.click_pet",
            .interaction: "interaction",
            .intent: "intent",
            .ping: "ping",
            .pong: "pong",
            .stateSnapshotRequest: "state.snapshot.request",
            .stateSnapshot: "state.snapshot",
            .agentReady: "agent.ready",
            .agentPause: "agent.pause",
        ]
        for (kind, raw) in expected {
            XCTAssertEqual(kind.rawValue, raw)
        }
    }

    func testDefaultPayloadIsEmptyObject() throws {
        let env = BridgeEnvelope.of(.ping)
        let data = try encoder.encode(env)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let payload = json?["payload"] as? [String: Any]
        XCTAssertEqual(payload?.count, 0)
    }
}
