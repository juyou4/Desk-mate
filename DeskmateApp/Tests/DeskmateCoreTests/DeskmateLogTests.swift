import XCTest
@testable import DeskmateCore

final class DeskmateLogTests: XCTestCase {
    func testWithTraceIdRestoresPreviousValue() {
        DeskmateLog.withTraceId("outer") {
            XCTAssertEqual(DeskmateLog.traceId, "outer")
            DeskmateLog.withTraceId("inner") {
                XCTAssertEqual(DeskmateLog.traceId, "inner")
            }
            XCTAssertEqual(DeskmateLog.traceId, "outer")
        }
        XCTAssertNil(DeskmateLog.traceId)
    }

    func testTraceIdPropagatesAcrossAsyncTasks() async throws {
        actor Seen {
            var values: [String: String?] = [:]
            func record(_ key: String, _ value: String?) { values[key] = value }
        }
        let seen = Seen()

        await DeskmateLog.withTraceId("parent-trace") {
            await withTaskGroup(of: Void.self) { group in
                group.addTask {
                    await seen.record("a", DeskmateLog.traceId)
                }
                group.addTask {
                    await seen.record("b", DeskmateLog.traceId)
                }
            }
        }

        let values = await seen.values
        XCTAssertEqual(values["a"], "parent-trace")
        XCTAssertEqual(values["b"], "parent-trace")
        XCTAssertNil(DeskmateLog.traceId)
    }
}
