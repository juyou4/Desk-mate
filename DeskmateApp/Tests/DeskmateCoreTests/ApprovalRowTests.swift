import XCTest
@testable import DeskmateCore

final class ApprovalRowTests: XCTestCase {
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    func testDecodesPendingApprovalWireFormat() throws {
        let raw = #"""
        {
          "approval_id": "ap-1",
          "prompt": "Allow clipboard read?",
          "status": "pending",
          "decision": "none",
          "priority": "P1",
          "created_at_ms": 1000,
          "expires_at_ms": null
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ApprovalRow.self, from: raw)
        XCTAssertEqual(row.approvalId, "ap-1")
        XCTAssertEqual(row.status, .pending)
        XCTAssertEqual(row.decision, .none)
        XCTAssertEqual(row.priority, .p1)
        XCTAssertEqual(row.createdAtMs, 1_000)
        XCTAssertNil(row.expiresAtMs)
    }

    func testDecodesResolvedApproval() throws {
        let raw = #"""
        {
          "approval_id": "ap-2",
          "prompt": "ok?",
          "status": "resolved",
          "decision": "allow",
          "resolved_at_ms": 5000
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ApprovalRow.self, from: raw)
        XCTAssertEqual(row.status, .resolved)
        XCTAssertEqual(row.decision, .allow)
        XCTAssertEqual(row.resolvedAtMs, 5_000)
    }

    func testDefaultsApplyForMissingFields() throws {
        let raw = #"""
        { "approval_id": "ap-3" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ApprovalRow.self, from: raw)
        XCTAssertEqual(row.prompt, "")
        XCTAssertEqual(row.status, .pending)
        XCTAssertEqual(row.decision, .none)
        XCTAssertEqual(row.priority, .p1)
    }

    func testUnknownStatusFallsBackGracefully() throws {
        let raw = #"""
        { "approval_id": "ap-4", "status": "snoozed" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ApprovalRow.self, from: raw)
        XCTAssertEqual(row.status, .unknown)
    }

    func testUnknownDecisionFallsBackGracefully() throws {
        let raw = #"""
        { "approval_id": "ap-5", "decision": "maybe" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ApprovalRow.self, from: raw)
        XCTAssertEqual(row.decision, .unknown)
    }

    func testRoundTripKeepsSnakeCase() throws {
        let row = ApprovalRow(
            approvalId: "ap-x",
            prompt: "run shell?",
            status: .resolved,
            decision: .deny,
            priority: .p0,
            sessionId: "s1",
            bubbleId: "b-x",
            createdAtMs: 100,
            expiresAtMs: 1000,
            resolvedAtMs: 500
        )
        let data = try encoder.encode(row)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNotNil(json?["approval_id"])
        XCTAssertNotNil(json?["created_at_ms"])
        XCTAssertNotNil(json?["expires_at_ms"])
        XCTAssertNotNil(json?["session_id"])
        XCTAssertNil(json?["approvalId"])
    }
}
