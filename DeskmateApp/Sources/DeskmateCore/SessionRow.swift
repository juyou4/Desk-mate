import Foundation

/// Wire-level session record (V10 L1-D / L2-#4).
///
/// Matches the shape pushed by Python inside ``state.snapshot``'s
/// ``active_sessions`` field. Unknown fields are tolerated so a newer agent
/// adding extra keys never crashes an older Swift client.
///
/// V10 L2-#4 ("actionable-first + subagent fold") adds three optional
/// fields to the wire shape; older agents that don't ship them keep
/// decoding because every field has a safe default — phase falls
/// back to ``.running``, parent / subagent kind to ``nil``, which
/// matches the V9 single-list-no-fold behaviour exactly.
public struct SessionRow: Equatable, Sendable, Codable {
    public enum State: String, Equatable, Sendable, Codable {
        case active
        case paused
        case closed
        case unknown

        public init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = State(rawValue: raw) ?? .unknown
        }
    }

    /// V10 L2-#4: orthogonal axis describing what kind of attention a
    /// session needs from the user *right now*.
    public enum Phase: String, Equatable, Sendable, Codable {
        case waitingForApproval = "waiting_for_approval"
        case waitingForAnswer = "waiting_for_answer"
        case thinking
        case editing
        case runningTool = "running_tool"
        case testing
        case running
        case failed
        case completed
        case unknown

        public init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Phase(rawValue: raw) ?? .unknown
        }
    }

    public var sessionId: String
    public var title: String
    public var summary: String
    public var state: State
    public var priority: Priority
    public var createdAtMs: Int
    public var updatedAtMs: Int
    public var closedAtMs: Int?

    /// V10 L2-#4: actionable-first sort key.
    public var phase: Phase
    /// V10 L2-#4: subagent linkage. Top-level sessions leave this nil.
    public var parentSessionId: String?
    /// V10 L2-#4: short tag (``tool_call`` / ``worktree`` / …) for the
    /// fold summary.
    public var subagentKind: String?
    /// Hook-originated working directory used for jump-back fallback.
    public var cwd: String?
    /// Hook-originated URL used for jump-back when allowlisted by Python.
    public var jumpUrl: String?
    /// Runtime source label, e.g. ``codex`` / ``claude_code`` / ``cursor``.
    public var source: String?
    /// Runtime kind label, e.g. ``cli_agent`` / ``gui_ide`` / ``hook_session``.
    public var kind: String?
    /// Passive process id when the row came from runtime scanning.
    public var processId: Int?
    /// Display-safe metadata from Python's session extras. Non-string values
    /// are ignored during decode so future raw payloads remain forward-safe.
    public var extras: [String: String]

    public init(
        sessionId: String,
        title: String = "",
        summary: String = "",
        state: State = .active,
        priority: Priority = .p2,
        createdAtMs: Int = 0,
        updatedAtMs: Int = 0,
        closedAtMs: Int? = nil,
        phase: Phase = .running,
        parentSessionId: String? = nil,
        subagentKind: String? = nil,
        cwd: String? = nil,
        jumpUrl: String? = nil,
        source: String? = nil,
        kind: String? = nil,
        processId: Int? = nil,
        extras: [String: String] = [:]
    ) {
        self.sessionId = sessionId
        self.title = title
        self.summary = summary
        self.state = state
        self.priority = priority
        self.createdAtMs = createdAtMs
        self.updatedAtMs = updatedAtMs
        self.closedAtMs = closedAtMs
        self.phase = phase
        self.parentSessionId = parentSessionId
        self.subagentKind = subagentKind
        self.cwd = cwd
        self.jumpUrl = jumpUrl
        self.source = source
        self.kind = kind
        self.processId = processId
        self.extras = extras
    }

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case title
        case summary
        case state
        case priority
        case createdAtMs = "created_at_ms"
        case updatedAtMs = "updated_at_ms"
        case closedAtMs = "closed_at_ms"
        case phase
        case parentSessionId = "parent_session_id"
        case subagentKind = "subagent_kind"
        case cwd
        case jumpUrl = "jump_url"
        case source
        case kind
        case processId = "process_id"
        case extras
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.sessionId = try c.decode(String.self, forKey: .sessionId)
        self.title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? ""
        self.summary = (try? c.decodeIfPresent(String.self, forKey: .summary)) ?? ""
        self.state =
            (try? c.decodeIfPresent(State.self, forKey: .state)) ?? .active
        self.priority =
            (try? c.decodeIfPresent(Priority.self, forKey: .priority)) ?? .p2
        self.createdAtMs =
            (try? c.decodeIfPresent(Int.self, forKey: .createdAtMs)) ?? 0
        self.updatedAtMs =
            (try? c.decodeIfPresent(Int.self, forKey: .updatedAtMs)) ?? 0
        self.closedAtMs = try? c.decodeIfPresent(Int.self, forKey: .closedAtMs)
        self.phase =
            (try? c.decodeIfPresent(Phase.self, forKey: .phase)) ?? .running
        self.parentSessionId = try? c.decodeIfPresent(
            String.self, forKey: .parentSessionId
        )
        self.subagentKind = try? c.decodeIfPresent(
            String.self, forKey: .subagentKind
        )
        self.cwd = try? c.decodeIfPresent(String.self, forKey: .cwd)
        self.jumpUrl = try? c.decodeIfPresent(String.self, forKey: .jumpUrl)
        self.source = try? c.decodeIfPresent(String.self, forKey: .source)
        self.kind = try? c.decodeIfPresent(String.self, forKey: .kind)
        self.processId = try? c.decodeIfPresent(Int.self, forKey: .processId)
        self.extras = Self.decodeStringExtras(from: c)
    }

    /// V10 L2-#4: convenience predicate for the session-list adapter.
    public var isSubagent: Bool { parentSessionId != nil }

    public var sourceLabel: String? {
        guard let raw = source?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty
        else { return nil }
        switch raw {
        case "codex": return "Codex"
        case "claude_code": return "Claude"
        case "cursor": return "Cursor"
        case "windsurf": return "Windsurf"
        case "vscode": return "VSCode"
        case "xcode": return "Xcode"
        case "jetbrains": return "JetBrains"
        case "opencode": return "OpenCode"
        // V10 polish: extended runtime lineup. Keep these in
        // sync with ``AgentRuntimeSource`` in
        // ``agent/deskmate_agent/agent_runtime.py``. The default
        // branch already PrettyPrints unknown sources, so adding
        // a new Python source without a matching case here
        // produces a reasonable label automatically; we add the
        // explicit cases when the auto-derived form is awkward
        // (e.g. ``Github Desktop`` vs ``GitHub Desktop``).
        case "aider": return "Aider"
        case "gemini": return "Gemini"
        case "kimi": return "Kimi"
        case "qwen": return "Qwen"
        case "factory_droid": return "Factory Droid"
        case "codebuddy": return "CodeBuddy"
        case "qoder": return "Qoder"
        case "zed": return "Zed"
        case "trae": return "Trae"
        case "sublime": return "Sublime"
        case "fleet": return "Fleet"
        case "nova": return "Nova"
        case "neovim": return "Neovim"
        case "github_desktop": return "GitHub Desktop"
        case "warp": return "Warp"
        case "terminal": return "Terminal"
        default:
            return raw
                .split(separator: "_")
                .map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
        }
    }

    public var phaseLabel: String {
        switch phase {
        case .waitingForApproval: return "needs approval"
        case .waitingForAnswer: return "needs answer"
        case .thinking: return "thinking"
        case .editing: return "editing"
        case .runningTool: return "running tool"
        case .testing: return "testing"
        case .running: return "running"
        case .failed: return "failed"
        case .completed: return "completed"
        case .unknown: return state.rawValue
        }
    }

    public var needsUserAction: Bool {
        phase == .waitingForApproval || phase == .waitingForAnswer
    }

    public var hasJumpTarget: Bool {
        let url = jumpUrl?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let path = cwd?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return !url.isEmpty || !path.isEmpty
    }

    public var canAttemptJump: Bool {
        if hasJumpTarget { return true }
        let sourceText = source?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let kindText = kind?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return !sourceText.isEmpty || !kindText.isEmpty || processId != nil
    }

    public var displayTitle: String {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? sessionId : trimmed
    }

    public var toolName: String? {
        cleanExtra("tool_name")
    }

    public var command: String? {
        cleanExtra("command")
    }

    public var filePath: String? {
        cleanExtra("file_path")
    }

    public var activityLine: String {
        let source = sourceLabel ?? "Agent"
        let workspace = workspaceLabel
        let action: String
        if let command {
            action = "cmd: \(shorten(command, max: 72))"
        } else if let filePath {
            action = "file: \(URL(fileURLWithPath: filePath).lastPathComponent)"
        } else if let toolName {
            action = "tool: \(toolName)"
        } else {
            let summaryText = summary.trimmingCharacters(in: .whitespacesAndNewlines)
            action = summaryText.isEmpty ? phaseLabel : summaryText
        }
        if workspace.isEmpty {
            return "\(source) · \(action)"
        }
        return "\(source) · \(workspace) · \(action)"
    }

    public var detailLine: String {
        activityLine
    }

    private var workspaceLabel: String {
        guard let cwd = cwd?.trimmingCharacters(in: .whitespacesAndNewlines),
              !cwd.isEmpty
        else { return "" }
        return URL(fileURLWithPath: cwd).lastPathComponent
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
