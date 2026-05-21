import Foundation

/// Wire-level reminder record (V10 L2-#4).
///
/// Mirrors the payload pushed inside ``state.snapshot``'s
/// ``pending_reminders`` field plus any runtime delta envelopes. Unknown
/// fields survive so older Swift clients never drop reminder data.
public struct ReminderRow: Equatable, Sendable, Codable {
    public enum Status: String, Equatable, Sendable, Codable {
        case pending
        case fired
        case dismissed
        case cancelled
        case unknown

        public init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Status(rawValue: raw) ?? .unknown
        }
    }

    public var reminderId: String
    public var text: String
    public var dueAtMs: Int
    public var createdAtMs: Int
    public var status: Status
    public var priority: Priority
    public var sessionId: String?
    public var bubbleId: String?
    public var firedAtMs: Int?
    public var resolvedAtMs: Int?

    public init(
        reminderId: String,
        text: String = "",
        dueAtMs: Int = 0,
        createdAtMs: Int = 0,
        status: Status = .pending,
        priority: Priority = .p1,
        sessionId: String? = nil,
        bubbleId: String? = nil,
        firedAtMs: Int? = nil,
        resolvedAtMs: Int? = nil
    ) {
        self.reminderId = reminderId
        self.text = text
        self.dueAtMs = dueAtMs
        self.createdAtMs = createdAtMs
        self.status = status
        self.priority = priority
        self.sessionId = sessionId
        self.bubbleId = bubbleId
        self.firedAtMs = firedAtMs
        self.resolvedAtMs = resolvedAtMs
    }

    enum CodingKeys: String, CodingKey {
        case reminderId = "reminder_id"
        case text
        case dueAtMs = "due_at_ms"
        case createdAtMs = "created_at_ms"
        case status
        case priority
        case sessionId = "session_id"
        case bubbleId = "bubble_id"
        case firedAtMs = "fired_at_ms"
        case resolvedAtMs = "resolved_at_ms"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.reminderId = try c.decode(String.self, forKey: .reminderId)
        self.text = (try? c.decodeIfPresent(String.self, forKey: .text)) ?? ""
        self.dueAtMs = (try? c.decodeIfPresent(Int.self, forKey: .dueAtMs)) ?? 0
        self.createdAtMs =
            (try? c.decodeIfPresent(Int.self, forKey: .createdAtMs)) ?? 0
        self.status =
            (try? c.decodeIfPresent(Status.self, forKey: .status)) ?? .pending
        self.priority =
            (try? c.decodeIfPresent(Priority.self, forKey: .priority)) ?? .p1
        self.sessionId = try? c.decodeIfPresent(String.self, forKey: .sessionId)
        self.bubbleId = try? c.decodeIfPresent(String.self, forKey: .bubbleId)
        self.firedAtMs = try? c.decodeIfPresent(Int.self, forKey: .firedAtMs)
        self.resolvedAtMs = try? c.decodeIfPresent(Int.self, forKey: .resolvedAtMs)
    }
}
