import Foundation

/// One recorded turn in the menu-bar "recent chat" ribbon (V10
/// Phase 12-iv). The role is whichever side produced the text.
public struct ChatEntry: Identifiable, Equatable, Sendable {
    public let id: UUID
    public let role: Role
    public let text: String
    public let timestampMs: Int

    public enum Role: String, Sendable, Equatable {
        case user
        case pet
    }

    public init(
        id: UUID = UUID(),
        role: Role,
        text: String,
        timestampMs: Int
    ) {
        self.id = id
        self.role = role
        self.text = text
        self.timestampMs = timestampMs
    }
}

/// Ring-ish buffer that records chat turns for the menu-bar history
/// section (V10 Phase 12-iv).
///
/// The buffer holds the last ``maxEntries`` turns in arrival order and
/// deliberately *ignores*:
///
/// - Non-chat bubbles (approvals, reminders, system chatter).
/// - The ``"…"`` placeholder emitted at the start of the typewriter
///   sequence — a placeholder isn't content worth logging.
/// - Bubbles with the same id as the previous bubble-recording call,
///   so the re-peek pattern used by the runtime never produces
///   duplicates.
public final class ChatHistoryBuffer {
    public private(set) var entries: [ChatEntry] = []
    private let maxEntries: Int
    private var lastRecordedBubbleId: String?

    public init(maxEntries: Int = 20) {
        self.maxEntries = maxEntries
    }

    /// Record an outgoing user turn (from the menu-bar TextField or
    /// any other Swift-side entry point).
    public func recordUserMessage(_ text: String, at timestampMs: Int) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        entries.append(
            ChatEntry(role: .user, text: trimmed, timestampMs: timestampMs)
        )
        trimToMax()
    }

    /// Record a pet turn sourced from a newly-visible
    /// :class:`BubbleSpec`. Returns ``true`` if the bubble was worth
    /// recording (callers can use this to decide whether to nudge
    /// an ObservableObject).
    @discardableResult
    public func recordBubbleIfChatLike(
        _ bubble: BubbleSpec, at timestampMs: Int
    ) -> Bool {
        guard bubble.kind == .chat else { return false }
        // Dedupe: the runtime re-peeks on every queue event, which
        // emits the same bubble repeatedly until it dismisses.
        guard bubble.id != lastRecordedBubbleId else { return false }
        lastRecordedBubbleId = bubble.id

        let trimmed = bubble.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty || trimmed == "…" {
            return false
        }
        entries.append(
            ChatEntry(role: .pet, text: trimmed, timestampMs: timestampMs)
        )
        trimToMax()
        return true
    }

    /// Wipe all history + dedup state.
    public func clear() {
        entries.removeAll()
        lastRecordedBubbleId = nil
    }

    private func trimToMax() {
        if entries.count > maxEntries {
            entries.removeFirst(entries.count - maxEntries)
        }
    }
}
