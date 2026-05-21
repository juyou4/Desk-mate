import XCTest
@testable import DeskmateCore

final class IslandHoverRouterTests: XCTestCase {
    func testHoverEnterFromCompactPromotesToSessionList() {
        let r = IslandHoverRouter()
        XCTAssertEqual(
            r.decide(event: .enter(tsMs: 0), current: .compact),
            .promote(to: .sessionList)
        )
    }

    func testHoverEnterFromNonCompactIsNoop() {
        let r = IslandHoverRouter()
        XCTAssertEqual(
            r.decide(event: .enter(tsMs: 0), current: .liveActivity),
            .noop
        )
    }

    func testHoverLeaveFromSessionListReturnsToCompact() {
        let r = IslandHoverRouter()
        XCTAssertEqual(
            r.decide(event: .leave(tsMs: 0), current: .sessionList),
            .promote(to: .compact)
        )
    }

    func testHoverLeaveFromNotificationCardIsNoop() {
        let r = IslandHoverRouter()
        XCTAssertEqual(
            r.decide(event: .leave(tsMs: 0), current: .notificationCard),
            .noop
        )
    }

    func testTapOnCompactPromotesToSessionList() {
        let r = IslandHoverRouter()
        XCTAssertEqual(
            r.decide(event: .tap(tsMs: 0), current: .compact),
            .promote(to: .sessionList)
        )
    }

    func testTapOnSessionListDismisses() {
        let r = IslandHoverRouter()
        XCTAssertEqual(
            r.decide(event: .tap(tsMs: 0), current: .sessionList),
            .dismiss
        )
    }

    func testTapOnEmptyIsNoop() {
        let r = IslandHoverRouter()
        XCTAssertEqual(r.decide(event: .tap(tsMs: 0), current: .empty), .noop)
    }

    func testConfigTogglesDisableBehaviour() {
        let r = IslandHoverRouter(
            hoverPromotesToSessionList: false,
            hoverLeaveReturnsToCompact: false,
            tapPromotesToSessionList: false,
            tapOnSessionListDismisses: false
        )
        XCTAssertEqual(r.decide(event: .enter(tsMs: 0), current: .compact), .noop)
        XCTAssertEqual(r.decide(event: .leave(tsMs: 0), current: .sessionList), .noop)
        XCTAssertEqual(r.decide(event: .tap(tsMs: 0), current: .compact), .noop)
        XCTAssertEqual(r.decide(event: .tap(tsMs: 0), current: .sessionList), .noop)
    }
}
