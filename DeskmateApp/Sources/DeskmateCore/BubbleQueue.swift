import Foundation

/// FIFO-within-priority bubble queue with TTL expiry (V10 I3 / L1-F).
///
/// Intents arrive faster than the pet can show them; the queue smoothens the
/// stream. It's a plain value type so the reducer / tests can pin behaviour
/// without any runtime observers.
///
/// Ordering rules:
///
/// 1. Higher priority first (``P0`` > ``P1`` > ``P2`` > ``P3``).
/// 2. FIFO within equal priority.
/// 3. Entries whose ``ttlMs`` has elapsed are dropped on ``prune`` / ``peek``.
/// 4. ``maxActive`` caps the queue length; on overflow the oldest *lowest*
///    priority entry is evicted first.
public struct BubbleQueue: Equatable, Sendable {
    public struct Entry: Equatable, Sendable {
        public let spec: BubbleSpec
        public let enqueuedAtMs: Int
        public let expiresAtMs: Int?

        fileprivate init(spec: BubbleSpec, enqueuedAtMs: Int) {
            self.spec = spec
            self.enqueuedAtMs = enqueuedAtMs
            if let ttl = spec.ttlMs, ttl > 0 {
                self.expiresAtMs = enqueuedAtMs + ttl
            } else {
                self.expiresAtMs = nil
            }
        }

        fileprivate func isExpired(at nowMs: Int) -> Bool {
            guard let expires = expiresAtMs else { return false }
            return nowMs >= expires
        }
    }

    public var maxActive: Int
    private var entries: [Entry]

    public init(maxActive: Int = 3) {
        precondition(maxActive > 0, "maxActive must be positive")
        self.maxActive = maxActive
        self.entries = []
    }

    public var count: Int { entries.count }
    public var isEmpty: Bool { entries.isEmpty }

    /// Raw entries in insertion order (for tests / diagnostics).
    public var allEntries: [Entry] { entries }

    // MARK: - Mutation

    @discardableResult
    public mutating func enqueue(_ spec: BubbleSpec, nowMs: Int) -> Entry {
        prune(nowMs: nowMs)
        let entry = Entry(spec: spec, enqueuedAtMs: nowMs)
        entries.append(entry)
        if entries.count > maxActive {
            evictLowestPriorityOldest()
        }
        return entry
    }

    /// Remove any entries whose ttl has elapsed.
    public mutating func prune(nowMs: Int) {
        entries.removeAll { $0.isExpired(at: nowMs) }
    }

    /// Return the next bubble to show without mutating the queue.
    public func peek(nowMs: Int) -> BubbleSpec? {
        guard let idx = indexOfNext(nowMs: nowMs) else { return nil }
        return entries[idx].spec
    }

    /// Return + remove the next bubble to show.
    public mutating func dequeue(nowMs: Int) -> BubbleSpec? {
        prune(nowMs: nowMs)
        guard let idx = indexOfNext(nowMs: nowMs) else { return nil }
        return entries.remove(at: idx).spec
    }

    public mutating func remove(id: String) {
        entries.removeAll { $0.spec.id == id }
    }

    public mutating func clear() {
        entries.removeAll()
    }

    // MARK: - Internals

    private func indexOfNext(nowMs: Int) -> Int? {
        // Entries are appended in FIFO order. Choose the lowest priorityRank
        // (``p0 = 0``, ``p1 = 1`` …) and among ties, the earliest insertion.
        var bestIdx: Int? = nil
        var bestRank = Int.max
        var bestTime = Int.max
        for (idx, entry) in entries.enumerated() where !entry.isExpired(at: nowMs) {
            let rank = priorityRank(entry.spec.priority)
            if rank < bestRank || (rank == bestRank && entry.enqueuedAtMs < bestTime) {
                bestIdx = idx
                bestRank = rank
                bestTime = entry.enqueuedAtMs
            }
        }
        return bestIdx
    }

    private mutating func evictLowestPriorityOldest() {
        // Evict the oldest entry at the worst priority rank so important
        // bubbles survive overflow.
        var victimIdx: Int? = nil
        var victimRank = Int.min
        var victimTime = Int.max
        for (idx, entry) in entries.enumerated() {
            let rank = priorityRank(entry.spec.priority)
            if rank > victimRank || (rank == victimRank && entry.enqueuedAtMs < victimTime) {
                victimIdx = idx
                victimRank = rank
                victimTime = entry.enqueuedAtMs
            }
        }
        if let idx = victimIdx { entries.remove(at: idx) }
    }

    private func priorityRank(_ p: Priority) -> Int {
        switch p {
        case .p0: return 0
        case .p1: return 1
        case .p2: return 2
        case .p3: return 3
        }
    }
}
