import XCTest
@testable import DeskmateCore

final class TaskRowTests: XCTestCase {
    private let decoder = JSONDecoder()

    func testDecodeActiveTaskWithCurrentStep() throws {
        let raw = #"""
        {
          "task_id": "task-1",
          "conversation_id": "default",
          "title": "Polish task lane",
          "status": "in_progress",
          "notes": "Keep island compact.",
          "created_at_ms": 1000,
          "updated_at_ms": 2000,
          "completed_step_count": 3,
          "total_step_count": 7,
          "current_step": {
            "step_id": "step-2",
            "position": 2,
            "content": "Expose task snapshot",
            "status": "in_progress",
            "active_form": "Exposing task snapshot"
          },
          "steps": [
            {
              "step_id": "step-1",
              "task_id": "task-1",
              "conversation_id": "default",
              "position": 1,
              "content": "Read references",
              "status": "completed"
            },
            {
              "step_id": "step-2",
              "task_id": "task-1",
              "conversation_id": "default",
              "position": 2,
              "content": "Expose task snapshot",
              "status": "in_progress",
              "active_form": "Exposing task snapshot"
            }
          ]
        }
        """#.data(using: .utf8)!
        let row = try decoder.decode(TaskRow.self, from: raw)
        XCTAssertEqual(row.taskId, "task-1")
        XCTAssertEqual(row.status, .inProgress)
        XCTAssertEqual(row.displayTitle, "Polish task lane")
        XCTAssertEqual(row.statusLabel, "in progress")
        XCTAssertEqual(row.currentStep?.status, .inProgress)
        XCTAssertEqual(row.currentStepLine, "step: Exposing task snapshot")
        XCTAssertEqual(row.steps.first?.status, .completed)
        XCTAssertEqual(row.completedStepCount, 3)
        XCTAssertEqual(row.totalStepCount, 7)
        XCTAssertEqual(row.stepProgressLabel, "3/7 steps")
        XCTAssertEqual(row.stepProgressLine, "progress: 3/7 steps")
    }

    func testDecodeDefaultsAndUnknownStatus() throws {
        let raw = #"""
        { "task_id": "task-2", "status": "blocked" }
        """#.data(using: .utf8)!
        let row = try decoder.decode(TaskRow.self, from: raw)
        XCTAssertEqual(row.conversationId, "default")
        XCTAssertEqual(row.title, "")
        XCTAssertEqual(row.displayTitle, "task-2")
        XCTAssertEqual(row.status, .unknown)
        XCTAssertEqual(row.steps, [])
        XCTAssertNil(row.currentStepLine)
        XCTAssertEqual(row.completedStepCount, 0)
        XCTAssertEqual(row.totalStepCount, 0)
        XCTAssertNil(row.stepProgressLabel)
        XCTAssertNil(row.stepProgressLine)
    }
}
