import XCTest
@testable import DeskmateCore

final class IslandContentProjectionTests: XCTestCase {
    func testApprovalBeatsBuildNotificationAndSessions() {
        let session = SessionRow(sessionId: "s1", phase: .running)
        let approval = ApprovalRow(approvalId: "a1", sessionId: "s1")
        let state = IslandSurfaceState(
            kind: .liveActivity,
            activityId: "build-demo",
            detail: "Running"
        )

        let content = IslandContentProjection.compute(
            islandState: state,
            sessions: [session],
            approvals: [approval]
        )

        guard case .approval(let matched, let row) = content else {
            return XCTFail("expected approval, got \(content)")
        }
        XCTAssertEqual(matched?.sessionId, "s1")
        XCTAssertEqual(row.approvalId, "a1")
    }

    func testBuildBeatsNotificationAndDetectsDoneFailure() {
        let content = IslandContentProjection.compute(
            islandState: IslandSurfaceState(
                kind: .liveActivity,
                activityId: "build-demo",
                detail: "Tests failed"
            ),
            sessions: [SessionRow(sessionId: "s1")],
            approvals: []
        )

        guard case .build(let activityId, _, let progress, let isDone, let isFailed) = content else {
            return XCTFail("expected build, got \(content)")
        }
        XCTAssertEqual(activityId, "build-demo")
        XCTAssertNil(progress)
        XCTAssertFalse(isDone)
        XCTAssertFalse(isFailed)
    }

    func testMultiSessionFocusesActionableSession() {
        let running = SessionRow(sessionId: "running", phase: .running)
        let answer = SessionRow(sessionId: "answer", phase: .waitingForAnswer)
        let content = IslandContentProjection.compute(
            islandState: nil,
            sessions: [running, answer],
            approvals: []
        )

        guard case .multiSession(let sessions, let focus) = content else {
            return XCTFail("expected multiSession, got \(content)")
        }
        XCTAssertEqual(sessions.map(\.sessionId), ["running", "answer"])
        XCTAssertEqual(focus?.sessionId, "answer")
    }

    func testActiveTaskBeatsOrdinarySession() {
        let task = TaskRow(
            taskId: "task-1",
            title: "Polish island task lane",
            status: .inProgress
        )
        let content = IslandContentProjection.compute(
            islandState: nil,
            sessions: [SessionRow(sessionId: "s1", phase: .running)],
            approvals: [],
            tasks: [task]
        )

        guard case .task(let row) = content else {
            return XCTFail("expected task, got \(content)")
        }
        XCTAssertEqual(row.taskId, "task-1")
    }

    func testNotificationBeatsActiveTask() {
        let task = TaskRow(taskId: "task-1", status: .inProgress)
        let content = IslandContentProjection.compute(
            islandState: IslandSurfaceState(
                kind: .notificationCard,
                activityId: "reminder-1",
                detail: "Stand up"
            ),
            sessions: [],
            approvals: [],
            tasks: [task]
        )

        guard case .notification(let state) = content else {
            return XCTFail("expected notification, got \(content)")
        }
        XCTAssertEqual(state.activityId, "reminder-1")
    }

    func testDueReminderBeatsOrdinarySession() {
        let reminder = ReminderRow(
            reminderId: "rem-1",
            text: "Drink water",
            dueAtMs: 900,
            status: .pending
        )
        let content = IslandContentProjection.compute(
            islandState: nil,
            sessions: [SessionRow(sessionId: "s1", phase: .running)],
            approvals: [],
            reminders: [reminder],
            nowMs: 1_000
        )

        guard case .reminder(let row) = content else {
            return XCTFail("expected reminder, got \(content)")
        }
        XCTAssertEqual(row.reminderId, "rem-1")
    }

    func testFutureReminderWaitsBehindActiveSession() {
        let reminder = ReminderRow(
            reminderId: "rem-1",
            text: "Drink water",
            dueAtMs: 10_000,
            status: .pending
        )
        let content = IslandContentProjection.compute(
            islandState: nil,
            sessions: [SessionRow(sessionId: "s1", phase: .running)],
            approvals: [],
            reminders: [reminder],
            nowMs: 1_000
        )

        guard case .session(let row) = content else {
            return XCTFail("expected session, got \(content)")
        }
        XCTAssertEqual(row.sessionId, "s1")
    }

    func testFutureReminderShowsWhenIslandOtherwiseIdle() {
        let reminder = ReminderRow(
            reminderId: "rem-1",
            text: "Drink water",
            dueAtMs: 10_000,
            status: .pending
        )
        let content = IslandContentProjection.compute(
            islandState: nil,
            sessions: [],
            approvals: [],
            reminders: [reminder],
            nowMs: 1_000
        )

        guard case .reminder(let row) = content else {
            return XCTFail("expected reminder, got \(content)")
        }
        XCTAssertEqual(row.reminderId, "rem-1")
    }

    func testClosedSessionsDoNotCreateContent() {
        let content = IslandContentProjection.compute(
            islandState: nil,
            sessions: [SessionRow(sessionId: "done", state: .closed)],
            approvals: []
        )
        XCTAssertEqual(content, .idle)
    }
}
