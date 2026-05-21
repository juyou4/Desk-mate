import XCTest
@testable import DeskmateCore

final class ReminderListAdapterTests: XCTestCase {
    private func row(
        _ id: String,
        status: ReminderRow.Status = .pending,
        priority: Priority = .p1,
        dueAtMs: Int = 1_000,
        resolvedAtMs: Int? = nil
    ) -> ReminderRow {
        ReminderRow(
            reminderId: id,
            dueAtMs: dueAtMs,
            status: status,
            priority: priority,
            resolvedAtMs: resolvedAtMs
        )
    }

    func testPendingBeforeFiredBeforeTerminal() {
        let adapter = ReminderListAdapter(
            maxRows: 10, hideResolvedAfterMs: nil
        )
        let rows = [
            row("cancelled", status: .cancelled),
            row("fired", status: .fired),
            row("pending", status: .pending),
            row("dismissed", status: .dismissed),
        ]
        let out = adapter.display(reminders: rows, nowMs: 10_000).map(\.reminderId)
        XCTAssertEqual(out, ["pending", "fired", "dismissed", "cancelled"])
    }

    func testHigherPriorityWinsWithinSameStatus() {
        let adapter = ReminderListAdapter(
            maxRows: 10, hideResolvedAfterMs: nil
        )
        let rows = [
            row("p3", priority: .p3),
            row("p0", priority: .p0),
            row("p2", priority: .p2),
        ]
        let out = adapter.display(reminders: rows, nowMs: 0).map(\.reminderId)
        XCTAssertEqual(out, ["p0", "p2", "p3"])
    }

    func testSoonerDueFirstOnTie() {
        let adapter = ReminderListAdapter(
            maxRows: 10, hideResolvedAfterMs: nil
        )
        let rows = [
            row("later", dueAtMs: 5_000),
            row("soon", dueAtMs: 1_000),
            row("medium", dueAtMs: 3_000),
        ]
        let out = adapter.display(reminders: rows, nowMs: 0).map(\.reminderId)
        XCTAssertEqual(out, ["soon", "medium", "later"])
    }

    func testResolvedOlderThanCutoffAreHidden() {
        let adapter = ReminderListAdapter(
            maxRows: 10, hideResolvedAfterMs: 1_000
        )
        let rows = [
            row("fresh", status: .dismissed, resolvedAtMs: 9_500),
            row("stale", status: .dismissed, resolvedAtMs: 500),
        ]
        let out = adapter.display(reminders: rows, nowMs: 10_000).map(\.reminderId)
        XCTAssertEqual(out, ["fresh"])
    }

    func testMaxRowsCap() {
        let adapter = ReminderListAdapter(
            maxRows: 2, hideResolvedAfterMs: nil
        )
        let rows = (1...5).map { row("r\($0)", dueAtMs: 100 * $0) }
        XCTAssertEqual(adapter.display(reminders: rows, nowMs: 0).count, 2)
    }

    func testEmptyInputProducesEmptyOutput() {
        XCTAssertEqual(
            ReminderListAdapter().display(reminders: [], nowMs: 0),
            []
        )
    }
}
