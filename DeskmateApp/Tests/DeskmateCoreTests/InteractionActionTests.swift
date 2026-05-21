import XCTest
@testable import DeskmateCore

final class InteractionActionTests: XCTestCase {
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    func testTypedActionEncodesAsDottedKind() throws {
        let act = InteractionAction(
            source: .island,
            target: .session,
            kind: .permissionResolve,
            payload: ["allow": .bool(true)]
        )
        let data = try encoder.encode(act)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(json?["kind"] as? String, "permission.resolve")
        XCTAssertEqual(json?["source"] as? String, "island")
    }

    func testRejectsUnknownKind() {
        let raw = """
        { "source": "island", "target": "session", "kind": "totally.invented", "payload": {} }
        """.data(using: .utf8)!
        XCTAssertThrowsError(try decoder.decode(InteractionAction.self, from: raw))
    }

    func testPreservesUnknownPayloadKeys() throws {
        let raw = """
        {
          "source": "pet", "target": "bubble", "kind": "pet.interact",
          "payload": { "gesture": "pat", "future_hint": 7 }
        }
        """.data(using: .utf8)!
        let act = try decoder.decode(InteractionAction.self, from: raw)
        XCTAssertEqual(act.payload["future_hint"], .int(7))
    }
}
