import SwiftUI
import DeskmateCore

/// Top-center island surface. The idle state blends into the hardware notch;
/// activity adds small leading/trailing modules, and hover expands into compact
/// actionable rows.
struct IslandOverlay: View {
    @ObservedObject var runtime: DeskmateMenuBarRuntime
    private let moduleRegistry = IslandModuleRegistry.deskmateDefaultModules()

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .top) {
                Color.clear
                notchSurface(availableSize: geometry.size)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .ignoresSafeArea()
        .animation(.easeOut(duration: 0.22), value: isExpanded)
        .animation(.easeOut(duration: 0.22), value: shouldShowCompactContent)
    }

    private func notchSurface(availableSize: CGSize) -> some View {
        let geometry = interactionGeometry(availableSize: availableSize)
        let size = geometry.surfaceSize
        let surfaceWidth = size.width
        let surfaceHeight = size.height
        let shape = isExpanded ? NotchShape.opened : NotchShape.closed

        return ZStack(alignment: .top) {
            shape
                .fill(Color.black)
                .frame(width: surfaceWidth, height: surfaceHeight)

            Group {
                if isExpanded {
                    expandedContent
                } else if shouldShowCompactContent {
                    compactContent
                        .frame(height: surfaceHeight, alignment: .bottom)
                } else {
                    idleEdge
                }
            }
            .frame(width: surfaceWidth, height: surfaceHeight, alignment: .top)
            .clipShape(shape)
        }
        .overlay(
            shape
                .stroke(borderColor, lineWidth: isExpanded ? 0.5 : 0)
                .frame(width: surfaceWidth, height: surfaceHeight)
        )
        .contentShape(shape)
        .onTapGesture {
            runtime.handleIslandHover(.tap(tsMs: nowMs()))
        }
        .frame(maxWidth: .infinity, alignment: .top)
    }

    private var compactContent: some View {
        HStack(spacing: 0) {
            compactModule(
                alignment: .leading,
                title: compactLeadingTitle,
                subtitle: compactLeadingSubtitle,
                color: chipColor
            )
            .frame(width: compactSideWidth, alignment: .leading)

            notchCore
                .frame(width: closedNotchWidth, height: compactSurfaceHeight)

            compactTrailingModule
                .frame(width: compactSideWidth, alignment: .trailing)
        }
        .frame(height: compactSurfaceHeight)
    }

    private var idleEdge: some View {
        Color.clear
    }

    private var expandedContent: some View {
        VStack(alignment: .leading, spacing: 9) {
            expandedHeader
            if visibleSessions.isEmpty {
                emptyExpandedState
            } else {
                ScrollView {
                    VStack(spacing: 7) {
                        ForEach(visibleSessions, id: \.sessionId) { session in
                            sessionRow(session)
                        }
                    }
                }
                .scrollIndicators(.hidden)
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var expandedHeader: some View {
        HStack(alignment: .center, spacing: 10) {
            statusDot
            Text(headerTitle)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.white.opacity(0.86))
            if let source = focusSession?.sourceLabel {
                Text(source)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.42))
            }
            Spacer()
            Text(clockLabel)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.white.opacity(0.58))
            Button {
                runtime.closeIslandSessionList(source: .island)
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
            }
            .buttonStyle(.plain)
            .foregroundStyle(.white.opacity(0.58))
            .help("Close")
        }
    }

    private func sessionRow(_ session: SessionRow) -> some View {
        HStack(alignment: .top, spacing: 10) {
            phaseGlyph(for: session)
                .frame(width: 18, height: 22)
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 7) {
                    Text(session.displayTitle)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.92))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(session.phaseLabel)
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(phaseColor(for: session))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(phaseColor(for: session).opacity(0.14)))
                    Spacer(minLength: 0)
                    Text(sessionAgeLabel(session))
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.35))
                    if session.canAttemptJump {
                        Button {
                            runtime.jumpToSession(session.sessionId, source: .island)
                        } label: {
                            Image(systemName: "arrow.up.forward.app")
                                .font(.system(size: 10, weight: .semibold))
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.white.opacity(0.62))
                        .help("Jump to session")
                    }
                }
                Text(sessionActivityLine(session))
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.white.opacity(0.48))
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let approval = approval(for: session) {
                    approvalInline(session: session, approval: approval)
                } else if session.phase == .waitingForAnswer {
                    questionInline(session: session)
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(Color.white.opacity(session.needsUserAction ? 0.075 : 0.045))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .strokeBorder(phaseColor(for: session).opacity(session.needsUserAction ? 0.22 : 0.08))
        )
    }

    private func approvalInline(session: SessionRow, approval: ApprovalRow) -> some View {
        HStack(spacing: 7) {
            Text(approvalTitle(session: session, approval: approval))
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.white.opacity(0.72))
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 0)
            Button("Deny") {
                runtime.resolveApproval(id: approval.approvalId, allow: false, source: .island)
            }
            .buttonStyle(VibeIslandButtonStyle(kind: .secondary))
            Button("Allow") {
                runtime.resolveApproval(id: approval.approvalId, allow: true, source: .island)
            }
            .buttonStyle(VibeIslandButtonStyle(kind: .primary))
        }
    }

    private func questionInline(session: SessionRow) -> some View {
        QuestionInlineView(runtime: runtime, session: session)
    }

    private var emptyExpandedState: some View {
        HStack(spacing: 9) {
            Circle()
                .fill(Color.white.opacity(0.18))
                .frame(width: 8, height: 8)
            Text("No live agent sessions")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.white.opacity(0.48))
            Spacer()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 13)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color.white.opacity(0.04)))
    }

    private var statusDot: some View {
        Circle()
            .fill(chipColor)
            .frame(width: isExpanded ? 7 : 7, height: isExpanded ? 7 : 7)
    }

    private func compactModule(
        alignment: HorizontalAlignment,
        title: String,
        subtitle: String?,
        color: Color
    ) -> some View {
        HStack(spacing: 6) {
            if alignment == .leading {
                pixelAvatar(color: color)
            }
            VStack(alignment: alignment, spacing: 1) {
                Text(title)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.88))
                    .lineLimit(1)
                    .minimumScaleFactor(0.82)
                if let subtitle {
                    Text(subtitle)
                        .font(.system(size: 8, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.42))
                        .lineLimit(1)
                }
            }
            if alignment == .trailing {
                pixelAvatar(color: color)
            }
        }
        .padding(.horizontal, 8)
    }

    private var notchCore: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            Capsule(style: .continuous)
                .fill(Color.white.opacity(hasCompactPresence ? 0.08 : 0))
                .frame(width: min(72, closedNotchWidth * 0.34), height: 3)
                .padding(.bottom, 6)
        }
    }

    private var compactTrailingModule: some View {
        HStack(spacing: 6) {
            if let notification = activeNotification {
                Image(systemName: notificationSymbol(for: notification))
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(Color.yellow)
                Text(activeModuleDescriptor?.badge ?? "now")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.72))
            } else if let badge = badgeCount {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(Color.orange)
                Text("\(badge)")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white)
            } else if activeSessionCount > 0 {
                Text("\(activeSessionCount)")
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.9))
                Text("live")
                    .font(.system(size: 8, weight: .medium, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.42))
            } else {
                Text(compactSourceLabel)
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.68))
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 8)
    }

    private func pixelAvatar(color: Color) -> some View {
        VStack(spacing: 1) {
            HStack(spacing: 1) {
                Rectangle().fill(color).frame(width: 3, height: 3)
                Rectangle().fill(color.opacity(0.65)).frame(width: 3, height: 3)
            }
            HStack(spacing: 1) {
                Rectangle().fill(.white.opacity(0.72)).frame(width: 3, height: 3)
                Rectangle().fill(color).frame(width: 3, height: 3)
            }
        }
        .frame(width: 9, height: 9)
    }

    private var borderColor: Color {
        isExpanded ? Color.white.opacity(0.06) : Color.clear
    }

    private var closedNotchWidth: CGFloat {
        islandScreen?.deskmateNotchSize.width ?? 224
    }

    private var closedNotchHeight: CGFloat {
        islandScreen?.deskmateIslandClosedHeight ?? 24
    }

    private var compactSurfaceHeight: CGFloat {
        interactionGeometry(availableSize: NSScreen.main?.frame.size ?? .zero)
            .closedSurfaceHeight
    }

    private var compactExpansionWidth: CGFloat {
        interactionGeometry(availableSize: NSScreen.main?.frame.size ?? .zero)
            .closedExpansionWidth
    }

    private var compactSideWidth: CGFloat {
        max(0, compactExpansionWidth / 2)
    }

    private var shouldShowCompactContent: Bool {
        hasCompactPresence || islandScreen?.deskmateHasPhysicalNotch != true
    }

    private var hasCompactPresence: Bool {
        runtime.bridgeState != .connected
            || activeIslandKindRequiresCompactPresence
            || !runtime.sessions.isEmpty
            || !runtime.approvals.isEmpty
    }

    private var activeIslandKindRequiresCompactPresence: Bool {
        guard let kind = runtime.island?.state.kind else { return false }
        switch kind {
        case .compact, .empty:
            return false
        case .liveActivity, .notificationCard, .sessionList:
            return true
        }
    }

    private var islandScreen: NSScreen? {
        NSScreen.screens.first(where: { $0.deskmateHasPhysicalNotch })
            ?? NSScreen.main
    }

    // MARK: - Derived

    private var isExpanded: Bool {
        runtime.island?.state.kind == .sessionList
    }

    private var visibleSessions: [SessionRow] {
        SessionListAdapter(maxRows: 5, showClosedAfterMs: 5 * 60 * 1000)
            .display(sessions: runtime.sessions, nowMs: nowMs())
    }

    private var actionableSession: SessionRow? {
        visibleSessions.first { $0.needsUserAction }
    }

    private var focusSession: SessionRow? {
        actionableSession
            ?? runtime.domain.activeSessionId.flatMap { active in
                visibleSessions.first { $0.sessionId == active }
            }
            ?? visibleSessions.first
    }

    private var chipColor: Color {
        switch runtime.bridgeState {
        case .stopped: return .red
        case .connecting, .waitingForRetry: return .orange
        case .connected:
            if activeNotification != nil { return .yellow }
            if !runtime.approvals.isEmpty { return .orange }
            if let session = focusSession { return phaseColor(for: session) }
            switch runtime.island?.state.kind {
            case .liveActivity: return .blue
            case .notificationCard: return .yellow
            case .sessionList: return .purple
            case .compact, .empty, .none: return .green
            }
        }
    }

    private var activeSessionCount: Int {
        visibleSessions.filter { $0.state != .closed }.count
    }

    private var headerTitle: String {
        if !runtime.approvals.isEmpty { return "Action needed" }
        if activeSessionCount > 0 { return "Agent sessions" }
        return "Deskmate Island"
    }

    private var compactLeadingTitle: String {
        if runtime.bridgeState != .connected { return "OFF" }
        if let descriptor = activeModuleDescriptor,
           activeIslandKindRequiresCompactPresence {
            return descriptor.title
        }
        if let session = focusSession {
            return (session.sourceLabel ?? sourceShortName(session.source ?? "agent")).uppercased()
        }
        return "DM"
    }

    private var compactLeadingSubtitle: String? {
        if runtime.bridgeState != .connected { return "offline" }
        if let descriptor = activeModuleDescriptor,
           activeIslandKindRequiresCompactPresence {
            return descriptor.subtitle.map { truncateForPill($0, maxChars: 14) }
        }
        if let session = focusSession { return session.phaseLabel }
        return nil
    }

    private var compactSourceLabel: String {
        if let session = focusSession {
            return sourceShortName(session.sourceLabel ?? session.source ?? "agent")
        }
        return "idle"
    }

    private var label: String {
        switch runtime.bridgeState {
        case .stopped: return "Deskmate offline"
        case .connecting: return "connecting..."
        case .waitingForRetry(let attempt, _):
            return "retry #\(attempt)"
        case .connected:
            if let change = runtime.island {
                return islandLabel(change)
            }
            if runtime.approvals.count > 0 {
                return "needs approval"
            }
            if let id = runtime.domain.activeSessionId {
                if let session = runtime.sessions.first(where: { $0.sessionId == id }) {
                    return "\(session.phaseLabel): \(session.displayTitle)"
                }
                return "in: \(id)"
            }
            return "Deskmate"
        }
    }

    private var activeNotification: IslandSurfaceState? {
        guard let state = runtime.island?.state,
              state.kind == .notificationCard
        else { return nil }
        return state
    }

    private var activeModuleDescriptor: IslandModuleRenderDescriptor? {
        guard let state = runtime.island?.state else {
            return moduleRegistry.renderDescriptor(
                for: IslandSurfaceState(kind: .compact)
            )
        }
        return moduleRegistry.renderDescriptor(for: state)
    }

    private func notificationTitle(for state: IslandSurfaceState) -> String {
        if let descriptor = moduleRegistry.renderDescriptor(for: state) {
            return descriptor.title
        }
        let id = (state.activityId ?? state.sessionId ?? "notice").lowercased()
        if id.contains("reminder") { return "REMIND" }
        if id.contains("approval") { return "ASK" }
        return "NOTICE"
    }

    private func notificationSubtitle(for state: IslandSurfaceState) -> String? {
        if let descriptor = moduleRegistry.renderDescriptor(for: state),
           let subtitle = descriptor.subtitle {
            return truncateForPill(subtitle, maxChars: 14)
        }
        if let detail = state.detail?.trimmingCharacters(in: .whitespacesAndNewlines),
           !detail.isEmpty {
            return truncateForPill(detail, maxChars: 14)
        }
        return "now"
    }

    private func notificationSymbol(for state: IslandSurfaceState) -> String {
        if let descriptor = moduleRegistry.renderDescriptor(for: state),
           let image = descriptor.systemImageName {
            return image
        }
        let id = (state.activityId ?? state.sessionId ?? "notice").lowercased()
        if id.contains("reminder") { return "bell.fill" }
        if id.contains("approval") { return "exclamationmark.triangle.fill" }
        return "sparkle"
    }

    private var badgeCount: Int? {
        let pending = runtime.approvals.count
        return pending > 0 ? pending : nil
    }

    private func phaseGlyph(for session: SessionRow) -> some View {
        let symbol: String
        let color: Color
        switch session.phase {
        case .waitingForApproval:
            symbol = "exclamationmark.triangle.fill"
            color = .yellow
        case .waitingForAnswer:
            symbol = "questionmark.circle.fill"
            color = .orange
        case .thinking:
            symbol = "brain.head.profile"
            color = .purple
        case .editing:
            symbol = "pencil"
            color = .blue
        case .runningTool:
            symbol = "terminal"
            color = .cyan
        case .testing:
            symbol = "checkmark.seal"
            color = .mint
        case .failed:
            symbol = "xmark.octagon.fill"
            color = .red
        case .completed:
            symbol = "checkmark.circle.fill"
            color = .green
        case .running, .unknown:
            symbol = "bolt.fill"
            color = .green
        }
        return Image(systemName: symbol)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(color)
    }

    private func approval(for session: SessionRow) -> ApprovalRow? {
        runtime.approvals.first { approval in
            approval.sessionId == session.sessionId
        }
    }

    private func phaseColor(for session: SessionRow) -> Color {
        switch session.phase {
        case .waitingForApproval: return .orange
        case .waitingForAnswer: return .yellow
        case .thinking: return .purple
        case .editing: return .blue
        case .runningTool: return .cyan
        case .testing: return .mint
        case .failed: return .red
        case .completed: return .green
        case .running, .unknown: return Color(red: 0.29, green: 0.86, blue: 0.46)
        }
    }

    private func shortWorkspace(for session: SessionRow) -> String {
        if let cwd = session.cwd, !cwd.isEmpty {
            return URL(fileURLWithPath: cwd).lastPathComponent
        }
        return ""
    }

    private func approvalTitle(session: SessionRow, approval: ApprovalRow) -> String {
        let prompt = approval.prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        if !prompt.isEmpty { return prompt }
        return session.displayTitle
    }

    private func sessionActivityLine(_ session: SessionRow) -> String {
        session.activityLine
    }

    private func sessionAgeLabel(_ session: SessionRow) -> String {
        let delta = max(0, nowMs() - session.updatedAtMs)
        if delta < 60_000 { return "\(max(1, delta / 1000))s" }
        let minutes = delta / 60_000
        if minutes < 60 { return "\(minutes)m" }
        return "\(minutes / 60)h"
    }

    private func sourceShortName(_ source: String) -> String {
        let cleaned = source.replacingOccurrences(of: " ", with: "")
        return String(cleaned.prefix(6))
    }

    private var clockLabel: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEE h:mm a"
        return formatter.string(from: Date())
    }

    private func islandLabel(_ change: LiveIslandSurfaceStore.ChangeEvent) -> String {
        switch change.state.kind {
        case .compact: return "Deskmate"
        case .empty: return "Deskmate"
        case .notificationCard:
            return change.state.activityId ?? "notification"
        case .liveActivity:
            let id = change.state.activityId ?? "-"
            let codingPrefix = "coding-"
            let primary: String
            if id.hasPrefix(codingPrefix) {
                let name = String(id.dropFirst(codingPrefix.count))
                primary = "Coding: \(name)"
            } else {
                primary = "live: \(id)"
            }
            if let detail = change.state.detail, !detail.isEmpty {
                return "\(primary) · \(truncateForPill(detail))"
            }
            return primary
        case .sessionList:
            let count = runtime.sessions.count
            return count > 0 ? "sessions (\(count))" : "sessions"
        }
    }

    private func truncateForPill(_ text: String, maxChars: Int = 36) -> String {
        guard text.count > maxChars else { return text }
        let head = text.prefix(maxChars - 1)
        return "\(head)..."
    }

    private func nowMs() -> Int {
        Int(Date().timeIntervalSince1970 * 1000)
    }

    private func interactionGeometry(availableSize: CGSize) -> IslandInteractionGeometry {
        let screen = islandScreen
        let frame = screen?.frame ?? CGRect(origin: .zero, size: availableSize)
        return IslandInteractionGeometry(input: IslandInteractionInput(
            screenFrame: frame,
            notchSize: screen?.deskmateNotchSize ?? CGSize(width: 224, height: 24),
            hasPhysicalNotch: screen?.deskmateHasPhysicalNotch == true,
            hasCompactPresence: hasCompactPresence,
            isExpanded: isExpanded,
            activeCount: max(visibleSessions.count, runtime.approvals.count)
        ))
    }
}

private struct QuestionInlineView: View {
    @ObservedObject var runtime: DeskmateMenuBarRuntime
    let session: SessionRow
    @State private var answer = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(prompt)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.white.opacity(0.72))
                .lineLimit(1)
                .truncationMode(.tail)
            HStack(spacing: 7) {
                TextField("Answer", text: $answer)
                    .textFieldStyle(.plain)
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.white.opacity(0.88))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 7)
                    .background(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .fill(Color.white.opacity(0.08))
                    )
                    .onSubmit(send)
                Button("Jump") {
                    runtime.jumpToSession(session.sessionId, source: .island)
                }
                .buttonStyle(VibeIslandButtonStyle(kind: .secondary))
                Button("Send") {
                    send()
                }
                .buttonStyle(VibeIslandButtonStyle(kind: .primary))
                .disabled(answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .opacity(answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.45 : 1)
            }
        }
    }

    private var prompt: String {
        let text = session.summary.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? "Agent is waiting for your answer." : text
    }

    private func send() {
        let trimmed = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        runtime.answerQuestion(
            sessionId: session.sessionId,
            answer: trimmed,
            source: .island
        )
        answer = ""
    }
}

private struct VibeIslandButtonStyle: ButtonStyle {
    enum Kind {
        case primary
        case secondary
    }

    let kind: Kind

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(foreground)
            .padding(.vertical, 9)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(background(configuration.isPressed))
            )
            .opacity(configuration.isPressed ? 0.82 : 1)
    }

    private var foreground: Color {
        switch kind {
        case .primary: return .black
        case .secondary: return .white.opacity(0.88)
        }
    }

    private func background(_ pressed: Bool) -> Color {
        switch kind {
        case .primary:
            return Color.white.opacity(pressed ? 0.76 : 0.9)
        case .secondary:
            return Color.white.opacity(pressed ? 0.09 : 0.14)
        }
    }
}
