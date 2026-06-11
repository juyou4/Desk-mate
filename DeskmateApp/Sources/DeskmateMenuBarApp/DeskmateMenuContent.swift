import SwiftUI
import DeskmateCore

/// SwiftUI view rendered inside the MenuBarExtra popover. Consumes the
/// shared :class:`DeskmateMenuBarRuntime` and lets the user respond to
/// approvals / jump to sessions / quit (V10 Phase 11d-v).
struct DeskmateMenuContent: View {
    @ObservedObject var runtime: DeskmateMenuBarRuntime
    @State private var messageDraft: String = ""
    @State private var showSettings = false
    @FocusState private var inputFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            Divider()
            messageInput
            Divider()
            approvalsSection
            if !runtime.approvals.isEmpty && (!runtime.tasks.isEmpty || !runtime.sessions.isEmpty || !runtime.reminders.isEmpty) {
                Divider()
            }
            tasksSection
            if !runtime.tasks.isEmpty && (!runtime.sessions.isEmpty || !runtime.reminders.isEmpty) {
                Divider()
            }
            sessionsSection
            if !runtime.sessions.isEmpty && !runtime.reminders.isEmpty {
                Divider()
            }
            remindersSection
            if !runtime.chatHistory.isEmpty {
                Divider()
                chatHistorySection
            }
            if !runtime.domain.codingTodayByIde.isEmpty {
                Divider()
                codingTodaySection
            }
            Divider()
            demoSection
            Divider()
            diagnosticsSection
            Divider()
            footer
        }
        .padding(12)
        .frame(width: 340)
        .onAppear {
            inputFocused = true
        }
        .sheet(isPresented: $showSettings) {
            SettingsSheet(runtime: runtime)
        }
    }

    // MARK: - Sections

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Circle()
                    .fill(bridgeColor)
                    .frame(width: 8, height: 8)
                Text(bridgeLabel).font(.headline)
                Spacer()
                Text("mood: \(runtime.domain.agentMood.rawValue)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            // Phase 15-i: today's cumulative coding time, pulled
            // straight from the domain state the Python side emits.
            // Hidden when zero to keep the header tidy for users
            // who haven't coded yet today.
            if runtime.domain.codingTodayMs > 0 {
                HStack(spacing: 4) {
                    Image(systemName: "laptopcomputer")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text(
                        "Today: "
                        + Self.formatDuration(
                            ms: runtime.domain.codingTodayMs
                        )
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
    }

    /// ``45s`` / ``23m`` / ``1h 5m`` formatter mirroring the Python
    /// side's :meth:`CodingSessionTracker._format_duration_ms`.
    static func formatDuration(ms: Int) -> String {
        let seconds = ms / 1000
        if seconds < 60 { return "\(max(0, seconds))s" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        let rem = minutes % 60
        return rem > 0 ? "\(hours)h \(rem)m" : "\(hours)h"
    }

    private var approvalsSection: some View {
        Group {
            if runtime.approvals.isEmpty {
                EmptyView()
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Approvals (\(runtime.approvals.count))")
                        .font(.subheadline.weight(.semibold))
                    ForEach(runtime.approvals, id: \.approvalId) { a in
                        approvalRow(a)
                    }
                }
            }
        }
    }

    private func approvalRow(_ a: ApprovalRow) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(a.prompt.isEmpty ? a.approvalId : a.prompt)
                .font(.body)
                .lineLimit(2)
            if let detail = a.detailLine {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }
            HStack(spacing: 8) {
                Button("Allow") {
                    runtime.resolveApproval(id: a.approvalId, allow: true)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                Button("Deny") {
                    runtime.resolveApproval(id: a.approvalId, allow: false)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                Spacer()
            }
        }
        .padding(.vertical, 2)
    }

    private var tasksSection: some View {
        Group {
            if runtime.tasks.isEmpty {
                EmptyView()
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Tasks (\(runtime.tasks.count))")
                        .font(.subheadline.weight(.semibold))
                    ForEach(runtime.tasks, id: \.taskId) { task in
                        taskRow(task)
                    }
                }
            }
        }
    }

    private func taskRow(_ task: TaskRow) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Button {
                runtime.openTaskDetail(task.taskId)
            } label: {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(task.displayTitle)
                            .font(.body)
                            .lineLimit(1)
                        Text(task.statusLabel)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    if let line = task.currentStepLine {
                        Text(line)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    if let line = task.stepProgressLine {
                        Text(line)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                    if !task.notes.isEmpty {
                        Text(task.notes)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.plain)
            taskControls(task)
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func taskControls(_ task: TaskRow) -> some View {
        HStack(spacing: 3) {
            if task.status == .open {
                taskControlButton("play.fill", help: "Start") {
                    runtime.startTask(task.taskId)
                }
            } else if task.status == .inProgress {
                taskControlButton("pause.fill", help: "Pause") {
                    runtime.pauseTask(task.taskId)
                }
                taskControlButton("forward.end.fill", help: "Next step") {
                    runtime.advanceTask(task.taskId)
                }
                taskControlButton("checkmark", help: "Complete") {
                    runtime.completeTask(task.taskId)
                }
            }
        }
    }

    private func taskControlButton(
        _ systemName: String,
        help: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 10, weight: .semibold))
                .frame(width: 20, height: 20)
        }
        .buttonStyle(.borderless)
        .controlSize(.mini)
        .help(help)
    }

    private var sessionsSection: some View {
        Group {
            if runtime.sessions.isEmpty {
                EmptyView()
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Sessions (\(runtime.sessions.count))")
                        .font(.subheadline.weight(.semibold))
                    ForEach(runtime.sessions, id: \.sessionId) { s in
                        Button {
                            runtime.jumpToSession(s.sessionId)
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(
                                        s.title.isEmpty
                                            ? s.sessionId : s.title
                                    )
                                    Text(s.activityLine)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                        .truncationMode(.middle)
                                    if let outcome = s.recentOutcomeLine {
                                        Text(outcome)
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                            .truncationMode(.middle)
                                    }
                                }
                                Spacer()
                                Text(s.phaseLabel)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private var remindersSection: some View {
        Group {
            if runtime.reminders.isEmpty {
                EmptyView()
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Reminders (\(runtime.reminders.count))")
                        .font(.subheadline.weight(.semibold))
                    ForEach(runtime.reminders, id: \.reminderId) { r in
                        HStack {
                            Text(r.text.isEmpty ? r.reminderId : r.text)
                                .lineLimit(1)
                            Spacer()
                            Text(dueLabel(for: r))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    /// Phase 15-i+: a compact list of per-IDE coding time for
    /// today. Comes pre-sorted descending by duration (Python side
    /// does the ORDER BY).
    private var codingTodaySection: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label("Today on…", systemImage: "laptopcomputer")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Text(Self.formatDuration(ms: runtime.domain.codingTodayMs))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(runtime.domain.codingTodayByIde.enumerated()), id: \.offset) { _, entry in
                    HStack(spacing: 6) {
                        Text(entry.0)
                            .font(.caption)
                            .foregroundStyle(.primary)
                        Spacer(minLength: 0)
                        Text(Self.formatDuration(ms: entry.1))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var chatHistorySection: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label("Recent chat", systemImage: "bubble.left.and.bubble.right")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Clear") { runtime.clearChatHistory() }
                    .buttonStyle(.plain)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(runtime.chatHistory.suffix(6)) { entry in
                        chatRow(entry)
                    }
                }
            }
            .frame(maxHeight: 140)
        }
    }

    private func chatRow(_ entry: ChatEntry) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(
                systemName: entry.role == .user
                    ? "person.fill"
                    : "cat.fill"
            )
            .foregroundStyle(
                entry.role == .user ? Color.blue : Color.orange
            )
            .font(.caption2)
            .frame(width: 14)
            Text(entry.text)
                .font(.caption)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(.primary)
            Spacer(minLength: 0)
        }
    }

    private var messageInput: some View {
        HStack(spacing: 6) {
            TextField("Message Deskmate", text: $messageDraft)
                .textFieldStyle(.roundedBorder)
                .focused($inputFocused)
                .onSubmit { dispatchMessage() }
            Button { dispatchMessage() } label: {
                Image(systemName: "paperplane.fill")
                    .frame(width: 18, height: 18)
            }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(
                    messageDraft.trimmingCharacters(
                        in: .whitespacesAndNewlines
                    ).isEmpty
                )
                .help("Send")
        }
    }

    private var demoSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Demo")
                .font(.subheadline.weight(.semibold))
            LazyVGrid(
                columns: [
                    GridItem(.flexible(), spacing: 6),
                    GridItem(.flexible(), spacing: 6),
                ],
                alignment: .leading,
                spacing: 6
            ) {
                demoButton("Start Build", scenario: "build")
                demoButton("Need Approval", scenario: "approval")
                demoButton("Due Reminder", scenario: "reminder")
                demoButton("Fake Codex Session", scenario: "codex_session")
                demoButton("Clear Demo", scenario: "clear")
            }
        }
    }

    private func demoButton(_ title: String, scenario: String) -> some View {
        Button(title) { runtime.triggerDemo(scenario) }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var diagnosticsSection: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 6) {
                Text(agentHealthSummary.menuText)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                Divider()
                Text(runtime.combinedIslandDiagnostics)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.top, 4)
        } label: {
            Label("Island diagnostics", systemImage: "display")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func dispatchMessage() {
        runtime.sendUserMessage(messageDraft)
        messageDraft = ""
    }

    private var footer: some View {
        HStack {
            Button("Poke pet") { runtime.clickPet() }
                .buttonStyle(.bordered)
                .controlSize(.small)
            Button {
                showSettings = true
            } label: {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help("Settings")
            Spacer()
            Button("Quit Deskmate") { runtime.quit() }
                .buttonStyle(.plain)
                .foregroundStyle(.red)
        }
    }

    // MARK: - Derived

    private var bridgeColor: Color {
        switch runtime.bridgeState {
        case .connected: return .green
        case .connecting: return .yellow
        case .waitingForRetry: return .orange
        case .stopped: return .red
        }
    }

    private var agentHealthSummary: AgentHealthSummary {
        AgentHealthSummary(sessions: runtime.sessions)
    }

    private var bridgeLabel: String {
        switch runtime.bridgeState {
        case .stopped: return "offline"
        case .connecting: return "connecting…"
        case .connected: return "connected"
        case .waitingForRetry(let attempt, let delayMs):
            return "retry #\(attempt) in \(delayMs)ms"
        }
    }

    private func dueLabel(for r: ReminderRow) -> String {
        let nowMs = Int(Date().timeIntervalSince1970 * 1000)
        let delta = r.dueAtMs - nowMs
        if delta <= 0 { return "due" }
        let mins = delta / 60_000
        if mins < 1 { return "<1m" }
        if mins < 60 { return "\(mins)m" }
        return "\(mins / 60)h"
    }
}
