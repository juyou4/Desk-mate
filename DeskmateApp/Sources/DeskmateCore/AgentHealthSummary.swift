import Foundation

/// Display-only health rollup for the island/menu diagnostics.
///
/// The Python side already normalizes hook sessions, CLI agents, and GUI IDEs
/// into ``SessionRow``. Keeping the aggregation here avoids adding another wire
/// field just to show users whether the island is seeing hooks or only passive
/// process fallback rows.
public struct AgentHealthSummary: Equatable, Sendable {
    public struct SourceCount: Equatable, Sendable {
        public var label: String
        public var count: Int

        public init(label: String, count: Int) {
            self.label = label
            self.count = count
        }
    }

    public var total: Int
    public var active: Int
    public var hookSessions: Int
    public var cliAgents: Int
    public var guiIDEs: Int
    public var unobserved: Int
    public var awaitingAction: Int
    public var sources: [SourceCount]

    public init(sessions: [SessionRow]) {
        let activeRows = sessions.filter { $0.state != .closed }
        total = sessions.count
        active = activeRows.count
        hookSessions = activeRows.filter { $0.kind == "hook_session" }.count
        cliAgents = activeRows.filter { $0.kind == "cli_agent" }.count
        guiIDEs = activeRows.filter { $0.kind == "gui_ide" }.count
        unobserved = activeRows.filter { $0.phaseSource == "unobserved" }.count
        awaitingAction = activeRows.filter(\.needsUserAction).count

        var counts: [String: Int] = [:]
        for row in activeRows {
            let label = row.sourceLabel ?? Self.normalizedLabel(row.source) ?? "Unknown"
            counts[label, default: 0] += 1
        }
        sources = counts
            .map { SourceCount(label: $0.key, count: $0.value) }
            .sorted {
                if $0.count != $1.count { return $0.count > $1.count }
                return $0.label.localizedCaseInsensitiveCompare($1.label) == .orderedAscending
            }
    }

    public var isEmpty: Bool { active == 0 }

    public var statusLine: String {
        if active == 0 { return "No active IDE or agent sessions" }
        var parts = ["Active \(active)"]
        if awaitingAction > 0 { parts.append("action \(awaitingAction)") }
        if unobserved > 0 { parts.append("unobserved \(unobserved)") }
        return parts.joined(separator: " · ")
    }

    public var kindLine: String {
        let parts = [
            hookSessions > 0 ? "Hook \(hookSessions)" : nil,
            cliAgents > 0 ? "CLI \(cliAgents)" : nil,
            guiIDEs > 0 ? "IDE \(guiIDEs)" : nil,
        ].compactMap { $0 }
        return parts.isEmpty ? "Kinds: none" : "Kinds: " + parts.joined(separator: " · ")
    }

    public var sourceLine: String {
        guard !sources.isEmpty else { return "Sources: none" }
        return "Sources: " + sources
            .prefix(5)
            .map { "\($0.label) \($0.count)" }
            .joined(separator: " · ")
    }

    public var hookLine: String {
        if active == 0 { return "Hooks: idle" }
        if hookSessions > 0 {
            return "Hooks: \(hookSessions) observed / \(active) active"
        }
        if unobserved > 0 {
            return "Hooks: no hook events · \(unobserved) unobserved"
        }
        return "Hooks: passive runtime only"
    }

    public var menuText: String {
        [
            statusLine,
            kindLine,
            sourceLine,
            hookLine,
        ].joined(separator: "\n")
    }

    public var expandedBadgeText: String {
        if active == 0 { return "idle" }
        if awaitingAction > 0 { return "\(awaitingAction) action" }
        if unobserved > 0 { return "\(unobserved) unobserved" }
        if hookSessions > 0 { return "\(hookSessions) hook" }
        return "\(active) live"
    }

    private static func normalizedLabel(_ raw: String?) -> String? {
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty
        else { return nil }
        return raw
            .split(separator: "_")
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: " ")
    }
}
