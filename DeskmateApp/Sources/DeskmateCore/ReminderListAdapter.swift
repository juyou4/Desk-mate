import Foundation

/// Display-model adapter for reminder lists in the menu bar / island
/// (V10 L2-#4).
///
/// Mirrors :class:`SessionListAdapter`: pure function, returns the slice +
/// ordering the UI should render given the raw wire rows. The Python
/// :class:`ReminderStore` stays authoritative.
public struct ReminderListAdapter: Equatable, Sendable {
    public var maxRows: Int
    /// Dismissed / cancelled reminders older than this are hidden. ``nil``
    /// disables the cutoff (useful for history-heavy UI).
    public var hideResolvedAfterMs: Int?

    public init(maxRows: Int = 12, hideResolvedAfterMs: Int? = 30 * 60 * 1000) {
        self.maxRows = maxRows
        self.hideResolvedAfterMs = hideResolvedAfterMs
    }

    /// Return reminders to display. Ordering:
    ///   1. ``pending`` before ``fired`` before terminal states.
    ///   2. Within a group, higher-priority first, then ``dueAtMs`` asc
    ///      (next-to-fire first).
    public func display(
        reminders: [ReminderRow], nowMs: Int
    ) -> [ReminderRow] {
        let filtered = reminders.filter { row in
            guard isResolved(row.status) else { return true }
            guard let cutoff = hideResolvedAfterMs,
                  let resolvedAt = row.resolvedAtMs
            else { return true }
            return nowMs - resolvedAt <= cutoff
        }
        let sorted = filtered.sorted(by: Self.order)
        return Array(sorted.prefix(max(0, maxRows)))
    }

    // MARK: - Ordering

    static func order(_ a: ReminderRow, _ b: ReminderRow) -> Bool {
        let aRank = statusRank(a.status)
        let bRank = statusRank(b.status)
        if aRank != bRank { return aRank < bRank }

        let aPrio = priorityRank(a.priority)
        let bPrio = priorityRank(b.priority)
        if aPrio != bPrio { return aPrio < bPrio }

        return a.dueAtMs < b.dueAtMs
    }

    private static func statusRank(_ s: ReminderRow.Status) -> Int {
        switch s {
        case .pending: return 0
        case .fired: return 1
        case .dismissed: return 2
        case .cancelled: return 3
        case .unknown: return 4
        }
    }

    private static func priorityRank(_ p: Priority) -> Int {
        switch p {
        case .p0: return 0
        case .p1: return 1
        case .p2: return 2
        case .p3: return 3
        }
    }

    private func isResolved(_ s: ReminderRow.Status) -> Bool {
        s == .dismissed || s == .cancelled
    }
}
