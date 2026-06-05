import SwiftUI
import DeskmateCore

/// Top-center island surface. The idle state blends into the hardware notch;
/// activity adds small leading/trailing modules, and hover expands into compact
/// actionable rows.
struct IslandOverlay: View {
    @ObservedObject var runtime: DeskmateMenuBarRuntime
    /// V11 architecture polish: dedicated ViewModel that collapses
    /// runtime's 6+ @Published fields into a closed `IslandContent`
    /// enum + `IslandStatus`. We migrate code paths to read from
    /// viewModel incrementally; the rest still observes runtime.
    @ObservedObject var viewModel: IslandViewModel
    /// V10 island polish #10: pull from the runtime registry so
    /// externally-registered modules show up in the trailing module
    /// without us instantiating them at view-init time.
    private var moduleRegistry: IslandModuleRegistry {
        runtime.islandModules
    }

    /// V10 island polish #2 (MioIsland-inspired): carousel index that
    /// rotates the trailing module through up to 4 facts every 3
    /// seconds when sessions are active. Reset to 0 when sessions
    /// disappear so we don't stick on a stale slide.
    @State private var carouselIndex: Int = 0
    @State private var pulsePhase: Bool = false
    private let carouselTimer = Timer.publish(every: 3, on: .main, in: .common).autoconnect()
    private let pulseTimer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    /// V10 island polish: asymmetric springs for the
    /// notch-surface transition, mirrored on the AppKit panel
    /// frame in ``IslandWindowController.applyPanelFrame``.
    /// Now just thin aliases to ``IslandAnimations`` (#6) so the
    /// numbers live in one place and the smoke binary can lock them
    /// without pulling in the menu-bar module.
    private static let openSpring = IslandAnimations.open
    private static let closeSpring = IslandAnimations.close
    private static let popSpring = IslandAnimations.pop
    /// Interactive spring for hover-triggered width changes (boring.notch).
    private static let hoverSpring = IslandAnimations.hover

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .top) {
                Color.clear
                notchSurface(availableSize: geometry.size)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .ignoresSafeArea()
        // Pick the asymmetric animation at runtime: open uses the
        // bouncy spring, close uses the smooth ease-out, and
        // sneak-peek uses the fast pop spring.
        .animation(
            isExpanded ? Self.openSpring : Self.closeSpring,
            value: isExpanded
        )
        // SneakPeek width transition uses the pop spring for a
        // quick attention-grabbing pulse.
        .animation(
            Self.popSpring,
            value: isSneakPeek
        )
        // Compact-content visibility flips on a smaller curve — its
        // appearance is decoupled from the surface size animation,
        // so a session arriving while the user is hovering doesn't
        // restart the open spring.
        .animation(.easeOut(duration: 0.18), value: shouldShowCompactContent)
        // V10 island polish #2: advance the trailing carousel only
        // while we have something to rotate through. The timer is
        // autoconnected globally but the index only ticks while the
        // carousel is visible.
        .onReceive(carouselTimer) { _ in
            guard carouselFactCount > 1 else {
                if carouselIndex != 0 { carouselIndex = 0 }
                return
            }
            withAnimation(.easeInOut(duration: 0.4)) {
                carouselIndex = (carouselIndex + 1) % carouselFactCount
            }
        }
        .onReceive(pulseTimer) { _ in
            guard shouldPulseStatusDot else {
                if pulsePhase { pulsePhase = false }
                return
            }
            withAnimation(.easeInOut(duration: 0.9)) {
                pulsePhase.toggle()
            }
        }
    }

    private func notchSurface(availableSize: CGSize) -> some View {
        let geometry = interactionGeometry(availableSize: availableSize)
        let size = geometry.surfaceSize
        // R5.1/R5.7: During SneakPeek, widen the compact surface by 80pt
        // to give the "peek" visual cue that something arrived.
        let surfaceWidth = (!isExpanded && isSneakPeek) ? size.width + 80 : size.width
        let surfaceHeight = size.height
        let shape = isExpanded ? NotchShape.opened : NotchShape.closed

        return ZStack(alignment: .top) {
            shape
                .fill(Color.black)
                .frame(width: surfaceWidth, height: surfaceHeight)

            Group {
                if isExpanded {
                    expandedContent
                        // Inset content from the clip boundary so the
                        // rounded corners of NotchShape don't cut into
                        // session rows. Same approach as open-vibe-island:
                        // content frame is narrower than the surface.
                        .frame(width: surfaceWidth - 28)
                        .transition(.asymmetric(
                            insertion: .opacity
                                .combined(with: .move(edge: .top))
                                .animation(Self.openSpring),
                            removal: .opacity
                                .animation(.easeIn(duration: 0.12))
                        ))
                } else if shouldShowCompactContent {
                    compactContent
                        .frame(height: surfaceHeight, alignment: .bottom)
                        .transition(.opacity.animation(.easeOut(duration: 0.16)))
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
        // R4: Full-width progress bar at the bottom of the pill
        .overlay(alignment: .bottom) {
            EmptyView() // Progress moved to trailing module
        }
        .contentShape(shape)
        .onTapGesture {
            // V11 architecture: route through viewModel action verbs
            // instead of reaching into runtime directly. Same effect,
            // but the view layer no longer knows about
            // ``handleIslandHover`` semantics.
            withAnimation(Self.hoverSpring) {
                viewModel.tap()
            }
        }
        // V10 island polish #5 (boring.notch-inspired): swipe down
        // to expand, swipe up to collapse. Threshold 18pt — short
        // enough to feel responsive but high enough to not trigger
        // on jitter while hovering. Only active in their respective
        // states so we don't try to expand an already-expanded notch.
        .gesture(
            DragGesture(minimumDistance: 18)
                .onEnded { value in
                    let dy = value.translation.height
                    if dy > 18 && !isExpanded {
                        viewModel.open()
                    } else if dy < -18 && isExpanded {
                        viewModel.close()
                    }
                }
        )
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
            .frame(width: leadingSideWidth, alignment: .leading)

            notchCore
                .frame(width: closedNotchWidth, height: compactSurfaceHeight)

            compactTrailingModule
                .frame(width: trailingSideWidth, alignment: .trailing)
        }
        .frame(height: compactSurfaceHeight)
        // Asymmetric width animation lets the trailing module morph
        // smoothly when build-done state arrives — the checkmark
        // appears in a wider column than the running progress bar.
        .animation(.spring(response: 0.32, dampingFraction: 0.86), value: isBuildDoneState)
    }

    /// When in build-done state, give trailing a bit more room but
    /// keep leading readable. 60/40 split instead of 50/150.
    private var leadingSideWidth: CGFloat {
        isBuildDoneState ? compactSideWidth * 0.75 : compactSideWidth
    }

    private var trailingSideWidth: CGFloat {
        isBuildDoneState ? compactSideWidth * 1.25 : compactSideWidth
    }

    private var idleEdge: some View {
        Color.clear
    }

    private var expandedContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            expandedHeader
            if visibleSessions.isEmpty {
                emptyExpandedState
            } else {
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(visibleSessions, id: \.sessionId) { session in
                            sessionRow(session)
                        }
                    }
                }
                .scrollIndicators(.hidden)
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, 8)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var expandedHeader: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(headerTitle)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.white.opacity(0.88))
            Spacer()
            Text(clockLabel)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.white.opacity(0.42))
            Button {
                viewModel.close()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.5))
                    .frame(width: 22, height: 22)
                    .background(.white.opacity(0.08), in: Circle())
            }
            .buttonStyle(.plain)
            .help("Close")
        }
        .frame(height: closedNotchHeight)
    }

    private func sessionRow(_ session: SessionRow) -> some View {
        HStack(alignment: .top, spacing: 12) {
            // Status dot instead of large glyph — cleaner, like open-vibe-island
            Circle()
                .fill(phaseColor(for: session))
                .frame(width: 8, height: 8)
                .padding(.top, 5)

            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .top, spacing: 8) {
                    Text(session.displayTitle)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.92))
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Spacer(minLength: 4)

                    HStack(spacing: 5) {
                        // Phase chip
                        HStack(spacing: 2) {
                            Text(session.phaseLabel)
                                .font(.system(size: 9, weight: .bold))
                                .foregroundStyle(
                                    isUnobserved(session)
                                        ? phaseColor(for: session).opacity(0.5)
                                        : phaseColor(for: session)
                                )
                            if isUnobserved(session) {
                                Text("?")
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundStyle(phaseColor(for: session).opacity(0.5))
                            } else if isWorkingPhase(session.phase) {
                                // V10 #4: pulsing ellipsis tells the
                                // user the agent is actively progressing.
                                AnimatedEllipsis(
                                    color: phaseColor(for: session),
                                    size: 9
                                )
                            }
                        }
                        .padding(.horizontal, 6)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(phaseColor(for: session).opacity(isUnobserved(session) ? 0.06 : 0.12)))

                        // Age badge
                        Text(sessionAgeLabel(session))
                            .font(.system(size: 9, weight: .medium, design: .monospaced))
                            .foregroundStyle(.white.opacity(0.35))

                        if session.canAttemptJump {
                            Button {
                                viewModel.jumpToSession(session.sessionId)
                            } label: {
                                Image(systemName: "arrow.up.forward.app")
                                    .font(.system(size: 10, weight: .semibold))
                            }
                            .buttonStyle(.plain)
                            .foregroundStyle(.white.opacity(0.5))
                        }
                    }
                }

                Text(sessionActivityLine(session))
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.white.opacity(0.5))
                    .lineLimit(1)
                    .truncationMode(.middle)

                if let approval = approval(for: session) {
                    approvalInline(session: session, approval: approval)
                } else if session.phase == .waitingForAnswer {
                    questionInline(session: session)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color.white.opacity(session.needsUserAction ? 0.06 : 0.03))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(
                    session.needsUserAction
                        ? phaseColor(for: session).opacity(0.3)
                        : Color.white.opacity(0.05)
                )
        )
    }

    private func approvalInline(session: SessionRow, approval: ApprovalRow) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(approvalTitle(session: session, approval: approval))
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.88))
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(Color(red: 0.11, green: 0.08, blue: 0.03))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .strokeBorder(.orange.opacity(0.18))
                )

            HStack(spacing: 8) {
                Button("Deny") {
                    viewModel.resolveApproval(approval, allow: false)
                }
                .buttonStyle(VibeIslandButtonStyle(kind: .secondary))
                Button("Allow") {
                    viewModel.resolveApproval(approval, allow: true)
                }
                .buttonStyle(VibeIslandButtonStyle(kind: .primary))
            }
        }
    }

    private func questionInline(session: SessionRow) -> some View {
        QuestionInlineView(runtime: runtime, session: session)
    }

    private var emptyExpandedState: some View {
        VStack(spacing: 12) {
            Spacer()
            Text("No live agent sessions")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(.white.opacity(0.4))
            Text("Start an agent to see activity here")
                .font(.system(size: 12))
                .foregroundStyle(.white.opacity(0.25))
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private var statusDot: some View {
        Circle()
            .fill(chipColor)
            .frame(width: isExpanded ? 7 : 7, height: isExpanded ? 7 : 7)
            .opacity(shouldPulseStatusDot && pulsePhase ? 0.52 : 1.0)
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
            if let progress = activeProgress, progress >= 0.0, progress <= 1.0 {
                // boring.notch style: GeometryReader-based progress that
                // fills whatever width the trailing module has available.
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule()
                            .fill(Color.white.opacity(0.12))
                        Capsule()
                            .fill(progressFillStyle)
                            .frame(width: max(0, geo.size.width * CGFloat(progress)))
                            .shadow(color: chipColor.opacity(0.5), radius: 4, x: 2)
                            .opacity(progress == 0 ? 0 : 1)
                            .animation(
                                runtime.degradationPolicy.level >= 1
                                    ? nil
                                    : IslandAnimations.progress,
                                value: progress
                            )
                    }
                }
                .frame(height: 6)
                .clipShape(Capsule())
                if Int(progress * 100) > 0 {
                    Text("\(Int(progress * 100))%")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.72))
                        .fixedSize()
                }
            } else if isBuildDoneState {
                // Completion state: checkmark + "done" in the same trailing
                // position where the progress bar was, so it feels like a
                // smooth morph from 100% → ✓ done.
                HStack(spacing: 4) {
                    Image(systemName: isBuildFailed ? "xmark.circle.fill" : "checkmark.circle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(isBuildFailed ? .red : .green)
                        .transition(.scale.combined(with: .opacity))
                    Text(buildDoneMessage)
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.82))
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                        .truncationMode(.tail)
                        .layoutPriority(1)
                }
                .animation(.spring(duration: 0.3), value: isBuildDoneState)
            } else if shouldShowMultiSessionGlyphs {
                multiSessionGlyphs
            } else if let notification = activeNotification {
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
                // V10 island polish #2: rotate facts about active
                // sessions every 3s instead of always showing the
                // count + "live". User can absorb richer status
                // without expanding.
                trailingCarousel
            } else {
                Text(compactSourceLabel)
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.68))
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 8)
    }

    // MARK: - Multi-Session Glyph Stack (R6)

    /// Multi-session glyph stack for the trailing module (R6).
    /// Shows overlapping circular badges for up to 3 sessions,
    /// plus a numeric overflow indicator when there are more.
    @ViewBuilder
    private var multiSessionGlyphs: some View {
        let sessions = visibleSessionsForGlyphs
        if sessions.count >= 2 {
            HStack(spacing: -4) {  // overlapping stack
                ForEach(sessions.prefix(3), id: \.sessionId) { session in
                    sessionGlyph(for: session)
                }
                if sessions.count > 3 {
                    Text("+\(min(sessions.count - 3, 99))")
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.72))
                        .padding(.leading, 4)
                }
            }
            .animation(
                .spring(duration: IslandAnimationTuning.default.multiSessionGlyphSpring),
                value: sessions.map(\.sessionId)
            )
        }
    }

    /// Individual session glyph: 14pt circle with phase colour and agent icon.
    private func sessionGlyph(for session: SessionRow) -> some View {
        let colors = PhaseColorTable.resolve(session.phase)
        return ZStack(alignment: .bottomTrailing) {
            Circle()
                .fill(colors.background)
                .frame(width: 14, height: 14)
                .overlay(
                    Image(systemName: agentKindIcon(for: session))
                        .font(.system(size: 7, weight: .bold))
                        .foregroundStyle(colors.foreground)
                )
            // R6.4: status dot for waiting phases
            if session.phase == .waitingForApproval || session.phase == .waitingForAnswer {
                Circle()
                    .fill(colors.stroke)
                    .frame(width: 4, height: 4)
                    .offset(x: 1, y: 1)
            }
        }
    }

    /// Map agent source to a system icon name for the glyph badge.
    private func agentKindIcon(for session: SessionRow) -> String {
        let source = (session.source ?? "").lowercased()
        if source.contains("codex") { return "terminal" }
        if source.contains("claude") { return "bubble.left" }
        if source.contains("gemini") { return "sparkle" }
        if source.contains("cursor") { return "cursorarrow" }
        if source.contains("windsurf") { return "wind" }
        if source.contains("kiro") { return "k.circle" }
        return "questionmark.circle"
    }

    /// Active (non-closed) sessions sorted by most-recent-activity-first.
    private var visibleSessionsForGlyphs: [SessionRow] {
        runtime.sessions
            .filter { $0.state != .closed }
            .sorted { $0.updatedAtMs > $1.updatedAtMs }
    }

    /// V11 architecture polish: multi-session glyph visibility now
    /// reads from the closed `IslandContent` enum. The 2+ session
    /// count and the "no other higher-priority content" check both
    /// happen in `IslandViewModel.computeContent`.
    private var shouldShowMultiSessionGlyphs: Bool {
        viewModel.content.isMultiSession
    }

    /// Progress capsule for the compact trailing module (R4).
    /// Now rendered inline in compactTrailingModule using GeometryReader.
    /// This computed property is kept for backward compat but unused.
    private var activeProgress: Double? {
        runtime.island?.state.progress
    }

    // MARK: - Trailing Carousel (#2 — MioIsland-inspired)

    /// Number of carousel slides we have facts for. Returns at most
    /// the number of sessions visible plus a count slide; capped at 4.
    private var carouselFactCount: Int {
        carouselFacts.count
    }

    /// Facts to rotate through. Order mirrors MioIsland's compact
    /// density: task/workspace, phase+duration, source, count/pending.
    private var carouselFacts: [(primary: String, secondary: String?)] {
        guard let session = focusSession else { return [] }
        var facts: [(String, String?)] = []

        let title = truncateForPill(session.displayTitle, maxChars: 12)
        if !title.isEmpty {
            facts.append((title.uppercased(), shortWorkspace(for: session).nilIfEmpty))
        }

        facts.append((session.phaseLabel.uppercased(), sessionAgeLabel(session)))

        if let src = session.sourceLabel ?? session.source {
            facts.append((sourceShortName(src).uppercased(), session.kind.map(sourceShortName)))
        }

        let pending = runtime.approvals.count
        if activeSessionCount > 1 {
            facts.append(("\(activeSessionCount)", pending > 0 ? "\(pending) ask" : "live"))
        } else if pending > 0 {
            facts.append(("\(pending)", "pending"))
        }

        return Array(facts.prefix(4))
    }

    @ViewBuilder
    private var trailingCarousel: some View {
        let facts = carouselFacts
        let idx = facts.isEmpty ? 0 : (carouselIndex % facts.count)
        let fact = facts[safe: idx]
        if let fact {
            HStack(spacing: 4) {
                Text(fact.primary)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.9))
                if let sub = fact.secondary {
                    Text(sub)
                        .font(.system(size: 8, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.42))
                }
            }
            .id("carousel-\(idx)")
            .transition(.asymmetric(
                insertion: .opacity.combined(with: .move(edge: .top)),
                removal: .opacity.combined(with: .move(edge: .bottom))
            ))
        }
    }

    /// V11 architecture polish: build-done detection now sources
    /// from the closed `IslandContent` enum. The complex
    /// activity-id + emoji + progress check lives in
    /// ``IslandViewModel.computeContent``; views just ask the enum.
    private var isBuildDoneState: Bool {
        viewModel.content.isBuildDone
    }

    private var isBuildFailed: Bool {
        if case .build(_, _, _, _, let isFailed) = viewModel.content {
            return isFailed
        }
        return false
    }

    private var buildDoneMessage: String {
        guard let detail = runtime.island?.state.detail else { return "done" }
        // Find emoji boundary then return the text after the last " · "
        // segment (that's the message slot).
        let cleaned = detail
            .replacingOccurrences(of: "✅", with: "")
            .replacingOccurrences(of: "❌", with: "")
            .trimmingCharacters(in: .whitespaces)
        // detail format: "<task> · [<branch> ·] <message>"
        let parts = cleaned.components(separatedBy: " · ")
        if parts.count >= 2, let last = parts.last,
           !last.trimmingCharacters(in: .whitespaces).isEmpty {
            return String(last.prefix(24))
        }
        // No message — fall back to a generic label.
        return isBuildFailed ? "failed" : "done"
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

    // MARK: - SneakPeek (R5)

    /// Whether the island is currently in the transient SneakPeek state.
    /// Reads from both the state machine and the surface store so the
    /// overlay reacts regardless of which path set the flag.
    private var isSneakPeek: Bool {
        runtime.island?.state.isSneakPeek == true || runtime.shell.islandSurface.isSneakPeek
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
            if let approval = runtime.approvals.first {
                return approvalUrgencyColor(approval)
            }
            if let session = focusSession { return phaseColor(for: session) }
            // V10 island polish #7: idle-state chip color defers to
            // the user's accent preset. Active phases keep their
            // semantic colors (orange = needs approval, blue = busy)
            // because those carry meaning across themes.
            return userAccentColor
        }
    }

    private var shouldPulseStatusDot: Bool {
        guard runtime.bridgeState == .connected else { return false }
        if !runtime.approvals.isEmpty { return true }
        if activeNotification != nil { return true }
        if let session = focusSession {
            return session.phase != .completed
                && session.phase != .failed
                && session.phase != .unknown
        }
        switch runtime.island?.state.kind {
        case .liveActivity, .sessionList:
            return true
        case .compact, .empty, .notificationCard, .none:
            return false
        }
    }

    /// V10 #7 helper: user-tunable accent color preset, defaulting to
    /// the system accent. Used for the idle chip color and (via
    /// ``progressFillStyle``) the progress capsule fill.
    private var userAccentColor: Color {
        runtime.topSurfaceCustomization.current.accent.color
    }

    /// V10 #8: progress capsule fill — solid by default, optional
    /// LinearGradient that fades from accent → muted accent for a
    /// subtle highlight look (Atoll-style).
    private var progressFillStyle: AnyShapeStyle {
        let useGradient = runtime.topSurfaceCustomization.current.useGradientProgress
        if useGradient {
            return AnyShapeStyle(LinearGradient(
                colors: [chipColor, chipColor.opacity(0.45)],
                startPoint: .trailing,
                endPoint: .leading
            ))
        }
        return AnyShapeStyle(chipColor)
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
        if isBuildDoneState {
            // The trailing module already shows ✓/✗ + status message.
            // Strip the redundant emoji + message from the leading subtitle
            // so the leading column shows just the task name.
            return buildTaskName.map { truncateForPill($0, maxChars: 14) }
        }
        if let descriptor = activeModuleDescriptor,
           activeIslandKindRequiresCompactPresence {
            return descriptor.subtitle.map { truncateForPill($0, maxChars: 14) }
        }
        if let session = focusSession { return session.phaseLabel }
        return nil
    }

    /// Extract the task name from the build-done detail string.
    /// Format: "✅ <task> · [<branch> ·] <message>" — return just "<task>".
    private var buildTaskName: String? {
        guard let detail = runtime.island?.state.detail else { return nil }
        let cleaned = detail
            .replacingOccurrences(of: "✅", with: "")
            .replacingOccurrences(of: "❌", with: "")
            .trimmingCharacters(in: .whitespaces)
        let first = cleaned.components(separatedBy: " · ").first?
            .trimmingCharacters(in: .whitespaces) ?? ""
        return first.isEmpty ? nil : first
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

    /// R11.2–R11.5: Check if a session's phase is unobserved (no hook/app-server events received).
    /// The `phase_source` field is carried through the session extras dict on the wire.
    private func isUnobserved(_ session: SessionRow) -> Bool {
        session.extras["phase_source"] == "unobserved"
    }

    private func phaseColor(for session: SessionRow) -> Color {
        PhaseColorTable.resolve(
            session.phase,
            createdAtMs: actionableStartedAtMs(for: session),
            nowMs: nowMs()
        ).foreground
    }

    private func actionableStartedAtMs(for session: SessionRow) -> Int {
        if let approval = approval(for: session),
           approval.createdAtMs > 0 {
            return approval.createdAtMs
        }
        return session.updatedAtMs > 0 ? session.updatedAtMs : session.createdAtMs
    }

    private func approvalUrgencyColor(_ approval: ApprovalRow) -> Color {
        PhaseColorTable.resolve(
            .waitingForApproval,
            createdAtMs: approval.createdAtMs,
            nowMs: nowMs()
        ).foreground
    }

    /// V10 #4: phases where work is actively progressing — show
    /// the AnimatedEllipsis to signal "still alive".
    private func isWorkingPhase(_ phase: SessionRow.Phase) -> Bool {
        switch phase {
        case .running, .thinking, .editing, .runningTool, .testing:
            return true
        case .waitingForApproval, .waitingForAnswer, .completed, .failed, .unknown:
            return false
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
            .font(.system(size: 11.5, weight: .semibold))
            .foregroundStyle(foreground)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(background(configuration.isPressed))
            )
    }

    private var foreground: Color {
        switch kind {
        case .primary: return .white
        case .secondary: return .white.opacity(0.88)
        }
    }

    private func background(_ pressed: Bool) -> Color {
        let pressedFactor: Double = pressed ? 0.78 : 1.0
        switch kind {
        case .primary:
            return Color(red: 0.26, green: 0.45, blue: 0.86).opacity(pressedFactor)
        case .secondary:
            return Color.white.opacity(pressed ? 0.12 : 0.16)
        }
    }
}

private extension Collection {
    /// Bounds-checked subscript so the carousel index can't crash when
    /// the facts list shrinks between ticks.
    subscript(safe index: Index) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}

private extension String {
    var nilIfEmpty: String? {
        isEmpty ? nil : self
    }
}

/// V10 island polish #4 (MioIsland-inspired): animated ellipsis that
/// pulses three dots in/out at staggered phase. Used as the trailing
/// suffix for "working" phase labels (running, thinking, editing,
/// running tool, testing) so the chip feels alive instead of static.
struct AnimatedEllipsis: View {
    var color: Color
    var size: CGFloat = 9
    @State private var phase: Double = 0

    var body: some View {
        HStack(spacing: 1.5) {
            ForEach(0..<3, id: \.self) { i in
                Text(".")
                    .font(.system(size: size, weight: .bold))
                    .foregroundStyle(color)
                    .opacity(opacity(for: i))
            }
        }
        .onAppear {
            withAnimation(.linear(duration: 1.2).repeatForever(autoreverses: false)) {
                phase = 3
            }
        }
    }

    private func opacity(for index: Int) -> Double {
        // Each dot's opacity peaks at a different phase offset.
        let offset = Double(index) * 0.33
        let p = (phase - offset).truncatingRemainder(dividingBy: 1.0)
        let normalized = p < 0 ? p + 1 : p
        // Ease in then out — peak around 0.5.
        return 0.3 + 0.7 * sin(normalized * .pi)
    }
}
