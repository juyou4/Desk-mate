import XCTest
@testable import DeskmateCore

final class SessionRowTests: XCTestCase {
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    func testDecodeWireFormatFromPython() throws {
        let raw = #"""
        {
          "session_id": "s1",
          "title": "Deploy",
          "summary": "CI waiting",
          "state": "active",
          "priority": "P1",
          "created_at_ms": 1000,
          "updated_at_ms": 2000,
          "closed_at_ms": null
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.sessionId, "s1")
        XCTAssertEqual(row.title, "Deploy")
        XCTAssertEqual(row.state, .active)
        XCTAssertEqual(row.priority, .p1)
        XCTAssertEqual(row.updatedAtMs, 2_000)
        XCTAssertNil(row.closedAtMs)
    }

    func testDefaultsForMissingFields() throws {
        let raw = #"""
        { "session_id": "s1" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.title, "")
        XCTAssertEqual(row.state, .active)
        XCTAssertEqual(row.priority, .p2)
    }

    func testUnknownStateFallsBackToUnknown() throws {
        let raw = #"""
        { "session_id": "s1", "state": "archived" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.state, .unknown)
    }

    func testDecodeHookJumpBackFields() throws {
        let raw = #"""
        {
          "session_id": "s1",
          "cwd": "/tmp/project",
          "jump_url": "codex://session/s1",
          "future_field": "kept"
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.cwd, "/tmp/project")
        XCTAssertEqual(row.jumpUrl, "codex://session/s1")
    }

    func testDecodeRuntimeSourceFields() throws {
        let raw = #"""
        {
          "session_id": "s1",
          "source": "claude_code",
          "kind": "cli_agent",
          "process_id": 4242
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.source, "claude_code")
        XCTAssertEqual(row.kind, "cli_agent")
        XCTAssertEqual(row.processId, 4242)
        XCTAssertEqual(row.sourceLabel, "Claude")
        XCTAssertTrue(row.canAttemptJump)
    }

    func testDecodeFineGrainedAgentPhase() throws {
        let raw = #"""
        { "session_id": "s1", "phase": "editing" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.phase, .editing)
        XCTAssertEqual(row.phaseLabel, "editing")
    }

    func testDecodeExtrasAndDeriveActivityLine() throws {
        let raw = #"""
        {
          "session_id": "s1",
          "source": "codex",
          "cwd": "/tmp/work",
          "summary": "fallback",
          "extras": {
            "tool_name": "Bash",
            "command": "pytest tests/test_app.py",
            "ignored": 42
          }
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.extras["tool_name"], "Bash")
        XCTAssertEqual(row.command, "pytest tests/test_app.py")
        XCTAssertNil(row.extras["ignored"])
        XCTAssertEqual(row.activityLine, "Codex · work · cmd: pytest tests/test_app.py")
    }

    func testActivityLineFallsBackToFileAndTool() {
        let file = SessionRow(
            sessionId: "s1",
            source: "claude_code",
            extras: ["file_path": "/tmp/work/app.py"]
        )
        XCTAssertEqual(file.activityLine, "Claude · file: app.py")

        let tool = SessionRow(
            sessionId: "s2",
            source: "cursor",
            extras: ["tool_name": "grep"]
        )
        XCTAssertEqual(tool.activityLine, "Cursor · tool: grep")
    }

    func testCanAttemptJumpUsesTargetsSourceKindOrProcessId() {
        XCTAssertFalse(SessionRow(sessionId: "plain").canAttemptJump)
        XCTAssertTrue(SessionRow(sessionId: "cwd", cwd: "/tmp/work").canAttemptJump)
        XCTAssertTrue(SessionRow(sessionId: "url", jumpUrl: "codex://threads/s1").canAttemptJump)
        XCTAssertTrue(SessionRow(sessionId: "source", source: "cursor").canAttemptJump)
        XCTAssertTrue(SessionRow(sessionId: "kind", kind: "cli_agent").canAttemptJump)
        XCTAssertTrue(SessionRow(sessionId: "pid", processId: 42).canAttemptJump)
    }

    func testRoundTripSnakeCase() throws {
        let row = SessionRow(
            sessionId: "s1",
            title: "Task",
            summary: "",
            state: .paused,
            priority: .p1,
            createdAtMs: 100,
            updatedAtMs: 200,
            closedAtMs: nil
        )
        let data = try encoder.encode(row)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNotNil(json?["session_id"])
        XCTAssertNotNil(json?["updated_at_ms"])
        XCTAssertNil(json?["sessionId"])  // must NOT be camelCase on the wire
    }
}
