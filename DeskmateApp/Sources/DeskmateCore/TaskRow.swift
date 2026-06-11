import Foundation

/// Wire-level durable user task record.
///
/// Mirrors ``state.snapshot.active_tasks`` from Python. These are
/// user-visible Deskmate tasks, not one-turn LLM tool-call tasks.
public struct TaskRow: Equatable, Sendable, Codable {
    public enum Status: String, Equatable, Sendable, Codable {
        case open
        case inProgress = "in_progress"
        case done
        case cancelled
        case unknown

        public init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Status(rawValue: raw) ?? .unknown
        }
    }

    public struct Step: Equatable, Sendable, Codable {
        public enum Status: String, Equatable, Sendable, Codable {
            case pending
            case inProgress = "in_progress"
            case completed
            case unknown

            public init(from decoder: Decoder) throws {
                let raw = try decoder.singleValueContainer().decode(String.self)
                self = Status(rawValue: raw) ?? .unknown
            }
        }

        public var stepId: String
        public var taskId: String
        public var conversationId: String
        public var position: Int
        public var content: String
        public var status: Status
        public var activeForm: String
        public var createdAtMs: Int
        public var updatedAtMs: Int
        public var completedAtMs: Int?

        public init(
            stepId: String,
            taskId: String = "",
            conversationId: String = "default",
            position: Int = 0,
            content: String = "",
            status: Status = .pending,
            activeForm: String = "",
            createdAtMs: Int = 0,
            updatedAtMs: Int = 0,
            completedAtMs: Int? = nil
        ) {
            self.stepId = stepId
            self.taskId = taskId
            self.conversationId = conversationId
            self.position = position
            self.content = content
            self.status = status
            self.activeForm = activeForm
            self.createdAtMs = createdAtMs
            self.updatedAtMs = updatedAtMs
            self.completedAtMs = completedAtMs
        }

        enum CodingKeys: String, CodingKey {
            case stepId = "step_id"
            case taskId = "task_id"
            case conversationId = "conversation_id"
            case position
            case content
            case status
            case activeForm = "active_form"
            case createdAtMs = "created_at_ms"
            case updatedAtMs = "updated_at_ms"
            case completedAtMs = "completed_at_ms"
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            self.stepId =
                (try? c.decodeIfPresent(String.self, forKey: .stepId)) ?? ""
            self.taskId =
                (try? c.decodeIfPresent(String.self, forKey: .taskId)) ?? ""
            self.conversationId =
                (try? c.decodeIfPresent(String.self, forKey: .conversationId))
                ?? "default"
            self.position =
                (try? c.decodeIfPresent(Int.self, forKey: .position)) ?? 0
            self.content =
                (try? c.decodeIfPresent(String.self, forKey: .content)) ?? ""
            self.status =
                (try? c.decodeIfPresent(Status.self, forKey: .status)) ?? .pending
            self.activeForm =
                (try? c.decodeIfPresent(String.self, forKey: .activeForm)) ?? ""
            self.createdAtMs =
                (try? c.decodeIfPresent(Int.self, forKey: .createdAtMs)) ?? 0
            self.updatedAtMs =
                (try? c.decodeIfPresent(Int.self, forKey: .updatedAtMs)) ?? 0
            self.completedAtMs = try? c.decodeIfPresent(
                Int.self, forKey: .completedAtMs
            )
        }

        public var displayText: String {
            if status == .inProgress {
                let active = activeForm.trimmingCharacters(in: .whitespacesAndNewlines)
                if !active.isEmpty { return active }
            }
            return content.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    public var taskId: String
    public var conversationId: String
    public var title: String
    public var status: Status
    public var notes: String
    public var createdAtMs: Int
    public var updatedAtMs: Int
    public var completedAtMs: Int?
    public var currentStep: Step?
    public var steps: [Step]
    private var completedStepCountWire: Int?
    private var totalStepCountWire: Int?

    public init(
        taskId: String,
        conversationId: String = "default",
        title: String = "",
        status: Status = .open,
        notes: String = "",
        createdAtMs: Int = 0,
        updatedAtMs: Int = 0,
        completedAtMs: Int? = nil,
        currentStep: Step? = nil,
        steps: [Step] = [],
        completedStepCountWire: Int? = nil,
        totalStepCountWire: Int? = nil
    ) {
        self.taskId = taskId
        self.conversationId = conversationId
        self.title = title
        self.status = status
        self.notes = notes
        self.createdAtMs = createdAtMs
        self.updatedAtMs = updatedAtMs
        self.completedAtMs = completedAtMs
        self.currentStep = currentStep
        self.steps = steps
        self.completedStepCountWire = completedStepCountWire
        self.totalStepCountWire = totalStepCountWire
    }

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
        case conversationId = "conversation_id"
        case title
        case status
        case notes
        case createdAtMs = "created_at_ms"
        case updatedAtMs = "updated_at_ms"
        case completedAtMs = "completed_at_ms"
        case currentStep = "current_step"
        case steps
        case completedStepCountWire = "completed_step_count"
        case totalStepCountWire = "total_step_count"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.taskId = try c.decode(String.self, forKey: .taskId)
        self.conversationId =
            (try? c.decodeIfPresent(String.self, forKey: .conversationId))
            ?? "default"
        self.title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? ""
        self.status =
            (try? c.decodeIfPresent(Status.self, forKey: .status)) ?? .open
        self.notes = (try? c.decodeIfPresent(String.self, forKey: .notes)) ?? ""
        self.createdAtMs =
            (try? c.decodeIfPresent(Int.self, forKey: .createdAtMs)) ?? 0
        self.updatedAtMs =
            (try? c.decodeIfPresent(Int.self, forKey: .updatedAtMs)) ?? 0
        self.completedAtMs = try? c.decodeIfPresent(
            Int.self, forKey: .completedAtMs
        )
        self.currentStep = try? c.decodeIfPresent(Step.self, forKey: .currentStep)
        self.steps = (try? c.decodeIfPresent([Step].self, forKey: .steps)) ?? []
        self.completedStepCountWire = try? c.decodeIfPresent(
            Int.self, forKey: .completedStepCountWire
        )
        self.totalStepCountWire = try? c.decodeIfPresent(
            Int.self, forKey: .totalStepCountWire
        )
    }

    public var displayTitle: String {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? taskId : trimmed
    }

    public var statusLabel: String {
        switch status {
        case .open: return "open"
        case .inProgress: return "in progress"
        case .done: return "done"
        case .cancelled: return "cancelled"
        case .unknown: return "unknown"
        }
    }

    public var currentStepLine: String? {
        guard let step = currentStep else { return nil }
        let text = step.displayText
        return text.isEmpty ? nil : "step: \(text)"
    }

    public var completedStepCount: Int {
        if let value = completedStepCountWire {
            return max(0, value)
        }
        return steps.filter { $0.status == .completed }.count
    }

    public var totalStepCount: Int {
        if let value = totalStepCountWire {
            return max(0, value)
        }
        return steps.count
    }

    public var stepProgressLabel: String? {
        let total = totalStepCount
        guard total > 0 else { return nil }
        let completed = min(completedStepCount, total)
        return "\(completed)/\(total) steps"
    }

    public var stepProgressLine: String? {
        guard let label = stepProgressLabel else { return nil }
        return "progress: \(label)"
    }
}
