import AppKit
import Combine
import Foundation
import SwiftUI
import DeskmateCore

/// V11 architecture polish (MioIsland-style ViewModel layer): single
/// source of truth between `DeskmateMenuBarRuntime` (which speaks 10+
/// `@Published` fields) and `IslandOverlay` (which only needs to
/// know "what status are we in" and "what should the trailing module
/// show right now").
///
/// The runtime keeps owning state. This view-model:
///   1. Subscribes to the runtime's `@Published` fields it cares
///      about and republishes only `status` + `content`.
///   2. Provides explicit action verbs (`open`, `close`, `tap`)
///      instead of leaking three different runtime methods to the
///      view layer.
///
/// SwiftUI views observe a single `@ObservedObject` instead of six.
@MainActor
final class IslandViewModel: ObservableObject {
    @Published private(set) var status: IslandStatus = .closed
    @Published private(set) var content: IslandContent = .idle

    /// Time the current `content` value was last entered, in ms since
    /// epoch. Lets the UI compute "how long has this approval been
    /// waiting" without snooping into the underlying SessionRow.
    @Published private(set) var contentEnteredAtMs: Int = 0

    let runtime: DeskmateMenuBarRuntime
    private var cancellables = Set<AnyCancellable>()

    init(runtime: DeskmateMenuBarRuntime) {
        self.runtime = runtime
        installSubscriptions()
        // Seed the derived state once so the view doesn't show the
        // default `.idle / .closed` for one tick at launch.
        recompute()
    }

    private func installSubscriptions() {
        // We collapse all five upstream signals into one callback —
        // recompute reads the latest snapshot of every field, so
        // ordering between Combine emissions doesn't matter.
        let recomputeSink: (Any) -> Void = { [weak self] _ in
            self?.recompute()
        }
        runtime.$island.sink(receiveValue: recomputeSink).store(in: &cancellables)
        runtime.$sessions.sink(receiveValue: recomputeSink).store(in: &cancellables)
        runtime.$approvals.sink(receiveValue: recomputeSink).store(in: &cancellables)
        runtime.$bridgeState
            .map { _ in () as Any }
            .sink(receiveValue: recomputeSink)
            .store(in: &cancellables)
        runtime.$domain
            .map { _ in () as Any }
            .sink(receiveValue: recomputeSink)
            .store(in: &cancellables)
    }

    // MARK: - Derived state

    private func recompute() {
        let newStatus: IslandStatus = runtime.isIslandExpanded ? .opened : .closed
        let newContent = computeContent()
        if newStatus != status {
            status = newStatus
        }
        if newContent != content {
            content = newContent
            contentEnteredAtMs = Int(Date().timeIntervalSince1970 * 1000)
        }
    }

    private func computeContent() -> IslandContent {
        IslandContentProjection.compute(
            islandState: runtime.island?.state,
            sessions: runtime.sessions,
            approvals: runtime.approvals
        )
    }

    // MARK: - Action verbs (taken from MioIsland's notchOpen / Close)

    /// Tap on the compact pill — toggle expanded session list.
    func tap() {
        runtime.handleIslandHover(.tap(tsMs: nowMs()))
    }

    /// Programmatic open (e.g. from swipe-down or notification arrival).
    func open() {
        runtime.openIslandSessionList()
    }

    /// Programmatic close (e.g. from swipe-up or close button).
    func close() {
        runtime.closeIslandSessionList(source: .island)
    }

    /// Resolve an approval (Allow / Deny). Convenience pass-through.
    func resolveApproval(_ approval: ApprovalRow, allow: Bool) {
        runtime.resolveApproval(id: approval.approvalId, allow: allow, source: .island)
    }

    /// Answer a question session. Convenience pass-through.
    func answerQuestion(sessionId: String, answer: String) {
        runtime.answerQuestion(sessionId: sessionId, answer: answer, source: .island)
    }

    /// Jump to a session in its host editor / terminal.
    func jumpToSession(_ sessionId: String) {
        runtime.jumpToSession(sessionId, source: .island)
    }

    private func nowMs() -> Int {
        Int(Date().timeIntervalSince1970 * 1000)
    }
}
