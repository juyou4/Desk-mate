import XCTest
@testable import DeskmateCore

final class PetDragControllerTests: XCTestCase {
    func testTapBelowThresholdEmitsTap() {
        var c = PetDragController()
        _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
        let out = c.apply(.mouseUp(point: CGPoint(x: 1, y: 1), tsMs: 50))
        XCTAssertEqual(out, .tap(at: CGPoint(x: 1, y: 1)))
        XCTAssertEqual(c.state, .idle)
    }

    func testLongPressThenReleaseIsNotATap() {
        var c = PetDragController(clickThresholdMs: 200)
        _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
        let out = c.apply(.mouseUp(point: .zero, tsMs: 1_000))
        XCTAssertEqual(out, .noop)
        XCTAssertEqual(c.state, .idle)
    }

    func testDragBeginsAfterThresholdDistance() {
        var c = PetDragController(clickThresholdPx: 4)
        _ = c.apply(.mouseDown(point: CGPoint(x: 10, y: 10), tsMs: 0))
        let below = c.apply(.mouseDragged(point: CGPoint(x: 12, y: 10), tsMs: 16))
        XCTAssertEqual(below, .noop)
        let above = c.apply(.mouseDragged(point: CGPoint(x: 20, y: 10), tsMs: 32))
        XCTAssertEqual(above, .beganDrag(from: CGPoint(x: 10, y: 10)))
    }

    func testDragDeltaIsRelativeToLastPoint() {
        var c = PetDragController(clickThresholdPx: 1)
        _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
        _ = c.apply(.mouseDragged(point: CGPoint(x: 5, y: 5), tsMs: 10))  // beganDrag
        let drag = c.apply(.mouseDragged(point: CGPoint(x: 8, y: 7), tsMs: 20))
        XCTAssertEqual(drag, .drag(delta: CGPoint(x: 3, y: 2)))
    }

    func testMouseUpDuringDragEndsDrag() {
        var c = PetDragController(clickThresholdPx: 1)
        _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
        _ = c.apply(.mouseDragged(point: CGPoint(x: 20, y: 0), tsMs: 10))
        let end = c.apply(.mouseUp(point: CGPoint(x: 30, y: 0), tsMs: 20))
        XCTAssertEqual(end, .endedDrag(to: CGPoint(x: 30, y: 0)))
        XCTAssertEqual(c.state, .idle)
    }

    func testCancelledFromAnyStateReturnsToIdle() {
        var c = PetDragController()
        _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
        let out = c.apply(.cancelled)
        XCTAssertEqual(out, .cancelled)
        XCTAssertEqual(c.state, .idle)
    }

    func testDragFromPressDoesNotFireTap() {
        var c = PetDragController(clickThresholdPx: 4, clickThresholdMs: 500)
        _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
        _ = c.apply(.mouseDragged(point: CGPoint(x: 10, y: 0), tsMs: 10))
        let out = c.apply(.mouseUp(point: CGPoint(x: 10, y: 0), tsMs: 20))
        // Drag path ends with endedDrag, not tap.
        XCTAssertEqual(out, .endedDrag(to: CGPoint(x: 10, y: 0)))
    }

    func testStrayEventsInIdleAreAbsorbed() {
        var c = PetDragController()
        XCTAssertEqual(c.apply(.mouseDragged(point: .zero, tsMs: 0)), .noop)
        XCTAssertEqual(c.apply(.mouseUp(point: .zero, tsMs: 0)), .noop)
        XCTAssertEqual(c.state, .idle)
    }
}
