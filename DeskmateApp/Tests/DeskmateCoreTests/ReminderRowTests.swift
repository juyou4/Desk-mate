import XCTest
@testable import DeskmateCore

final class ReminderRowTests: XCTestCase {
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    func testDecodesWireFormatFromPython() throws {
        let raw = #"""
        {
          "reminder_id": "r1",
          "text": "stretch",
          "due_at_ms": 10000,
          "created_at_ms": 9000,
          "status": "pending",
          "priority": "P1",
          "session_id": null,
          "bubble_id": null
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ReminderRow.self, from: raw)
        XCTAssertEqual(row.reminderId, "r1")
        XCTAssertEqual(row.text, "stretch")
        XCTAssertEqual(row.status, .pending)
        XCTAssertEqual(row.priority, .p1)
        XCTAssertNil(row.sessionId)
    }

    func testDefaultsApplyForMissingFields() throws {
        let raw = #"""
        { "reminder_id": "r1" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ReminderRow.self, from: raw)
        XCTAssertEqual(row.text, "")
        XCTAssertEqual(row.status, .pending)
        XCTAssertEqual(row.priority, .p1)
    }

    func testUnknownStatusFallsBackGracefully() throws {
        let raw = #"""
        { "reminder_id": "r1", "status": "snoozed" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ReminderRow.self, from: raw)
        XCTAssertEqual(row.status, .unknown)
    }

    func testRoundTripKeepsSnakeCase() throws {
        let row = ReminderRow(
            reminderId: "r1",
            text: "task",
            dueAtMs: 100,
            createdAtMs: 50,
            status: .fired,
            priority: .p0,
            bubbleId: "bubble-x",
            firedAtMs: 110
        )
        let data = try encoder.encode(row)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNotNil(json?["reminder_id"])
        XCTAssertNotNil(json?["due_at_ms"])
        XCTAssertNotNil(json?["bubble_id"])
        XCTAssertNotNil(json?["fired_at_ms"])
        XCTAssertNil(json?["reminderId"])
    }
}
