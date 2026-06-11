import Foundation

/// Wire-level approval record (V10 Phase 6 / L1-F).
///
/// Matches the Python :class:`Approval` payload pushed inside
/// ``state.snapshot``'s ``pending_approvals_detail`` field. Unknown
/// fields survive so older Swift clients never drop data.
public struct ApprovalRow: Equatable, Sendable, Codable {
    public enum Status: String, Equatable, Sendable, Codable {
        case pending
        case resolved
        case expired
        case cancelled
        case unknown

        public init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Status(rawValue: raw) ?? .unknown
        }
    }

    public enum Decision: String, Equatable, Sendable, Codable {
        case none
        case allow
        case deny
        case unknown

        public init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Decision(rawValue: raw) ?? .unknown
        }
    }

    public var approvalId: String
    public var prompt: String
    public var status: Status
    public var decision: Decision
    public var priority: Priority
    public var sessionId: String?
    public var bubbleId: String?
    public var createdAtMs: Int
    public var expiresAtMs: Int?
    public var resolvedAtMs: Int?
    /// Display-safe metadata from Python's approval extras. Non-string values
    /// are ignored during decode so future raw payloads remain forward-safe.
    public var extras: [String: String]

    public init(
        approvalId: String,
        prompt: String = "",
        status: Status = .pending,
        decision: Decision = .none,
        priority: Priority = .p1,
        sessionId: String? = nil,
        bubbleId: String? = nil,
        createdAtMs: Int = 0,
        expiresAtMs: Int? = nil,
        resolvedAtMs: Int? = nil,
        extras: [String: String] = [:]
    ) {
        self.approvalId = approvalId
        self.prompt = prompt
        self.status = status
        self.decision = decision
        self.priority = priority
        self.sessionId = sessionId
        self.bubbleId = bubbleId
        self.createdAtMs = createdAtMs
        self.expiresAtMs = expiresAtMs
        self.resolvedAtMs = resolvedAtMs
        self.extras = extras
    }

    enum CodingKeys: String, CodingKey {
        case approvalId = "approval_id"
        case prompt
        case status
        case decision
        case priority
        case sessionId = "session_id"
        case bubbleId = "bubble_id"
        case createdAtMs = "created_at_ms"
        case expiresAtMs = "expires_at_ms"
        case resolvedAtMs = "resolved_at_ms"
        case extras
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.approvalId = try c.decode(String.self, forKey: .approvalId)
        self.prompt = (try? c.decodeIfPresent(String.self, forKey: .prompt)) ?? ""
        self.status =
            (try? c.decodeIfPresent(Status.self, forKey: .status)) ?? .pending
        self.decision =
            (try? c.decodeIfPresent(Decision.self, forKey: .decision)) ?? .none
        self.priority =
            (try? c.decodeIfPresent(Priority.self, forKey: .priority)) ?? .p1
        self.sessionId = try? c.decodeIfPresent(String.self, forKey: .sessionId)
        self.bubbleId = try? c.decodeIfPresent(String.self, forKey: .bubbleId)
        self.createdAtMs =
            (try? c.decodeIfPresent(Int.self, forKey: .createdAtMs)) ?? 0
        self.expiresAtMs = try? c.decodeIfPresent(Int.self, forKey: .expiresAtMs)
        self.resolvedAtMs = try? c.decodeIfPresent(Int.self, forKey: .resolvedAtMs)
        self.extras = Self.decodeStringExtras(from: c)
    }

    public var approvalKind: String? {
        cleanExtra("kind") ?? cleanExtra("source")
    }

    public var toolName: String? {
        cleanExtra("tool_name")
    }

    public var toolAction: String? {
        cleanExtra("tool_action")
            ?? cleanExtra("action_kind")
            ?? toolName
    }

    public var toolTarget: String? {
        cleanExtra("tool_target")
            ?? cleanExtra("target")
            ?? filePath
    }

    public var toolSummary: String? {
        cleanExtra("tool_summary")
    }

    public var approvalPreview: String? {
        cleanExtra("approval_preview")
    }

    public var command: String? {
        cleanExtra("command")
    }

    public var filePath: String? {
        cleanExtra("file_path")
    }

    public var riskLevel: String? {
        cleanExtra("risk_level") ?? cleanExtra("risk")
    }

    public var riskSummary: String? {
        cleanExtra("risk_summary")
            ?? cleanExtra("memory_reason")
            ?? cleanExtra("task_reason")
    }

    public var detailLine: String? {
        if let approvalPreview {
            return shorten(approvalPreview, max: 96)
        }
        if let toolSummary {
            return shorten(toolSummary, max: 96)
        }
        if let command {
            return "cmd: \(shorten(command, max: 88))"
        }
        if let filePath {
            return "file: \(URL(fileURLWithPath: filePath).lastPathComponent)"
        }
        if let toolAction {
            if let toolTarget {
                return "tool: \(toolAction) -> \(shorten(toolTarget, max: 64))"
            }
            return "tool: \(toolAction)"
        }
        if let riskSummary {
            if let riskLevel {
                return "\(riskLevel): \(shorten(riskSummary, max: 80))"
            }
            return shorten(riskSummary, max: 92)
        }
        return nil
    }

    private func cleanExtra(_ key: String) -> String? {
        guard let raw = extras[key]?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty
        else { return nil }
        return raw
    }

    private func shorten(_ value: String, max: Int) -> String {
        guard value.count > max else { return value }
        return String(value.prefix(max - 3)) + "..."
    }

    private static func decodeStringExtras(
        from c: KeyedDecodingContainer<CodingKeys>
    ) -> [String: String] {
        guard let raw = try? c.decodeIfPresent(
            [String: AnyJSONValue].self, forKey: .extras
        ) else { return [:] }
        var out: [String: String] = [:]
        for (key, value) in raw {
            if case .string(let text) = value {
                out[key] = text
            }
        }
        return out
    }
}
