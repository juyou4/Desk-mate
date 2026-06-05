import XCTest
@testable import DeskmateCore

final class IslandStateMachineTests: XCTestCase {
    func testPresentFromEmptySlidesIn() {
        var sm = IslandStateMachine()
        let effect = sm.apply(.present(
            kind: .notificationCard,
            sessionId: "s1",
            activityId: nil,
            detail: nil,
            surfaceId: nil,
            priority: .p2,
            tsMs: 100
        ))
        XCTAssertEqual(effect.transition, .slideIn)
        XCTAssertTrue(effect.changed)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
        XCTAssertEqual(sm.priority, .p2)
    }

    func testPresentOfDifferentKindMorphs() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act-1", detail: nil, surfaceId: nil, priority: .p2, tsMs: 200
        ))
        XCTAssertEqual(effect.transition, .morph)
        XCTAssertEqual(sm.surface.kind, .liveActivity)
    }

    func testLowerPriorityCannotReplaceHigher() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p1, tsMs: 100
        ))
        let effect = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act", detail: nil, surfaceId: nil, priority: .p3, tsMs: 200
        ))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertFalse(effect.changed)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
        XCTAssertEqual(sm.priority, .p1)
    }

    func testHigherPriorityReplacesLower() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p3, tsMs: 100
        ))
        let effect = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act", detail: nil, surfaceId: nil, priority: .p0, tsMs: 200
        ))
        XCTAssertEqual(effect.transition, .morph)
        XCTAssertEqual(sm.priority, .p0)
        XCTAssertEqual(sm.surface.activityId, "act")
    }

    func testDismissWithMatchingIdSlidesOut() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: "s1", tsMs: 200))
        XCTAssertEqual(effect.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testDismissWithWrongIdIsIgnored() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: "other", tsMs: 200))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
    }

    func testGenericDismissClearsCurrent() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: nil, tsMs: 200))
        XCTAssertEqual(effect.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testUpdateOnlyTakesEffectForMatchingActivity() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act-1", detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let matched = sm.apply(
            .update(activityId: "act-1", detail: nil, tsMs: 200)
        )
        XCTAssertEqual(matched.transition, .none)
        let touchedBefore = sm.lastTouchedMs
        XCTAssertEqual(touchedBefore, 200)

        let unmatched = sm.apply(
            .update(activityId: "other", detail: nil, tsMs: 500)
        )
        XCTAssertEqual(unmatched.transition, .none)
        XCTAssertEqual(sm.lastTouchedMs, touchedBefore)  // no touch on mismatch
    }

    func testUpdateWithDetailMutatesSurfaceInPlace() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "coding-VSCode", detail: "a.py",
            surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.update(
            activityId: "coding-VSCode", detail: "b.py", tsMs: 200
        ))
        // Phase 13-ii: update now mutates the detail slot and the
        // reducer reports a change event (no morph / slide, just an
        // in-place refresh so the overlay re-renders).
        XCTAssertEqual(effect.transition, .none)
        XCTAssertTrue(effect.changed)
        XCTAssertEqual(sm.surface.detail, "b.py")
    }

    func testUpdateWithUnchangedDetailIsNoop() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "coding-Zed", detail: "main.rs",
            surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.update(
            activityId: "coding-Zed", detail: "main.rs", tsMs: 200
        ))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertFalse(effect.changed)
    }

    func testTickAutoDismissesAfterTimeout() {
        var sm = IslandStateMachine(autoDismissMs: 1_000)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let early = sm.apply(.tick(tsMs: 500))
        XCTAssertEqual(early.transition, .none)
        let late = sm.apply(.tick(tsMs: 2_000))
        XCTAssertEqual(late.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testHighPriorityIsPinned() {
        var sm = IslandStateMachine(pinnedPriorityCeiling: .p1, autoDismissMs: 1_000)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p1, tsMs: 100
        ))
        let effect = sm.apply(.tick(tsMs: 10_000))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
    }

    func testUserInteractResetsIdleTimer() {
        var sm = IslandStateMachine(autoDismissMs: 1_000)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil, priority: .p2, tsMs: 0
        ))
        _ = sm.apply(.userInteract(tsMs: 900))
        let effect = sm.apply(.tick(tsMs: 1_500))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
    }

    // MARK: - SurfaceId-based dismiss matching (R3.4, R3.5)

    func testDismissWithMatchingSurfaceIdSlidesOut() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil,
            surfaceId: "approval:abc123", priority: .p0, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: "approval:abc123", tsMs: 200))
        XCTAssertEqual(effect.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testDismissWithMismatchedSurfaceIdIsNoop() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil,
            surfaceId: "approval:abc123", priority: .p0, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: "approval:other", tsMs: 200))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertFalse(effect.changed)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
        XCTAssertEqual(sm.surface.surfaceId, "approval:abc123")
    }

    func testDismissOnEmptySurfaceIsNoop() {
        var sm = IslandStateMachine()
        let effect = sm.apply(.dismiss(id: "approval:abc123", tsMs: 100))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertFalse(effect.changed)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testDismissWithoutSurfaceIdFallsBackToSessionId() {
        var sm = IslandStateMachine()
        // Surface without surfaceId — should fall back to sessionId matching
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil,
            surfaceId: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: "s1", tsMs: 200))
        XCTAssertEqual(effect.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testGenericDismissClearsSurfaceWithSurfaceId() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil,
            surfaceId: "approval:abc123", priority: .p0, tsMs: 100
        ))
        // Generic dismiss (id: nil) should still clear
        let effect = sm.apply(.dismiss(id: nil, tsMs: 200))
        XCTAssertEqual(effect.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testPresentStoresSurfaceId() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil,
            surfaceId: "question:s1:3", priority: .p1, tsMs: 100
        ))
        XCTAssertEqual(sm.surface.surfaceId, "question:s1:3")
    }

    // MARK: - SneakPeek (R5)

    /// R5.1: P1 notification_card with degradation < 4 enters SneakPeek.
    ///
    /// **Validates: Requirements 5.1**
    func testSneakPeekEntryForP1NotificationCard() {
        var sm = IslandStateMachine(degradationLevel: 0)
        let effect = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: "approval:s1",
            priority: .p1, tsMs: 100
        ))
        XCTAssertTrue(sm.isSneakPeek)
        XCTAssertEqual(effect.transition, .slideIn)
        XCTAssertTrue(effect.changed)
    }

    /// R5.3: P0 skips SneakPeek and pins directly.
    ///
    /// **Validates: Requirements 5.3**
    func testSneakPeekSkippedForP0() {
        var sm = IslandStateMachine(degradationLevel: 0)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil,
            priority: .p0, tsMs: 100
        ))
        XCTAssertFalse(sm.isSneakPeek)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
    }

    /// R5.6: Degradation >= 4 universally skips SneakPeek.
    ///
    /// **Validates: Requirements 5.6**
    func testSneakPeekSkippedWhenDegradationHigh() {
        var sm = IslandStateMachine(degradationLevel: 4)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil,
            priority: .p1, tsMs: 100
        ))
        XCTAssertFalse(sm.isSneakPeek)
    }

    /// R5.2: SneakPeek timeout collapses to empty.
    ///
    /// **Validates: Requirements 5.2**
    func testSneakPeekTimeoutCollapsesToEmpty() {
        var sm = IslandStateMachine(degradationLevel: 0)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil,
            priority: .p1, tsMs: 100
        ))
        XCTAssertTrue(sm.isSneakPeek)
        let effect = sm.apply(.tick(tsMs: 2000))  // past 1800ms deadline
        XCTAssertFalse(sm.isSneakPeek)
        XCTAssertEqual(sm.surface.kind, .empty)
        XCTAssertEqual(effect.transition, .slideOut)
    }

    /// R5.5: Hover during SneakPeek promotes to full notification_card.
    ///
    /// **Validates: Requirements 5.5**
    func testSneakPeekPromoteOnHover() {
        var sm = IslandStateMachine(degradationLevel: 0)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil,
            priority: .p1, tsMs: 100
        ))
        XCTAssertTrue(sm.isSneakPeek)
        let effect = sm.promoteSneakPeek(tsMs: 500)
        XCTAssertFalse(sm.isSneakPeek)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
        XCTAssertTrue(effect.changed)
    }

    /// R5.8: New present during SneakPeek preempts the existing peek.
    ///
    /// **Validates: Requirements 5.8**
    func testSneakPeekPreemptedByNewPresent() {
        var sm = IslandStateMachine(degradationLevel: 0)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: "a",
            priority: .p2, tsMs: 100
        ))
        XCTAssertTrue(sm.isSneakPeek)
        // New P1 present preempts
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s2",
            activityId: nil, detail: nil, surfaceId: "b",
            priority: .p1, tsMs: 200
        ))
        XCTAssertTrue(sm.isSneakPeek)  // new peek started
        XCTAssertEqual(sm.surface.sessionId, "s2")
    }

    /// R5.9: Degradation crossing to >= 4 during peek collapses it.
    ///
    /// **Validates: Requirements 5.9**
    func testDegradationCrossingCollapsesPeek() {
        var sm = IslandStateMachine(degradationLevel: 0)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, surfaceId: nil,
            priority: .p1, tsMs: 100
        ))
        XCTAssertTrue(sm.isSneakPeek)
        let effect = sm.apply(.degradationChanged(level: 4, tsMs: 500))
        XCTAssertFalse(sm.isSneakPeek)
        XCTAssertEqual(sm.surface.kind, .empty)
        XCTAssertEqual(effect.transition, .slideOut)
    }
}
