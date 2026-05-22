import Foundation

/// Observable class wrapper around the value-typed :class:`BubbleQueue`
/// (V10 Phase 7d / I3).
///
/// The pure :class:`BubbleQueue` is ideal for reducer tests but useless
/// as a shared model because mutating copies only update the caller's
/// copy. ``LiveBubbleQueue`` owns one canonical ``BubbleQueue``, routes
/// intents into it, and fans change notifications out to subscribers
/// (pet view, badge count, etc.).
///
/// The wrapper deliberately exposes a narrow surface: ``enqueue`` /
/// ``dismiss`` / ``dequeue`` / ``peek`` / ``prune``. Everything else —
/// ordering, TTL, overflow eviction — remains in :class:`BubbleQueue`.
public final class LiveBubbleQueue {
    public typealias Clock = () -> Int

    private var queue: BubbleQueue
    private let clock: Clock
    private var subscribers: [UUID: (BubbleQueue) -> Void] = [:]

    public init(
        maxActive: Int = 3,
        clock: @escaping Clock = LiveBubbleQueue.defaultClock
    ) {
        self.queue = BubbleQueue(maxActive: maxActive)
        self.clock = clock
    }

    // MARK: - Reads

    /// Snapshot of the underlying queue. Value type → caller gets a copy.
    public var current: BubbleQueue { queue }
    public var count: Int { queue.count }
    public var isEmpty: Bool { queue.isEmpty }

    public func peek() -> BubbleSpec? {
        queue.peek(nowMs: clock())
    }

    // MARK: - Mutations

    public func enqueue(_ spec: BubbleSpec) {
        queue.enqueue(spec, nowMs: clock())
        notify()
    }

    /// Drop any queued bubble whose ``id`` matches. Silently ignores
    /// misses — a dismiss for a bubble that was already shown + dequeued
    /// is a no-op, not an error.
    public func dismiss(id: String) {
        let before = queue.count
        queue.remove(id: id)
        if queue.count != before { notify() }
    }

    /// Replace the text of the bubble whose ``id`` matches. Returns
    /// ``true`` when a live entry was patched, ``false`` when no
    /// matching entry exists (caller may decide to enqueue a fresh
    /// bubble in that case). Used by V10 L3-B1 streaming chat.
    @discardableResult
    public func update(
        id: String,
        text: String,
        markdown: String? = nil,
        refreshTtl: Bool = false
    ) -> Bool {
        let patched = queue.update(
            id: id,
            text: text,
            markdown: markdown,
            nowMs: clock(),
            refreshTtl: refreshTtl
        )
        if patched { notify() }
        return patched
    }

    @discardableResult
    public func dequeue() -> BubbleSpec? {
        guard let spec = queue.dequeue(nowMs: clock()) else { return nil }
        notify()
        return spec
    }

    public func prune() {
        let before = queue.count
        queue.prune(nowMs: clock())
        if queue.count != before { notify() }
    }

    public func clear() {
        guard !queue.isEmpty else { return }
        queue.clear()
        notify()
    }

    // MARK: - Subscription

    @discardableResult
    public func subscribe(
        _ cb: @escaping (BubbleQueue) -> Void
    ) -> () -> Void {
        let id = UUID()
        subscribers[id] = cb
        return { [weak self] in
            self?.subscribers.removeValue(forKey: id)
        }
    }

    public var subscriberCount: Int { subscribers.count }

    // MARK: - Internals

    private func notify() {
        for cb in subscribers.values { cb(queue) }
    }

    public static func defaultClock() -> Int {
        Int(Date().timeIntervalSince1970 * 1000)
    }
}
