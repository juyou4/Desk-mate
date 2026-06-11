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

    func testDecodeExtrasAndDeriveApprovalDetailLine() throws {
        let raw = #"""
        {
          "approval_id": "ap-6",
          "prompt": "Allow edit?",
            "extras": {
              "tool_name": "Edit",
              "file_path": "/tmp/work/App.swift",
              "command": "python scripts/update.py",
              "approval_preview": "cmd: python scripts/update.py",
              "risk_level": "medium",
              "risk_summary": "Modifies a source file.",
              "ignored": 42
            }
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(ApprovalRow.self, from: raw)
        XCTAssertEqual(row.extras["tool_name"], "Edit")
        XCTAssertEqual(row.toolName, "Edit")
        XCTAssertEqual(row.toolAction, "Edit")
        XCTAssertEqual(row.filePath, "/tmp/work/App.swift")
        XCTAssertEqual(row.command, "python scripts/update.py")
        XCTAssertEqual(row.approvalPreview, "cmd: python scripts/update.py")
        XCTAssertEqual(row.riskLevel, "medium")
        XCTAssertEqual(row.riskSummary, "Modifies a source file.")
        XCTAssertEqual(row.detailLine, "cmd: python scripts/update.py")
        XCTAssertNil(row.extras["ignored"])
    }

    func testApprovalDetailLineUsesToolAndSuggestionReasons() {
        let tool = ApprovalRow(
            approvalId: "tool",
            extras: [
                "tool_action": "deskmate_open_app",
                "tool_target": "Calendar"
            ]
        )
        XCTAssertEqual(tool.detailLine, "tool: deskmate_open_app -> Calendar")

        let memory = ApprovalRow(
            approvalId: "memory",
            extras: [
                "kind": "memory_suggestion",
                "memory_reason": "Useful for coding help."
            ]
        )
        XCTAssertEqual(memory.approvalKind, "memory_suggestion")
        XCTAssertEqual(memory.detailLine, "Useful for coding help.")
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
            resolvedAtMs: 500,
            extras: ["tool_name": "Bash"]
        )
        let data = try encoder.encode(row)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNotNil(json?["approval_id"])
        XCTAssertNotNil(json?["created_at_ms"])
        XCTAssertNotNil(json?["expires_at_ms"])
        XCTAssertNotNil(json?["session_id"])
        XCTAssertEqual((json?["extras"] as? [String: String])?["tool_name"], "Bash")
        XCTAssertNil(json?["approvalId"])
    }
}
