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

    func testDecodeStructuredToolExtrasAndDeriveActivityLine() throws {
        let raw = #"""
        {
          "session_id": "tools",
          "source": "deskmate",
          "phase": "completed",
          "summary": "Reminder scheduled for 1 minute: stretch.",
          "extras": {
            "tool_action": "deskmate_schedule_reminder",
            "tool_target": "stretch",
            "tool_outcome": "Reminder scheduled for 1 minute: stretch.",
            "tool_needs_user": "false",
            "tool_summary": "action=deskmate_schedule_reminder; status=completed; target=stretch; outcome=Reminder scheduled for 1 minute: stretch.; needs_user=false",
            "tool_task_id": "deskmate-tool-task-default-1",
            "tool_task_status": "completed",
            "tool_task_summary": "action=deskmate_schedule_reminder; status=completed; target=stretch"
          }
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(SessionRow.self, from: raw)
        XCTAssertEqual(row.toolAction, "deskmate_schedule_reminder")
        XCTAssertEqual(row.toolTarget, "stretch")
        XCTAssertEqual(row.toolOutcome, "Reminder scheduled for 1 minute: stretch.")
        XCTAssertFalse(row.toolNeedsUser)
        XCTAssertTrue(row.toolSummary?.hasPrefix("action=deskmate_schedule_reminder") == true)
        XCTAssertEqual(row.toolTaskId, "deskmate-tool-task-default-1")
        XCTAssertEqual(row.toolTaskStatus, "completed")
        XCTAssertTrue(row.toolTaskSummary?.contains("target=stretch") == true)
        XCTAssertEqual(row.activityLine, "Deskmate · tool: deskmate_schedule_reminder -> stretch")
    }

    func testCockpitPresentationExtras() {
        let row = SessionRow(
            sessionId: "s1",
            source: "codex",
            kind: "hook_session",
            processId: 42,
            extras: [
                "prompt": "Refactor the island row",
                "last_assistant_message": "Implemented the compact cockpit.",
                "branch": "feat/island-cockpit",
                "window_title": "IslandOverlay.swift",
                "phase_source": "unobserved"
            ]
        )
        XCTAssertEqual(row.promptText, "Refactor the island row")
        XCTAssertEqual(row.assistantText, "Implemented the compact cockpit.")
        XCTAssertEqual(row.branchName, "feat/island-cockpit")
        XCTAssertEqual(row.windowTitle, "IslandOverlay.swift")
        XCTAssertEqual(row.phaseSource, "unobserved")
        XCTAssertEqual(row.kindLabel, "Hook")
    }

    func testJumpDiagnosticsExtras() {
        let row = SessionRow(
            sessionId: "s1",
            extras: [
                "last_jump_effect": "session.jump.workspace_opened",
                "last_jump_route": "workspace",
                "last_jump_detail": "Opened workspace in Cursor.",
                "last_jump_attempts": "terminal:route_failed; workspace:Cursor:opened",
                "last_jump_at_ms": "7777"
            ]
        )

        XCTAssertEqual(row.lastJumpEffect, "session.jump.workspace_opened")
        XCTAssertEqual(row.lastJumpRoute, "workspace")
        XCTAssertEqual(row.lastJumpDetail, "Opened workspace in Cursor.")
        XCTAssertEqual(row.lastJumpAttempts, "terminal:route_failed; workspace:Cursor:opened")
        XCTAssertEqual(row.lastJumpAtMs, 7_777)
        XCTAssertEqual(row.recentOutcomeLine, "Jump: Opened workspace in Cursor.")
    }

    func testApprovalResolutionExtras() {
        let row = SessionRow(
            sessionId: "s1",
            extras: [
                "last_approval_id": "ap-1",
                "last_approval_decision": "deny",
                "last_approval_prompt": "Allow shell?",
                "last_approval_risk_level": "high",
                "last_approval_preview": "cmd: sudo rm -rf build/cache",
                "last_approval_resolved_at_ms": "5000"
            ]
        )

        XCTAssertEqual(row.lastApprovalId, "ap-1")
        XCTAssertEqual(row.lastApprovalDecision, "deny")
        XCTAssertEqual(row.lastApprovalPrompt, "Allow shell?")
        XCTAssertEqual(row.lastApprovalRiskLevel, "high")
        XCTAssertEqual(row.lastApprovalPreview, "cmd: sudo rm -rf build/cache")
        XCTAssertEqual(row.lastApprovalResolvedAtMs, 5_000)
        XCTAssertEqual(row.recentOutcomeLine, "Denied high approval: cmd: sudo rm -rf build/cache")
    }

    func testAgentHealthSummaryRollsUpRuntimeVisibility() {
        let rows = [
            SessionRow(
                sessionId: "hook",
                phase: .waitingForApproval,
                source: "codex",
                kind: "hook_session",
                extras: ["phase_source": "hook"]
            ),
            SessionRow(
                sessionId: "cli",
                source: "claude_code",
                kind: "cli_agent",
                extras: ["phase_source": "unobserved"]
            ),
            SessionRow(
                sessionId: "ide",
                source: "cursor",
                kind: "gui_ide"
            ),
            SessionRow(
                sessionId: "closed",
                state: .closed,
                source: "windsurf",
                kind: "gui_ide"
            )
        ]

        let summary = AgentHealthSummary(sessions: rows)
        XCTAssertEqual(summary.total, 4)
        XCTAssertEqual(summary.active, 3)
        XCTAssertEqual(summary.hookSessions, 1)
        XCTAssertEqual(summary.cliAgents, 1)
        XCTAssertEqual(summary.guiIDEs, 1)
        XCTAssertEqual(summary.unobserved, 1)
        XCTAssertEqual(summary.awaitingAction, 1)
        XCTAssertTrue(summary.statusLine.contains("Active 3"))
        XCTAssertTrue(summary.statusLine.contains("action 1"))
        XCTAssertTrue(summary.kindLine.contains("Hook 1"))
        XCTAssertTrue(summary.sourceLine.contains("Codex 1"))
        XCTAssertEqual(summary.expandedBadgeText, "1 action")
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
        XCTAssertEqual(tool.toolAction, "grep")
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
