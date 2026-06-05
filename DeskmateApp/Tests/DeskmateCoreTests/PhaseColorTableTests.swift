import XCTest
import SwiftUI
@testable import DeskmateCore

final class PhaseColorTableTests: XCTestCase {
    /// R7.4 Property 5: PhaseColorTable is a pure function — repeated calls
    /// with the same arguments return identical results.
    ///
    /// **Validates: Requirements 7.4**
    func testPurityAcrossRepeatedCalls() {
        for phase in SessionRow.Phase.allCases {
            for scheme in [ColorScheme.light, ColorScheme.dark] {
                let first = PhaseColorTable.resolve(phase, scheme: scheme)
                let second = PhaseColorTable.resolve(phase, scheme: scheme)
                XCTAssertEqual(first, second, "PhaseColorTable not pure for \(phase) / \(scheme)")
            }
        }
    }

    /// R7.3: Every SessionRow.Phase case is covered (exhaustive switch).
    /// This test iterates all cases and asserts a non-nil triple is returned.
    ///
    /// **Validates: Requirements 7.3**
    func testExhaustiveCoverage() {
        for phase in SessionRow.Phase.allCases {
            let triple = PhaseColorTable.resolve(phase, scheme: .dark)
            // If the switch were non-exhaustive, this would crash at compile time.
            // At runtime, we just verify the triple has meaningful values.
            XCTAssertNotNil(triple.foreground)
            XCTAssertNotNil(triple.background)
            XCTAssertNotNil(triple.stroke)
        }
    }

    func testWaitingApprovalUrgencyEscalatesAtThresholds() {
        let now = 100_000
        XCTAssertEqual(
            PhaseColorTable.urgency(
                phase: .waitingForApproval,
                createdAtMs: now - PhaseColorTable.unattendedThresholdMs + 1,
                nowMs: now
            ),
            .normal
        )
        XCTAssertEqual(
            PhaseColorTable.urgency(
                phase: .waitingForApproval,
                createdAtMs: now - PhaseColorTable.unattendedThresholdMs,
                nowMs: now
            ),
            .unattended
        )
        XCTAssertEqual(
            PhaseColorTable.urgency(
                phase: .waitingForApproval,
                createdAtMs: now - PhaseColorTable.overdueThresholdMs,
                nowMs: now
            ),
            .overdue
        )
    }

    func testNonWaitingPhasesDoNotEscalate() {
        let now = 100_000
        for phase in SessionRow.Phase.allCases where
            phase != .waitingForApproval && phase != .waitingForAnswer {
            XCTAssertEqual(
                PhaseColorTable.urgency(
                    phase: phase,
                    createdAtMs: now - PhaseColorTable.overdueThresholdMs * 2,
                    nowMs: now
                ),
                .normal,
                "\(phase) should not escalate"
            )
        }
    }
}
