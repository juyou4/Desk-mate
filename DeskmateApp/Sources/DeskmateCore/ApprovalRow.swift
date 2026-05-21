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
        resolvedAtMs: Int? = nil
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
    }
}
