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

    func testClosedSessionsDoNotCreateContent() {
        let content = IslandContentProjection.compute(
            islandState: nil,
            sessions: [SessionRow(sessionId: "done", state: .closed)],
            approvals: []
        )
        XCTAssertEqual(content, .idle)
    }
}
