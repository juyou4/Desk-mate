import XCTest
@testable import DeskmateCore

final class IslandGeometryTests: XCTestCase {
    private let screen = CGRect(x: 0, y: 0, width: 1512, height: 982)

    func testCompactRectHugsNotchWhenPresent() {
        let notch = CGSize(width: 200, height: 32)
        let g = IslandGeometry(screenFrame: screen, notchSize: notch, topInset: 0)
        let r = g.compactRect()
        XCTAssertEqual(r.width, notch.width)
        XCTAssertEqual(r.height, notch.height)
        XCTAssertEqual(r.midX, screen.midX)
        XCTAssertEqual(r.maxY, screen.maxY)  // top edge flush with screen top
    }

    func testCompactRectFallsBackForNotchlessMachines() {
        let fallback = CGSize(width: 180, height: 28)
        let g = IslandGeometry(
            screenFrame: screen,
            notchSize: nil,
            compactFallbackSize: fallback
        )
        let r = g.compactRect()
        XCTAssertEqual(r.size, fallback)
        XCTAssertEqual(r.midX, screen.midX)
    }

    func testExpandedRectCentresHorizontally() {
        let g = IslandGeometry(screenFrame: screen)
        let target = CGSize(width: 380, height: 90)
        let r = g.expandedRect(size: target)
        XCTAssertEqual(r.size, target)
        XCTAssertEqual(r.midX, screen.midX)
        XCTAssertEqual(r.maxY, screen.maxY)
    }

    func testInterpolatedRectIsCompactAtZeroProgress() {
        let g = IslandGeometry(screenFrame: screen, notchSize: CGSize(width: 200, height: 32))
        let compact = g.compactRect()
        let i = g.interpolatedRect(to: CGSize(width: 400, height: 120), progress: 0)
        XCTAssertEqual(i.size.width, compact.width, accuracy: 0.01)
        XCTAssertEqual(i.size.height, compact.height, accuracy: 0.01)
    }

    func testInterpolatedRectIsExpandedAtFullProgress() {
        let g = IslandGeometry(screenFrame: screen)
        let target = CGSize(width: 400, height: 120)
        let expanded = g.expandedRect(size: target)
        let i = g.interpolatedRect(to: target, progress: 1)
        XCTAssertEqual(i.size, expanded.size)
    }

    func testInterpolatedRectClampsProgressOutsideUnit() {
        let g = IslandGeometry(screenFrame: screen)
        let target = CGSize(width: 400, height: 120)
        let expanded = g.expandedRect(size: target)
        let tooHigh = g.interpolatedRect(to: target, progress: 5)
        XCTAssertEqual(tooHigh.size, expanded.size)

        let compact = g.compactRect()
        let tooLow = g.interpolatedRect(to: target, progress: -1)
        XCTAssertEqual(tooLow.size.width, compact.width, accuracy: 0.01)
    }

    func testCornerRadiusCapsAtHalfShorterEdge() {
        let g = IslandGeometry(
            screenFrame: screen,
            compactCornerRadius: 30,
            expandedCornerRadius: 30
        )
        // A 20pt tall pill can only round to 10pt.
        let tiny = CGRect(x: 0, y: 0, width: 200, height: 20)
        XCTAssertEqual(g.cornerRadius(for: tiny, progress: 0.5), 10, accuracy: 0.01)
    }
}
