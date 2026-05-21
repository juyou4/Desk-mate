import XCTest
@testable import DeskmateCore

final class PetWindowGeometryTests: XCTestCase {
    private let petSize = CGSize(width: 64, height: 64)
    private let margin: CGFloat = 8

    private func singleScreen() -> [PetScreen] {
        [PetScreen(id: 0, visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 900))]
    }

    private func dualScreen() -> [PetScreen] {
        [
            PetScreen(id: 0, visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 900)),
            PetScreen(id: 1, visibleFrame: CGRect(x: 1440, y: 0, width: 2560, height: 1440)),
        ]
    }

    // MARK: - Clamping

    func testClampKeepsPositionWhenInside() {
        let geo = PetWindowGeometry(screens: singleScreen(), petSize: petSize, edgeMargin: margin)
        let resolved = geo.clamp(requested: CGPoint(x: 500, y: 300))
        XCTAssertEqual(resolved?.origin, CGPoint(x: 500, y: 300))
        XCTAssertEqual(resolved?.screenId, 0)
        XCTAssertEqual(resolved?.didClamp, false)
    }

    func testClampPushesOffscreenOriginBackInside() {
        let geo = PetWindowGeometry(screens: singleScreen(), petSize: petSize, edgeMargin: margin)
        let resolved = geo.clamp(requested: CGPoint(x: -500, y: -200))
        XCTAssertNotNil(resolved)
        XCTAssertEqual(resolved?.screenId, 0)
        XCTAssertEqual(resolved?.didClamp, true)
        XCTAssertEqual(resolved?.origin.x, margin)
        XCTAssertEqual(resolved?.origin.y, margin)
    }

    func testClampRespectsEdgeMarginOnRightAndTop() {
        let geo = PetWindowGeometry(screens: singleScreen(), petSize: petSize, edgeMargin: margin)
        let resolved = geo.clamp(requested: CGPoint(x: 10_000, y: 10_000))
        XCTAssertNotNil(resolved)
        XCTAssertEqual(resolved?.origin.x, 1440 - margin - petSize.width)
        XCTAssertEqual(resolved?.origin.y, 900 - margin - petSize.height)
    }

    // MARK: - Multi-screen

    func testClampLandsOnScreenContainingCentre() {
        let geo = PetWindowGeometry(screens: dualScreen(), petSize: petSize, edgeMargin: margin)
        let resolved = geo.clamp(requested: CGPoint(x: 2000, y: 500))
        XCTAssertEqual(resolved?.screenId, 1)
        XCTAssertEqual(resolved?.didClamp, false)
    }

    func testClampFallsBackToNearestScreenWhenCentreIsOffscreen() {
        let geo = PetWindowGeometry(screens: dualScreen(), petSize: petSize, edgeMargin: margin)
        // x = 4500 is beyond both screens but closer to screen 1 centre.
        let resolved = geo.clamp(requested: CGPoint(x: 4500, y: 500))
        XCTAssertEqual(resolved?.screenId, 1)
        XCTAssertEqual(resolved?.didClamp, true)
    }

    // MARK: - Default origin

    func testDefaultOriginIsBottomRightOfFirstScreen() {
        let geo = PetWindowGeometry(screens: singleScreen(), petSize: petSize, edgeMargin: margin)
        let resolved = geo.defaultOrigin()
        XCTAssertEqual(resolved?.origin.x, 1440 - margin - petSize.width)
        XCTAssertEqual(resolved?.origin.y, margin)
    }

    func testEmptyScreensReturnsNil() {
        let geo = PetWindowGeometry(screens: [], petSize: petSize, edgeMargin: margin)
        XCTAssertNil(geo.clamp(requested: .zero))
        XCTAssertNil(geo.defaultOrigin())
    }
}
