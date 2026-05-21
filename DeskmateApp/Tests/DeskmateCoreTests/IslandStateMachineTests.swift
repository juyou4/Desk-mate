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
            activityId: nil, detail: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act-1", detail: nil, priority: .p2, tsMs: 200
        ))
        XCTAssertEqual(effect.transition, .morph)
        XCTAssertEqual(sm.surface.kind, .liveActivity)
    }

    func testLowerPriorityCannotReplaceHigher() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, priority: .p1, tsMs: 100
        ))
        let effect = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act", detail: nil, priority: .p3, tsMs: 200
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
            activityId: nil, detail: nil, priority: .p3, tsMs: 100
        ))
        let effect = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act", detail: nil, priority: .p0, tsMs: 200
        ))
        XCTAssertEqual(effect.transition, .morph)
        XCTAssertEqual(sm.priority, .p0)
        XCTAssertEqual(sm.surface.activityId, "act")
    }

    func testDismissWithMatchingIdSlidesOut() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: "s1", tsMs: 200))
        XCTAssertEqual(effect.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testDismissWithWrongIdIsIgnored() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: "other", tsMs: 200))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
    }

    func testGenericDismissClearsCurrent() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, priority: .p2, tsMs: 100
        ))
        let effect = sm.apply(.dismiss(id: nil, tsMs: 200))
        XCTAssertEqual(effect.transition, .slideOut)
        XCTAssertEqual(sm.surface.kind, .empty)
    }

    func testUpdateOnlyTakesEffectForMatchingActivity() {
        var sm = IslandStateMachine()
        _ = sm.apply(.present(
            kind: .liveActivity, sessionId: nil,
            activityId: "act-1", detail: nil, priority: .p2, tsMs: 100
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
            priority: .p2, tsMs: 100
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
            priority: .p2, tsMs: 100
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
            activityId: nil, detail: nil, priority: .p2, tsMs: 100
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
            activityId: nil, detail: nil, priority: .p1, tsMs: 100
        ))
        let effect = sm.apply(.tick(tsMs: 10_000))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
    }

    func testUserInteractResetsIdleTimer() {
        var sm = IslandStateMachine(autoDismissMs: 1_000)
        _ = sm.apply(.present(
            kind: .notificationCard, sessionId: "s1",
            activityId: nil, detail: nil, priority: .p2, tsMs: 0
        ))
        _ = sm.apply(.userInteract(tsMs: 900))
        let effect = sm.apply(.tick(tsMs: 1_500))
        XCTAssertEqual(effect.transition, .none)
        XCTAssertEqual(sm.surface.kind, .notificationCard)
    }
}
