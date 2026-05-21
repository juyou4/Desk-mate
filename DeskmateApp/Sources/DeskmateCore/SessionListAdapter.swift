import Foundation

/// Display-model adapter for the Island ``session_list`` surface
/// (V10 L2-#8 / L2-#4).
///
/// Pure function: given a raw ``[SessionRow]`` + current wall clock,
/// returns the slice + ordering the island should actually render. The
/// Python :class:`SessionStore` is authoritative; this adapter only
/// decides how much of it to show and in what order.
///
/// V10 L2-#4 ("actionable-first + subagent fold") sharpens two things:
///
/// - Sort by ``phase`` first so ``waitingForApproval`` /
///   ``waitingForAnswer`` rows always sit above plain ``running`` /
///   ``completed`` ones, no matter how recently the latter were
///   touched.
/// - Subagent rows (``parentSessionId != nil``) get hidden from the
///   primary list and rolled up into a per-parent
///   :class:`SessionListFoldEntry`, mirroring the Python
///   :class:`deskmate_agent.sessions.SessionListItem` shape.
public struct SessionListAdapter: Equatable, Sendable {
    public var maxRows: Int
    /// Closed sessions older than ``showClosedAfterMs`` milliseconds are
    /// hidden from the list. Nil disables the TTL.
    public var showClosedAfterMs: Int?
    /// V10 L2-#4: cap on the number of subagent labels each fold
    /// entry surfaces. The badge always shows the full count.
    public var maxSummariesPerParent: Int

    public init(
        maxRows: Int = 8,
        showClosedAfterMs: Int? = 60 * 60 * 1000,
        maxSummariesPerParent: Int = 3
    ) {
        self.maxRows = maxRows
        self.showClosedAfterMs = showClosedAfterMs
        self.maxSummariesPerParent = maxSummariesPerParent
    }

    /// Return the rows to display. Ordering:
    ///   1. Phase rank — ``waitingForApproval`` < ``waitingForAnswer`` <
    ///      ``running`` < ``completed`` (V10 L2-#4 actionable-first).
    ///   2. Non-closed before closed.
    ///   3. Priority rank — P0 first (preserves the L2-#8 contract).
    ///   4. ``updatedAtMs`` descending (newest first).
    ///
    /// Subagents (``parentSessionId != nil``) are excluded from the
    /// returned array. Use :meth:`displayWithFold` to get them rolled
    /// up under their parent.
    public func display(sessions: [SessionRow], nowMs: Int) -> [SessionRow] {
        let filtered = sessions
            .filter { !$0.isSubagent }
            .filter { row in
                guard row.state == .closed else { return true }
                guard let ttl = showClosedAfterMs, let closed = row.closedAtMs
                else { return true }
                return nowMs - closed <= ttl
            }

        let sorted = filtered.sorted(by: Self.order)
        return Array(sorted.prefix(max(0, maxRows)))
    }

    /// Top-level rows paired with a per-parent subagent fold.
    ///
    /// Mirrors the Python :class:`SessionListItem` contract: each
    /// entry carries the parent row, the live subagent count, and at
    /// most ``maxSummariesPerParent`` short labels (title-or-kind-or-id).
    public func displayWithFold(
        sessions: [SessionRow], nowMs: Int
    ) -> [SessionListFoldEntry] {
        let tops = display(sessions: sessions, nowMs: nowMs)
        // Bucket subagents by parent once so the per-parent loop is
        // O(N) regardless of how many parents we render.
        var subsByParent: [String: [SessionRow]] = [:]
        for row in sessions where row.isSubagent {
            guard let pid = row.parentSessionId else { continue }
            subsByParent[pid, default: []].append(row)
        }
        for key in subsByParent.keys {
            subsByParent[key]?.sort(by: Self.order)
        }

        return tops.map { top in
            let kids = subsByParent[top.sessionId] ?? []
            let cap = max(0, maxSummariesPerParent)
            let summaries = kids.prefix(cap).map { kid -> String in
                let trimmed = kid.title.trimmingCharacters(
                    in: .whitespacesAndNewlines
                )
                if !trimmed.isEmpty { return trimmed }
                if let k = kid.subagentKind, !k.isEmpty { return k }
                return kid.sessionId
            }
            return SessionListFoldEntry(
                row: top,
                subagentCount: kids.count,
                subagentSummaries: summaries
            )
        }
    }

    // MARK: - Ordering

    static func order(_ a: SessionRow, _ b: SessionRow) -> Bool {
        // V10 L2-#4 inserts phase at the head of the sort key. The
        // remaining tiers preserve the L2-#8 contract: closed last,
        // higher priority first, then most-recently-updated first.
        let aPhase = phaseRank(a.phase)
        let bPhase = phaseRank(b.phase)
        if aPhase != bPhase { return aPhase < bPhase }
        let aClosed = a.state == .closed ? 1 : 0
        let bClosed = b.state == .closed ? 1 : 0
        if aClosed != bClosed { return aClosed < bClosed }
        let aRank = priorityRank(a.priority)
        let bRank = priorityRank(b.priority)
        if aRank != bRank { return aRank < bRank }
        return a.updatedAtMs > b.updatedAtMs
    }

    private static func phaseRank(_ p: SessionRow.Phase) -> Int {
        switch p {
        case .waitingForApproval: return 0
        case .waitingForAnswer: return 1
        case .failed: return 2
        case .runningTool: return 3
        case .editing: return 4
        case .testing: return 5
        case .thinking: return 6
        case .running: return 7
        case .completed: return 8
        case .unknown: return 9
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
}


/// V10 L2-#4 fold entry returned by ``SessionListAdapter.displayWithFold``.
public struct SessionListFoldEntry: Equatable, Sendable {
    public let row: SessionRow
    public let subagentCount: Int
    public let subagentSummaries: [String]

    public init(
        row: SessionRow,
        subagentCount: Int = 0,
        subagentSummaries: [String] = []
    ) {
        self.row = row
        self.subagentCount = subagentCount
        self.subagentSummaries = subagentSummaries
    }
}
