import XCTest
@testable import DeskmateCore

final class SessionListAdapterTests: XCTestCase {
    private func row(
        _ id: String,
        state: SessionRow.State = .active,
        priority: Priority = .p2,
        updated: Int = 1_000,
        closed: Int? = nil,
        phase: SessionRow.Phase = .running
    ) -> SessionRow {
        SessionRow(
            sessionId: id,
            state: state,
            priority: priority,
            updatedAtMs: updated,
            closedAtMs: closed,
            phase: phase
        )
    }

    func testActiveRowsComeBeforeClosed() {
        let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
        let rows = [
            row("closed", state: .closed, updated: 5_000, closed: 5_000),
            row("active", state: .active, updated: 1_000),
        ]
        let out = adapter.display(sessions: rows, nowMs: 10_000)
        XCTAssertEqual(out.map(\.sessionId), ["active", "closed"])
    }

    func testHigherPriorityBeatsRecentUpdate() {
        let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
        let rows = [
            row("p3-new", priority: .p3, updated: 10_000),
            row("p0-old", priority: .p0, updated: 1_000),
        ]
        let out = adapter.display(sessions: rows, nowMs: 20_000)
        XCTAssertEqual(out.map(\.sessionId), ["p0-old", "p3-new"])
    }

    func testSamePriorityOrdersByUpdatedAtDesc() {
        let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
        let rows = [
            row("a", priority: .p2, updated: 1_000),
            row("b", priority: .p2, updated: 3_000),
            row("c", priority: .p2, updated: 2_000),
        ]
        let out = adapter.display(sessions: rows, nowMs: 10_000)
        XCTAssertEqual(out.map(\.sessionId), ["b", "c", "a"])
    }

    func testMaxRowsCap() {
        let adapter = SessionListAdapter(maxRows: 2, showClosedAfterMs: nil)
        let rows = (1...5).map { row("s\($0)", updated: 100 * $0) }
        let out = adapter.display(sessions: rows, nowMs: 10_000)
        XCTAssertEqual(out.count, 2)
    }

    func testStaleClosedSessionsAreHidden() {
        let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: 1_000)
        let rows = [
            row("fresh-closed", state: .closed, updated: 9_500, closed: 9_500),
            row("stale-closed", state: .closed, updated: 500, closed: 500),
        ]
        let out = adapter.display(sessions: rows, nowMs: 10_000)
        XCTAssertEqual(out.map(\.sessionId), ["fresh-closed"])
    }

    func testNilTTLKeepsEvenAncientClosed() {
        let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
        let rows = [
            row("ancient", state: .closed, updated: 0, closed: 0),
        ]
        let out = adapter.display(sessions: rows, nowMs: 10_000_000)
        XCTAssertEqual(out.count, 1)
    }

    func testClosedWithoutClosedAtMsIsKept() {
        // Malformed row (closed but no closed_at_ms) — still renderable.
        let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: 1_000)
        let rows = [
            row("malformed", state: .closed, updated: 500, closed: nil),
        ]
        let out = adapter.display(sessions: rows, nowMs: 10_000)
        XCTAssertEqual(out.count, 1)
    }

    func testEmptyInputProducesEmptyOutput() {
        XCTAssertEqual(
            SessionListAdapter().display(sessions: [], nowMs: 0),
            []
        )
    }

    func testFineGrainedPhaseOrdering() {
        let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
        let rows = [
            row("thinking", phase: .thinking),
            row("editing", phase: .editing),
            row("tool", phase: .runningTool),
            row("failed", phase: .failed),
        ]
        let out = adapter.display(sessions: rows, nowMs: 0)
        XCTAssertEqual(out.map(\.sessionId), ["failed", "tool", "editing", "thinking"])
    }
}
