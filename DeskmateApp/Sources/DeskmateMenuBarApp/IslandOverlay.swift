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
    @State private var phasePeekUntil: Date = .distantPast
    @State private var lastPhaseSignature: PhasePeekSignature?
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
    /// Transient width budget for showing full source/phase text in
    /// compact mode. Open/Vibe and Mio keep steady collapsed content
    /// dense; this gives full labels a short readable window without
    /// permanently occupying the menu bar.
    private static let sneakPeekExtraWidth: CGFloat = 160
    private static let phasePeekDuration: TimeInterval = 2.2

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
            updatePhasePeek()
            if phasePeekUntil <= Date(), phasePeekUntil != .distantPast {
                withAnimation(Self.popSpring) {
                    phasePeekUntil = .distantPast
                }
            }
            guard shouldPulseStatusDot else {
                if pulsePhase { pulsePhase = false }
                return
            }
            withAnimation(.easeInOut(duration: 0.9)) {
                pulsePhase.toggle()
            }
        }
        .onAppear {
            updatePhasePeek(allowPeek: false)
        }
    }

    private func notchSurface(availableSize: CGSize) -> some View {
        let geometry = interactionGeometry(availableSize: availableSize)
        let size = geometry.surfaceSize
        // R5.1/R5.7: During SneakPeek, widen the compact surface
        // to give the "peek" visual cue that something arrived.
        let surfaceWidth = (!isExpanded && isSneakPeek) ? size.width + Self.sneakPeekExtraWidth : size.width
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
        let base = isBuildDoneState ? compactSideWidth * 0.75 : compactSideWidth
        return base + compactPeekSideWidth
    }

    private var trailingSideWidth: CGFloat {
        let base = isBuildDoneState ? compactSideWidth * 1.25 : compactSideWidth
        return base + compactPeekSideWidth
    }

    private var idleEdge: some View {
        Color.clear
    }

    private var expandedContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            expandedHeader
            if !agentHealthSummary.isEmpty {
                agentHealthStrip
            }
            if visibleSessions.isEmpty && visibleReminderRows.isEmpty {
                emptyExpandedState
            } else {
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(visibleReminderRows, id: \.reminderId) { reminder in
                            reminderRow(reminder)
                        }
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

    private var agentHealthStrip: some View {
        HStack(spacing: 6) {
            Label(agentHealthSummary.expandedBadgeText, systemImage: "waveform.path.ecg")
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(healthColor.opacity(0.9))
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)

            Text(agentHealthSummary.kindLine.replacingOccurrences(of: "Kinds: ", with: ""))
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.42))
                .lineLimit(1)
                .truncationMode(.tail)

            Spacer(minLength: 0)

            Text(agentHealthSummary.sourceLine.replacingOccurrences(of: "Sources: ", with: ""))
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.34))
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(
            Capsule(style: .continuous)
                .fill(healthColor.opacity(0.08))
        )
        .overlay(
            Capsule(style: .continuous)
                .strokeBorder(healthColor.opacity(0.14), lineWidth: 0.5)
        )
    }

    private func sessionRow(_ session: SessionRow) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                ZStack {
                    Circle()
                        .fill(phaseColor(for: session).opacity(0.16))
                        .frame(width: 24, height: 24)
                    phaseGlyph(for: session)
                }
                .padding(.top, 1)

                VStack(alignment: .leading, spacing: 3) {
                    Text(session.displayTitle)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.92))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(sessionHeaderDetail(session))
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.white.opacity(0.38))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                Spacer(minLength: 4)

                HStack(spacing: 5) {
                    phaseChip(session)

                    Text(sessionAgeLabel(session))
                        .font(.system(size: 9, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.35))
                        .fixedSize(horizontal: true, vertical: false)

                    if session.canAttemptJump {
                        Button {
                            viewModel.jumpToSession(session.sessionId)
                        } label: {
                            Image(systemName: "arrow.up.forward.app")
                                .font(.system(size: 10, weight: .semibold))
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(.white.opacity(0.5))
                        .help("Jump to session")
                    }
                }
            }

            VStack(alignment: .leading, spacing: 5) {
                ForEach(sessionCockpitLines(session), id: \.id) { line in
                    sessionCockpitLine(line)
                }
            }

            let chips = sessionMetaChips(session)
            if !chips.isEmpty {
                HStack(spacing: 5) {
                    ForEach(chips.prefix(4), id: \.self) { chip in
                        sessionMetaChip(chip)
                    }
                }
            }

            if let approval = approval(for: session) {
                approvalInline(session: session, approval: approval)
            } else if session.phase == .waitingForAnswer {
                questionInline(session: session)
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

    private func reminderRow(_ reminder: ReminderRow) -> some View {
        HStack(alignment: .top, spacing: 10) {
            ZStack {
                Circle()
                    .fill(reminderColor(for: reminder).opacity(0.16))
                    .frame(width: 24, height: 24)
                Image(systemName: reminderIcon(for: reminder))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(reminderColor(for: reminder))
            }
            .padding(.top, 1)

            VStack(alignment: .leading, spacing: 4) {
                Text(reminder.text.isEmpty ? "Reminder" : reminder.text)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.92))
                    .lineLimit(2)
                    .truncationMode(.tail)
                Text(reminderDueLabel(reminder))
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.44))
                    .lineLimit(1)
                    .truncationMode(.tail)
            }

            Spacer(minLength: 4)

            Text(reminderStatusLabel(reminder).uppercased())
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(reminderColor(for: reminder))
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(
                    Capsule(style: .continuous)
                        .fill(reminderColor(for: reminder).opacity(0.12))
                )
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color.white.opacity(reminder.status == .fired ? 0.06 : 0.03))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .strokeBorder(reminderColor(for: reminder).opacity(0.18), lineWidth: 0.5)
        )
    }

    @ViewBuilder
    private func phaseChip(_ session: SessionRow) -> some View {
        HStack(spacing: 2) {
            Text(session.phaseLabel)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(
                    isUnobserved(session)
                        ? phaseColor(for: session).opacity(0.5)
                        : phaseColor(for: session)
                )
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
            if isUnobserved(session) {
                Text("?")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(phaseColor(for: session).opacity(0.5))
                    .fixedSize(horizontal: true, vertical: false)
            } else if isWorkingPhase(session.phase) {
                AnimatedEllipsis(
                    color: phaseColor(for: session),
                    size: 9
                )
                .fixedSize(horizontal: true, vertical: false)
            }
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 3)
        .background(Capsule().fill(phaseColor(for: session).opacity(isUnobserved(session) ? 0.06 : 0.12)))
        .layoutPriority(2)
    }

    private struct CockpitLine: Hashable {
        let id: String
        let label: String
        let text: String
        let icon: String
    }

    private func sessionCockpitLines(_ session: SessionRow) -> [CockpitLine] {
        var lines: [CockpitLine] = []
        lines.append(CockpitLine(
            id: "activity",
            label: sessionActivityLabel(session),
            text: sessionActivityText(session),
            icon: sessionActivityIcon(session)
        ))
        if let outcome = session.recentOutcomeLine {
            lines.append(CockpitLine(id: "recent", label: "LAST", text: outcome, icon: "clock"))
        }
        if let prompt = session.promptText {
            lines.append(CockpitLine(id: "prompt", label: "YOU", text: prompt, icon: "person.fill"))
        }
        if let assistant = session.assistantText {
            lines.append(CockpitLine(id: "assistant", label: "AI", text: assistant, icon: "sparkles"))
        }
        return Array(lines.prefix(3))
    }

    private func sessionCockpitLine(_ line: CockpitLine) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: line.icon)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.white.opacity(0.32))
                .frame(width: 12)
            Text(line.label)
                .font(.system(size: 8, weight: .bold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.32))
                .frame(width: 34, alignment: .leading)
            Text(line.text)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.white.opacity(0.62))
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private func sessionMetaChip(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 8.5, weight: .semibold, design: .monospaced))
            .foregroundStyle(.white.opacity(0.45))
            .lineLimit(1)
            .minimumScaleFactor(0.7)
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background(Capsule().fill(Color.white.opacity(0.06)))
    }

    private func approvalInline(session: SessionRow, approval: ApprovalRow) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 4) {
                Text(approvalTitle(session: session, approval: approval))
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.88))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                if let detail = approval.detailLine {
                    Text(detail)
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.54))
                        .lineLimit(2)
                        .truncationMode(.middle)
                }
            }
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
                compactStatusGlyph(color: color)
            }
            VStack(alignment: alignment, spacing: 1) {
                Text(title)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.88))
                    .lineLimit(1)
                    .minimumScaleFactor(0.68)
                    .allowsTightening(true)
                if let subtitle {
                    Text(subtitle)
                        .font(.system(size: 8, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.42))
                        .lineLimit(1)
                        .minimumScaleFactor(0.62)
                        .allowsTightening(true)
                }
            }
            if alignment == .trailing {
                compactStatusGlyph(color: color)
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
            } else if shouldShowFullCompactLabels {
                compactPeekTrailingModule
            } else if let reminder = activeReminder {
                reminderCompactTrailing(reminder)
            } else if let session = focusSession,
                      shouldPrioritizePhaseInCompact(session) {
                compactPhasePill(session)
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
            } else if let task = activeTask {
                taskCompactTrailing(task)
            } else {
                Text(compactSourceLabel)
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.68))
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 8)
    }

    @ViewBuilder
    private var compactPeekTrailingModule: some View {
        if let notification = activeNotification {
            compactPeekText(
                icon: notificationSymbol(for: notification),
                primary: notificationPeekPrimary(notification),
                secondary: notificationPeekSecondary(notification),
                color: .yellow
            )
        } else if let reminder = activeReminder {
            reminderCompactTrailing(reminder)
        } else if let session = focusSession {
            compactPeekText(
                icon: sessionActivityIcon(session),
                primary: sessionPeekPrimary(session),
                secondary: sessionPeekSecondary(session),
                color: phaseColor(for: session)
            )
        } else if activeSessionCount > 0 {
            trailingCarousel
        } else if let task = activeTask {
            taskCompactTrailing(task)
        } else {
            Text(compactSourceLabel)
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.68))
                .lineLimit(1)
        }
    }

    private func taskCompactTrailing(_ task: TaskRow) -> some View {
        compactPeekText(
            icon: task.status == .inProgress ? "play.fill" : "circle",
            primary: taskCompactPrimary(task),
            secondary: taskCompactSecondary(task),
            color: .cyan
        )
    }

    private func reminderCompactTrailing(_ reminder: ReminderRow) -> some View {
        compactPeekText(
            icon: reminderIcon(for: reminder),
            primary: reminderCompactPrimary(reminder),
            secondary: reminderCompactSecondary(reminder),
            color: reminderColor(for: reminder)
        )
    }

    private func reminderCompactPrimary(_ reminder: ReminderRow) -> String {
        let text = reminder.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { return reminderStatusLabel(reminder).uppercased() }
        return truncateForPill(text.uppercased(), maxChars: 20)
    }

    private func reminderCompactSecondary(_ reminder: ReminderRow) -> String? {
        truncateForPill(reminderDueLabel(reminder), maxChars: 24)
    }

    private func taskCompactPrimary(_ task: TaskRow) -> String {
        if let step = task.currentStepLine?
            .replacingOccurrences(of: "step: ", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !step.isEmpty {
            return truncateForPill(step.uppercased(), maxChars: 20)
        }
        return truncateForPill(task.displayTitle.uppercased(), maxChars: 20)
    }

    private func taskCompactSecondary(_ task: TaskRow) -> String? {
        let label = task.status == .inProgress ? "doing" : "open"
        let progress = task.stepProgressLabel.map { " · \($0)" } ?? ""
        let title = task.displayTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        if title.isEmpty { return truncateForPill("\(label)\(progress)", maxChars: 24) }
        return truncateForPill("\(label)\(progress) · \(title)", maxChars: 24)
    }

    private func compactPeekText(
        icon: String,
        primary: String,
        secondary: String?,
        color: Color
    ) -> some View {
        HStack(spacing: 5) {
            Image(systemName: icon)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(color.opacity(0.9))
                .frame(width: 12)
            VStack(alignment: .trailing, spacing: 1) {
                Text(primary)
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.9))
                    .lineLimit(1)
                    .minimumScaleFactor(0.54)
                    .truncationMode(.middle)
                    .allowsTightening(true)
                if let secondary {
                    Text(secondary)
                        .font(.system(size: 8, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.44))
                        .lineLimit(1)
                        .minimumScaleFactor(0.58)
                        .truncationMode(.middle)
                        .allowsTightening(true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
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

        if shouldShowFullCompactLabels {
            facts.append((fullPhaseLabel(for: session).uppercased(), fullSourceName(session.sourceLabel ?? session.source ?? "agent")))
            if let detail = runtime.island?.state.detail, !detail.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                facts.append((truncateForPill(detail, maxChars: 22).uppercased(), sessionAgeLabel(session)))
            } else {
                facts.append((truncateForPill(session.displayTitle, maxChars: 22).uppercased(), sessionAgeLabel(session)))
            }
            return Array(facts.prefix(2))
        }

        let title = truncateForPill(session.displayTitle, maxChars: 12)
        if !title.isEmpty {
            facts.append((title.uppercased(), shortWorkspace(for: session).nilIfEmpty))
        }

        facts.append((compactPhaseLabel(for: session), sessionAgeLabel(session)))

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
            VStack(alignment: .trailing, spacing: 1) {
                Text(fact.primary)
                    .font(.system(size: shouldShowFullCompactLabels ? 9 : 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.9))
                    .lineLimit(1)
                    .minimumScaleFactor(shouldShowFullCompactLabels ? 0.52 : 0.62)
                    .allowsTightening(true)
                if let sub = fact.secondary {
                    Text(sub)
                        .font(.system(size: 8, weight: .medium, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.42))
                        .lineLimit(1)
                        .minimumScaleFactor(0.62)
                        .allowsTightening(true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
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

    @ViewBuilder
    private func compactStatusGlyph(color: Color) -> some View {
        if let phase = focusSession?.phase,
           let preset = MatrixLoaderPreset(phase: phase) {
            MatrixStatusLoader(preset: preset)
                .frame(width: 17, height: 17)
        } else {
            pixelAvatar(color: color)
        }
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

    private var compactPeekSideWidth: CGFloat {
        (!isExpanded && isSneakPeek) ? Self.sneakPeekExtraWidth / 2 : 0
    }

    private var shouldShowCompactContent: Bool {
        hasCompactPresence || islandScreen?.deskmateHasPhysicalNotch != true
    }

    private var hasCompactPresence: Bool {
        runtime.bridgeState != .connected
            || activeIslandKindRequiresCompactPresence
            || !runtime.sessions.isEmpty
            || !runtime.approvals.isEmpty
            || activeReminder != nil
            || activeTask != nil
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

    private var visibleReminderRows: [ReminderRow] {
        ReminderListAdapter(maxRows: 3)
            .display(reminders: runtime.reminders, nowMs: nowMs())
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

    private var activeTask: TaskRow? {
        viewModel.content.activeTask
    }

    private var activeReminder: ReminderRow? {
        viewModel.content.activeReminder
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
            if let reminder = activeReminder {
                return reminderColor(for: reminder)
            }
            if let session = focusSession { return phaseColor(for: session) }
            if activeTask != nil { return .cyan }
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
        if let reminder = activeReminder {
            return reminder.status == .fired
                || (reminder.status == .pending && reminder.dueAtMs <= nowMs())
        }
        if let session = focusSession {
            return session.phase != .completed
                && session.phase != .failed
                && session.phase != .unknown
        }
        if activeTask?.status == .inProgress { return true }
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
        visibleSessions.count
    }

    private var agentHealthSummary: AgentHealthSummary {
        AgentHealthSummary(sessions: runtime.sessions)
    }

    private var healthColor: Color {
        if agentHealthSummary.awaitingAction > 0 { return .orange }
        if agentHealthSummary.unobserved > 0 { return .yellow }
        if agentHealthSummary.hookSessions > 0 { return .green }
        return .cyan
    }

    private var headerTitle: String {
        if !runtime.approvals.isEmpty { return "Action needed" }
        if activeReminder != nil { return "Reminders" }
        if activeSessionCount > 0 { return "Agent sessions" }
        if activeTask != nil { return "Active task" }
        return "Deskmate Island"
    }

    private var compactLeadingTitle: String {
        if runtime.bridgeState != .connected { return "OFF" }
        if let descriptor = activeModuleDescriptor,
           activeIslandKindRequiresCompactPresence {
            return descriptor.title
        }
        if activeReminder != nil { return "REM" }
        if let session = focusSession {
            let source = session.sourceLabel ?? session.source ?? "agent"
            return shouldShowFullCompactLabels ? fullSourceName(source).uppercased() : compactSourceCode(source)
        }
        if activeTask != nil { return "TASK" }
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
        if let reminder = activeReminder {
            return reminderStatusLabel(reminder).lowercased()
        }
        if let session = focusSession {
            return shouldShowFullCompactLabels
                ? fullPhaseLabel(for: session).lowercased()
                : compactPhaseLabel(for: session).lowercased()
        }
        if let task = activeTask {
            return task.status == .inProgress ? "doing" : "open"
        }
        return nil
    }

    private var shouldShowFullCompactLabels: Bool {
        !isExpanded && (isSneakPeek || isPhasePeekActive)
    }

    private var isPhasePeekActive: Bool {
        phasePeekUntil > Date()
    }

    private func compactPhaseLabel(for session: SessionRow) -> String {
        switch session.phase {
        case .waitingForApproval:
            return "ASK"
        case .waitingForAnswer:
            return "Q"
        case .runningTool:
            return "TOOL"
        case .thinking:
            return "PLAN"
        case .editing:
            return "EDIT"
        case .testing:
            return "TEST"
        case .running:
            return "RUN"
        case .completed:
            return "DONE"
        case .failed:
            return "FAIL"
        case .unknown:
            return "IDE"
        }
    }

    private func shouldPrioritizePhaseInCompact(_ session: SessionRow) -> Bool {
        switch session.phase {
        case .thinking, .editing, .runningTool, .testing, .completed, .failed,
             .waitingForApproval, .waitingForAnswer:
            return true
        case .running, .unknown:
            return false
        }
    }

    private func compactPhasePill(_ session: SessionRow) -> some View {
        HStack(spacing: 4) {
            phaseGlyph(for: session)
                .frame(width: 12)
            Text(compactPhaseLabel(for: session))
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.9))
                .lineLimit(1)
                .minimumScaleFactor(0.68)
            if isWorkingPhase(session.phase) {
                AnimatedEllipsis(color: phaseColor(for: session), size: 7)
                    .frame(width: 12)
            }
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(
            Capsule(style: .continuous)
                .fill(phaseColor(for: session).opacity(0.16))
        )
        .overlay(
            Capsule(style: .continuous)
                .stroke(phaseColor(for: session).opacity(0.35), lineWidth: 0.5)
        )
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
        if activeReminder != nil { return "rem" }
        if let session = focusSession {
            return compactSourceCode(session.sourceLabel ?? session.source ?? "agent")
        }
        if activeTask != nil { return "task" }
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

    private func notificationPeekPrimary(_ state: IslandSurfaceState) -> String {
        if let descriptor = moduleRegistry.renderDescriptor(for: state) {
            let title = descriptor.title.trimmingCharacters(in: .whitespacesAndNewlines)
            if !title.isEmpty { return truncateForPill(title.uppercased(), maxChars: 18) }
        }
        let title = notificationTitle(for: state).trimmingCharacters(in: .whitespacesAndNewlines)
        return title.isEmpty ? "NOTICE" : truncateForPill(title.uppercased(), maxChars: 18)
    }

    private func notificationPeekSecondary(_ state: IslandSurfaceState) -> String? {
        if let descriptor = moduleRegistry.renderDescriptor(for: state),
           let subtitle = descriptor.subtitle?.trimmingCharacters(in: .whitespacesAndNewlines),
           !subtitle.isEmpty {
            return truncateForPill(subtitle, maxChars: 22)
        }
        if let detail = state.detail?.trimmingCharacters(in: .whitespacesAndNewlines),
           !detail.isEmpty {
            return truncateForPill(detail, maxChars: 22)
        }
        if let id = state.activityId?.trimmingCharacters(in: .whitespacesAndNewlines),
           !id.isEmpty {
            return truncateForPill(id, maxChars: 22)
        }
        return nil
    }

    private func sessionPeekPrimary(_ session: SessionRow) -> String {
        let activity = sessionActivityText(session)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !activity.isEmpty && activity != session.phaseLabel {
            return truncateForPill(activity.uppercased(), maxChars: 20)
        }
        return truncateForPill(session.displayTitle.uppercased(), maxChars: 20)
    }

    private func sessionPeekSecondary(_ session: SessionRow) -> String? {
        let pieces = [
            fullPhaseLabel(for: session),
            session.sourceLabel ?? session.source,
            shortWorkspace(for: session).nilIfEmpty,
        ].compactMap { value -> String? in
            guard let value,
                  !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { return nil }
            return value
        }
        guard !pieces.isEmpty else { return nil }
        return truncateForPill(pieces.joined(separator: " · "), maxChars: 24)
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

    private func reminderColor(for reminder: ReminderRow) -> Color {
        switch reminder.status {
        case .fired:
            return .orange
        case .pending:
            return reminder.dueAtMs <= nowMs() ? .orange : .yellow
        case .dismissed, .cancelled:
            return .gray
        case .unknown:
            return .yellow
        }
    }

    private func reminderIcon(for reminder: ReminderRow) -> String {
        switch reminder.status {
        case .fired:
            return "bell.badge.fill"
        case .pending:
            return reminder.dueAtMs <= nowMs() ? "bell.badge.fill" : "bell.fill"
        case .dismissed:
            return "checkmark.circle.fill"
        case .cancelled:
            return "xmark.circle.fill"
        case .unknown:
            return "bell"
        }
    }

    private func reminderStatusLabel(_ reminder: ReminderRow) -> String {
        switch reminder.status {
        case .fired:
            return "due"
        case .pending:
            return reminder.dueAtMs <= nowMs() ? "due" : "next"
        case .dismissed:
            return "done"
        case .cancelled:
            return "cancelled"
        case .unknown:
            return "reminder"
        }
    }

    private func reminderDueLabel(_ reminder: ReminderRow) -> String {
        let due = reminder.dueAtMs
        guard due > 0 else { return reminderStatusLabel(reminder) }
        let delta = due - nowMs()
        if reminder.status == .fired || delta <= 0 {
            let overdue = abs(delta)
            if overdue < 60_000 { return "due now" }
            let minutes = overdue / 60_000
            if minutes < 60 { return "due \(minutes)m ago" }
            return "due \(minutes / 60)h ago"
        }
        if delta < 60_000 { return "in <1m" }
        let minutes = delta / 60_000
        if minutes < 60 { return "in \(minutes)m" }
        let hours = minutes / 60
        if hours < 24 { return "in \(hours)h" }
        return "in \(hours / 24)d"
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

    private func sessionHeaderDetail(_ session: SessionRow) -> String {
        let source = session.sourceLabel ?? "Agent"
        let workspace = session.workspaceName
        let kind = session.kindLabel
        let pieces = [source, workspace, kind]
            .compactMap { piece -> String? in
                guard let piece,
                      !piece.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                else { return nil }
                return piece
            }
        return pieces.isEmpty ? session.sessionId : pieces.joined(separator: " · ")
    }

    private func sessionActivityLine(_ session: SessionRow) -> String {
        session.activityLine
    }

    private func sessionActivityLabel(_ session: SessionRow) -> String {
        if session.command != nil { return "CMD" }
        if session.filePath != nil { return "FILE" }
        if session.toolAction != nil { return "TOOL" }
        if session.windowTitle != nil { return "WIN" }
        return "NOW"
    }

    private func sessionActivityText(_ session: SessionRow) -> String {
        if let command = session.command {
            return command
        }
        if let filePath = session.filePath {
            return URL(fileURLWithPath: filePath).lastPathComponent
        }
        if let toolAction = session.toolAction {
            if let target = session.toolTarget {
                return "\(toolAction) -> \(target)"
            }
            if let outcome = session.toolOutcome {
                return "\(toolAction) -> \(outcome)"
            }
            return toolAction
        }
        if let windowTitle = session.windowTitle {
            return windowTitle
        }
        let summary = session.summary.trimmingCharacters(in: .whitespacesAndNewlines)
        if !summary.isEmpty { return summary }
        return session.phaseLabel
    }

    private func sessionActivityIcon(_ session: SessionRow) -> String {
        if session.command != nil { return "terminal" }
        if session.filePath != nil { return "doc.text" }
        if session.toolAction != nil { return "wrench.and.screwdriver" }
        if session.windowTitle != nil { return "macwindow" }
        switch session.phase {
        case .waitingForApproval: return "exclamationmark.triangle"
        case .waitingForAnswer: return "questionmark.circle"
        case .thinking: return "brain.head.profile"
        case .editing: return "pencil"
        case .runningTool: return "terminal"
        case .testing: return "checkmark.seal"
        case .failed: return "xmark.octagon"
        case .completed: return "checkmark.circle"
        case .running, .unknown: return "bolt"
        }
    }

    private func sessionMetaChips(_ session: SessionRow) -> [String] {
        var chips: [String] = []
        if let branch = session.branchName {
            chips.append("git \(truncateForPill(branch, maxChars: 18))")
        }
        if let cwd = session.cwd,
           !cwd.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            chips.append(URL(fileURLWithPath: cwd).lastPathComponent)
        }
        if let source = session.sourceLabel {
            chips.append(source)
        }
        if let kind = session.kindLabel {
            chips.append(kind)
        }
        if let pid = session.processId {
            chips.append("pid \(pid)")
        }
        if let phaseSource = session.phaseSource {
            chips.append(phaseSource == "unobserved" ? "unobserved" : phaseSource)
        }
        return uniqueNonEmpty(chips)
    }

    private func uniqueNonEmpty(_ values: [String]) -> [String] {
        var seen: Set<String> = []
        var out: [String] = []
        for value in values {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }
            let key = trimmed.lowercased()
            guard !seen.contains(key) else { continue }
            seen.insert(key)
            out.append(trimmed)
        }
        return out
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

    private func compactSourceCode(_ source: String) -> String {
        let normalized = source
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: " ", with: "_")
            .replacingOccurrences(of: "-", with: "_")
        if normalized.contains("codex") { return "CX" }
        if normalized.contains("claude") { return "CL" }
        if normalized.contains("cursor") { return "CU" }
        if normalized.contains("windsurf") || normalized.contains("winsurf") { return "WS" }
        if normalized.contains("vscode") || normalized.contains("visual_studio_code") { return "VS" }
        if normalized.contains("xcode") { return "XC" }
        if normalized.contains("jetbrains") || normalized.contains("intellij") { return "JB" }
        if normalized.contains("terminal") || normalized.contains("iterm") || normalized.contains("ghostty") { return "SH" }
        let letters = normalized.filter { $0.isLetter || $0.isNumber }
        if letters.isEmpty { return "AG" }
        return String(letters.prefix(2)).uppercased()
    }

    private func fullSourceName(_ source: String) -> String {
        let normalized = source
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: " ", with: "_")
            .replacingOccurrences(of: "-", with: "_")
        if normalized.contains("codex") { return "Codex" }
        if normalized.contains("claude") { return "Claude" }
        if normalized.contains("cursor") { return "Cursor" }
        if normalized.contains("windsurf") || normalized.contains("winsurf") { return "Windsurf" }
        if normalized.contains("vscode") || normalized.contains("visual_studio_code") { return "VSCode" }
        if normalized.contains("xcode") { return "Xcode" }
        if normalized.contains("jetbrains") || normalized.contains("intellij") { return "JetBrains" }
        if normalized.contains("terminal") { return "Terminal" }
        if normalized.contains("iterm") { return "iTerm" }
        if normalized.contains("ghostty") { return "Ghostty" }
        let cleaned = source.trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty ? "Agent" : sourceShortName(cleaned)
    }

    private func fullPhaseLabel(for session: SessionRow) -> String {
        switch session.phase {
        case .waitingForApproval:
            return "approval"
        case .waitingForAnswer:
            return "answer"
        case .runningTool:
            return "running tool"
        case .thinking:
            return "thinking"
        case .editing:
            return "editing"
        case .testing:
            return "testing"
        case .running:
            return "running"
        case .completed:
            return "completed"
        case .failed:
            return "failed"
        case .unknown:
            return "ide"
        }
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

    private func updatePhasePeek(allowPeek: Bool = true) {
        guard let session = focusSession else {
            lastPhaseSignature = nil
            phasePeekUntil = .distantPast
            return
        }
        let signature = PhasePeekSignature(session: session)
        defer { lastPhaseSignature = signature }
        guard allowPeek,
              !isExpanded,
              signature != lastPhaseSignature,
              shouldPeekPhaseChange(signature)
        else { return }
        withAnimation(Self.popSpring) {
            phasePeekUntil = Date().addingTimeInterval(Self.phasePeekDuration)
        }
    }

    private func shouldPeekPhaseChange(_ signature: PhasePeekSignature) -> Bool {
        guard runtime.degradationPolicy.level < 4 else { return false }
        switch signature.phase {
        case .thinking, .editing, .runningTool, .testing, .completed, .failed,
             .waitingForApproval, .waitingForAnswer:
            return true
        case .running, .unknown:
            return false
        }
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

private struct PhasePeekSignature: Equatable {
    let sessionId: String
    let phase: SessionRow.Phase
    let updatedAtMs: Int

    init(session: SessionRow) {
        self.sessionId = session.sessionId
        self.phase = session.phase
        self.updatedAtMs = session.updatedAtMs
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

private enum MatrixLoaderPreset {
    case thinking
    case editing
    case runningTool
    case testing
    case completed
    case failed

    init?(phase: SessionRow.Phase) {
        switch phase {
        case .thinking:
            self = .thinking
        case .editing:
            self = .editing
        case .runningTool:
            self = .runningTool
        case .running:
            self = .editing
        case .testing:
            self = .testing
        case .completed:
            self = .completed
        case .failed:
            self = .failed
        case .waitingForApproval, .waitingForAnswer, .unknown:
            return nil
        }
    }

    var onColor: Color {
        switch self {
        case .thinking:
            return Color(red: 0.42, green: 0.71, blue: 1.0)
        case .editing:
            return Color(red: 0.086, green: 0.824, blue: 0.471)
        case .runningTool:
            return Color(red: 1.0, green: 0.749, blue: 0.424)
        case .testing:
            return Color(red: 0.843, green: 0.855, blue: 0.282)
        case .completed:
            return Color(red: 0.290, green: 0.871, blue: 0.502)
        case .failed:
            return Color(red: 0.973, green: 0.443, blue: 0.443)
        }
    }

    var offColor: Color {
        switch self {
        case .thinking:
            return Color(red: 0.063, green: 0.106, blue: 0.149)
        case .editing:
            return Color(red: 0.012, green: 0.125, blue: 0.071)
        case .runningTool:
            return Color(red: 0.149, green: 0.114, blue: 0.063)
        case .testing:
            return Color(red: 0.125, green: 0.129, blue: 0.043)
        case .completed:
            return Color(red: 0.063, green: 0.106, blue: 0.149)
        case .failed:
            return Color(red: 0.149, green: 0.031, blue: 0.031)
        }
    }

    var fps: Double {
        switch self {
        case .thinking:
            return 13
        case .editing, .runningTool:
            return 24
        case .testing:
            return 6
        case .completed:
            return 13
        case .failed:
            return 13
        }
    }

    var loops: Bool {
        switch self {
        case .completed, .failed:
            return false
        case .thinking, .editing, .runningTool, .testing:
            return true
        }
    }

    var frames: [[[Double]]] {
        switch self {
        case .thinking:
            return Self.thinkingFrames
        case .editing:
            return Self.editingFrames
        case .runningTool:
            return Self.runningToolFrames
        case .testing:
            return Self.testingFrames
        case .completed:
            return Self.completedFrames
        case .failed:
            return Self.failedFrames
        }
    }

    private static let thinkingFrames: [[[Double]]] = [
        [[1, 0, 0, 0], [0.825, 0, 0, 0], [0.65, 0, 0, 0], [0.475, 0, 0, 0]],
        [[0.825, 1, 0, 0], [0.65, 0, 0, 0], [0.475, 0, 0, 0], [0, 0, 0, 0]],
        [[0.65, 0.825, 1, 0], [0.475, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        [[0.475, 0.65, 0.825, 1], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 0.475, 0.65, 0.825], [0, 0, 0, 1], [0, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 0, 0.475, 0.65], [0, 0, 0, 0.825], [0, 0, 0, 1], [0, 0, 0, 0]],
        [[0, 0, 0, 0.475], [0, 0, 0, 0.65], [0, 0, 0, 0.825], [0, 0, 0, 1]],
        [[0, 0, 0, 0], [0, 0, 0, 0.475], [0, 0, 0, 0.65], [0, 0, 1, 0.825]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0.475], [0, 1, 0.825, 0.65]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [1, 0.825, 0.65, 0.475]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [1, 0, 0, 0], [0.825, 0.65, 0.475, 0]],
        [[0, 0, 0, 0], [1, 0, 0, 0], [0.825, 0, 0, 0], [0.65, 0.475, 0, 0]]
    ]

    private static let editingFrames: [[[Double]]] = [
        [[0.5, 0.8586780454, 0.9997868015, 0.8377315903], [0.8586780454, 0.9997868015, 0.8377315903, 0.4708129283], [0.9997868015, 0.8377315903, 0.4708129283, 0.1215987523], [0.8377315903, 0.4708129283, 0.1215987523, 0.0019176956]],
        [[0.3705904774, 0.7562959048, 0.9865356755, 0.9216494341], [0.7562959048, 0.9865356755, 0.9216494341, 0.6009963039], [0.9865356755, 0.9216494341, 0.6009963039, 0.2190801711], [0.9216494341, 0.6009963039, 0.2190801711, 0.0075662369]],
        [[0.25, 0.6364476218, 0.9401279472, 0.9768325657], [0.6364476218, 0.9401279472, 0.9768325657, 0.7242969484], [0.9401279472, 0.9768325657, 0.7242969484, 0.3357058119], [0.9768325657, 0.7242969484, 0.3357058119, 0.0467733253]],
        [[0.1464466094, 0.5073006589, 0.8637262266, 0.999520346], [0.5073006589, 0.8637262266, 0.999520346, 0.8323121265], [0.8637262266, 0.999520346, 0.8323121265, 0.4635278302], [0.999520346, 0.8323121265, 0.4635278302, 0.1168670627]],
        [[0.0669872981, 0.3776561681, 0.7625371648, 0.9881666403], [0.3776561681, 0.7625371648, 0.9881666403, 0.9176807823], [0.7625371648, 0.9881666403, 0.9176807823, 0.5938353665], [0.9881666403, 0.9176807823, 0.5938353665, 0.2130706766]],
        [[0.0170370869, 0.2563492073, 0.6434566291, 0.9435451847], [0.2563492073, 0.6434566291, 0.9435451847, 0.9745851831], [0.6434566291, 0.9435451847, 0.9745851831, 0.7177481777], [0.9435451847, 0.9745851831, 0.7177481777, 0.3288280496]],
        [[0, 0.1516466453, 0.5145997612, 0.8686968578], [0.1516466453, 0.5145997612, 0.8686968578, 0.9991473879], [0.5145997612, 0.8686968578, 0.9991473879, 0.8268218104], [0.8686968578, 0.9991473879, 0.8268218104, 0.4562505083]],
        [[0.0170370869, 0.0706837888, 0.3847479436, 0.7687224493], [0.0706837888, 0.3847479436, 0.7687224493, 0.9896935231], [0.3847479436, 0.7687224493, 0.9896935231, 0.9136230769], [0.7687224493, 0.9896935231, 0.9136230769, 0.5866544225]],
        [[0.0669872981, 0.0189781226, 0.2627503633, 0.65043505], [0.0189781226, 0.2627503633, 0.65043505, 0.946867854], [0.2627503633, 0.65043505, 0.946867854, 0.9722366142], [0.65043505, 0.946867854, 0.9722366142, 0.711152981]],
        [[0.1464466094, 0.0000533025, 0.1569209536, 0.5218957506], [0.0000533025, 0.1569209536, 0.5218957506, 0.8735888791], [0.1569209536, 0.5218957506, 0.8735888791, 0.9986680066], [0.5218957506, 0.8735888791, 0.9986680066, 0.8212618128]],
        [[0.25, 0.0151990235, 0.074471814, 0.391864292], [0.0151990235, 0.074471814, 0.391864292, 0.7748504395], [0.074471814, 0.391864292, 0.7748504395, 0.9911159985], [0.391864292, 0.7748504395, 0.9911159985, 0.9094771829]],
        [[0.3705904774, 0.06338313, 0.021021717, 0.2692021033], [0.06338313, 0.021021717, 0.2692021033, 0.6573813967], [0.021021717, 0.2692021033, 0.6573813967, 0.9500952467], [0.2692021033, 0.6573813967, 0.9500952467, 0.9697873598]],
        [[0.5, 0.1413219546, 0.0002131985, 0.1622684097], [0.1413219546, 0.0002131985, 0.1622684097, 0.5291870717], [0.0002131985, 0.1622684097, 0.5291870717, 0.8784012477], [0.1622684097, 0.5291870717, 0.8784012477, 0.9980823044]],
        [[0.6294095226, 0.2437040952, 0.0134643245, 0.0783505659], [0.2437040952, 0.0134643245, 0.0783505659, 0.3990036961], [0.0134643245, 0.0783505659, 0.3990036961, 0.7809198289], [0.0783505659, 0.3990036961, 0.7809198289, 0.9924337631]],
        [[0.75, 0.3635523782, 0.0598720528, 0.0231674343], [0.3635523782, 0.0598720528, 0.0231674343, 0.2757030516], [0.0598720528, 0.0231674343, 0.2757030516, 0.6642941881], [0.0231674343, 0.2757030516, 0.6642941881, 0.9532266747]],
        [[0.8535533906, 0.4926993411, 0.1362737734, 0.000479654], [0.4926993411, 0.1362737734, 0.000479654, 0.1676878735], [0.1362737734, 0.000479654, 0.1676878735, 0.5364721698], [0.000479654, 0.1676878735, 0.5364721698, 0.8831329373]],
        [[0.9330127019, 0.6223438319, 0.2374628352, 0.0118333597], [0.6223438319, 0.2374628352, 0.0118333597, 0.0823192177], [0.2374628352, 0.0118333597, 0.0823192177, 0.4061646335], [0.0118333597, 0.0823192177, 0.4061646335, 0.7869293234]],
        [[0.9829629131, 0.7436507927, 0.3565433709, 0.0564548153], [0.7436507927, 0.3565433709, 0.0564548153, 0.0254148169], [0.3565433709, 0.0564548153, 0.0254148169, 0.2822518223], [0.0564548153, 0.0254148169, 0.2822518223, 0.6711719504]],
        [[1, 0.8483533547, 0.4854002388, 0.1313031422], [0.8483533547, 0.4854002388, 0.1313031422, 0.0008526121], [0.4854002388, 0.1313031422, 0.0008526121, 0.1731781896], [0.1313031422, 0.0008526121, 0.1731781896, 0.5437494917]],
        [[0.9829629131, 0.9293162112, 0.6152520564, 0.2312775507], [0.9293162112, 0.6152520564, 0.2312775507, 0.0103064769], [0.6152520564, 0.2312775507, 0.0103064769, 0.0863769231], [0.2312775507, 0.0103064769, 0.0863769231, 0.4133455775]],
        [[0.9330127019, 0.9810218774, 0.7372496367, 0.34956495], [0.9810218774, 0.7372496367, 0.34956495, 0.053132146], [0.7372496367, 0.34956495, 0.053132146, 0.0277633858], [0.34956495, 0.053132146, 0.0277633858, 0.288847019]],
        [[0.8535533906, 0.9999466975, 0.8430790464, 0.4781042494], [0.9999466975, 0.8430790464, 0.4781042494, 0.1264111209], [0.8430790464, 0.4781042494, 0.1264111209, 0.0013319934], [0.4781042494, 0.1264111209, 0.0013319934, 0.1787381872]],
        [[0.75, 0.9848009765, 0.925528186, 0.608135708], [0.9848009765, 0.925528186, 0.608135708, 0.2251495605], [0.925528186, 0.608135708, 0.2251495605, 0.0088840015], [0.608135708, 0.2251495605, 0.0088840015, 0.0905228171]],
        [[0.6294095226, 0.93661687, 0.978978283, 0.7307978967], [0.93661687, 0.978978283, 0.7307978967, 0.3426186033], [0.978978283, 0.7307978967, 0.3426186033, 0.0499047533], [0.7307978967, 0.3426186033, 0.0499047533, 0.0302126402]]
    ]

    private static let runningToolFrames: [[[Double]]] = [
        [[0.4798115587, 0.8480261248, 0.8480261248, 0.4798115587], [0.8480261248, 0.9363390159, 0.9363390159, 0.8480261248], [0.8480261248, 0.9363390159, 0.9363390159, 0.8480261248], [0.4798115587, 0.8480261248, 0.8480261248, 0.4798115587]],
        [[0.6098034549, 0.9290823118, 0.9290823118, 0.6098034549], [0.9290823118, 0.8582809631, 0.8582809631, 0.9290823118], [0.9290823118, 0.8582809631, 0.8582809631, 0.9290823118], [0.6098034549, 0.9290823118, 0.9290823118, 0.6098034549]],
        [[0.7323124272, 0.9808972483, 0.9808972483, 0.7323124272], [0.9808972483, 0.7558066547, 0.7558066547, 0.9808972483], [0.9808972483, 0.7558066547, 0.7558066547, 0.9808972483], [0.7323124272, 0.9808972483, 0.9808972483, 0.7323124272]],
        [[0.8389896915, 0.9999398321, 0.9999398321, 0.8389896915], [0.9999398321, 0.6358995456, 0.6358995456, 0.9999398321], [0.9999398321, 0.6358995456, 0.6358995456, 0.9999398321], [0.8389896915, 0.9999398321, 0.9999398321, 0.8389896915]],
        [[0.9225653685, 0.9849123425, 0.9849123425, 0.9225653685], [0.9849123425, 0.506731107, 0.506731107, 0.9849123425], [0.9849123425, 0.506731107, 0.506731107, 0.9849123425], [0.9225653685, 0.9849123425, 0.9849123425, 0.9225653685]],
        [[0.977343914, 0.9368388781, 0.9368388781, 0.977343914], [0.9368388781, 0.3771039546, 0.3771039546, 0.9368388781], [0.9368388781, 0.3771039546, 0.3771039546, 0.9368388781], [0.977343914, 0.9368388781, 0.9368388781, 0.977343914]],
        [[0.9995922606, 0.8589955661, 0.8589955661, 0.9995922606], [0.8589955661, 0.2558519646, 0.2558519646, 0.8589955661], [0.8589955661, 0.2558519646, 0.2558519646, 0.8589955661], [0.9995922606, 0.8589955661, 0.8589955661, 0.9995922606]],
        [[0.9877942202, 0.7566872995, 0.7566872995, 0.9877942202], [0.7566872995, 0.1512382597, 0.1512382597, 0.7566872995], [0.7566872995, 0.1512382597, 0.1512382597, 0.7566872995], [0.9877942202, 0.7566872995, 0.7566872995, 0.9877942202]],
        [[0.9427538099, 0.6368862177, 0.6368862177, 0.9427538099], [0.6368862177, 0.0703920911, 0.0703920911, 0.6368862177], [0.6368862177, 0.0703920911, 0.0703920911, 0.6368862177], [0.9427538099, 0.6368862177, 0.6368862177, 0.9427538099]],
        [[0.8675404591, 0.5077565663, 0.5077565663, 0.8675404591], [0.5077565663, 0.0188229915, 0.0188229915, 0.5077565663], [0.5077565663, 0.0188229915, 0.0188229915, 0.5077565663], [0.8675404591, 0.5077565663, 0.5077565663, 0.8675404591]],
        [[0.7672798334, 0.3780983178, 0.3780983178, 0.7672798334], [0.3780983178, 0.0000453099, 0.0000453099, 0.3780983178], [0.3780983178, 0.0000453099, 0.0000453099, 0.3780983178], [0.7672798334, 0.3780983178, 0.3780983178, 0.7672798334]],
        [[0.6488045287, 0.2567474675, 0.2567474675, 0.6488045287], [0.2567474675, 0.0153387141, 0.0153387141, 0.2567474675], [0.2567474675, 0.0153387141, 0.0153387141, 0.2567474675], [0.6488045287, 0.2567474675, 0.2567474675, 0.6488045287]],
        [[0.5201884413, 0.1519738752, 0.1519738752, 0.5201884413], [0.1519738752, 0.0636609841, 0.0636609841, 0.1519738752], [0.1519738752, 0.0636609841, 0.0636609841, 0.1519738752], [0.5201884413, 0.1519738752, 0.1519738752, 0.5201884413]],
        [[0.3901965451, 0.0709176882, 0.0709176882, 0.3901965451], [0.0709176882, 0.1417190369, 0.1417190369, 0.0709176882], [0.0709176882, 0.1417190369, 0.1417190369, 0.0709176882], [0.3901965451, 0.0709176882, 0.0709176882, 0.3901965451]],
        [[0.2676875728, 0.0191027517, 0.0191027517, 0.2676875728], [0.0191027517, 0.2441933453, 0.2441933453, 0.0191027517], [0.0191027517, 0.2441933453, 0.2441933453, 0.0191027517], [0.2676875728, 0.0191027517, 0.0191027517, 0.2676875728]],
        [[0.1610103085, 0.0000601679, 0.0000601679, 0.1610103085], [0.0000601679, 0.3641004544, 0.3641004544, 0.0000601679], [0.0000601679, 0.3641004544, 0.3641004544, 0.0000601679], [0.1610103085, 0.0000601679, 0.0000601679, 0.1610103085]],
        [[0.0774346315, 0.0150876575, 0.0150876575, 0.0774346315], [0.0150876575, 0.493268893, 0.493268893, 0.0150876575], [0.0150876575, 0.493268893, 0.493268893, 0.0150876575], [0.0774346315, 0.0150876575, 0.0150876575, 0.0774346315]],
        [[0.022656086, 0.0631611219, 0.0631611219, 0.022656086], [0.0631611219, 0.6228960454, 0.6228960454, 0.0631611219], [0.0631611219, 0.6228960454, 0.6228960454, 0.0631611219], [0.022656086, 0.0631611219, 0.0631611219, 0.022656086]],
        [[0.0004077394, 0.1410044339, 0.1410044339, 0.0004077394], [0.1410044339, 0.7441480354, 0.7441480354, 0.1410044339], [0.1410044339, 0.7441480354, 0.7441480354, 0.1410044339], [0.0004077394, 0.1410044339, 0.1410044339, 0.0004077394]],
        [[0.0122057798, 0.2433127005, 0.2433127005, 0.0122057798], [0.2433127005, 0.8487617403, 0.8487617403, 0.2433127005], [0.2433127005, 0.8487617403, 0.8487617403, 0.2433127005], [0.0122057798, 0.2433127005, 0.2433127005, 0.0122057798]],
        [[0.0572461901, 0.3631137823, 0.3631137823, 0.0572461901], [0.3631137823, 0.9296079089, 0.9296079089, 0.3631137823], [0.3631137823, 0.9296079089, 0.9296079089, 0.3631137823], [0.0572461901, 0.3631137823, 0.3631137823, 0.0572461901]],
        [[0.1324595409, 0.4922434337, 0.4922434337, 0.1324595409], [0.4922434337, 0.9811770085, 0.9811770085, 0.4922434337], [0.4922434337, 0.9811770085, 0.9811770085, 0.4922434337], [0.1324595409, 0.4922434337, 0.4922434337, 0.1324595409]],
        [[0.2327201666, 0.6219016822, 0.6219016822, 0.2327201666], [0.6219016822, 0.9999546901, 0.9999546901, 0.6219016822], [0.6219016822, 0.9999546901, 0.9999546901, 0.6219016822], [0.2327201666, 0.6219016822, 0.6219016822, 0.2327201666]],
        [[0.3511954713, 0.7432525325, 0.7432525325, 0.3511954713], [0.7432525325, 0.9846612859, 0.9846612859, 0.7432525325], [0.7432525325, 0.9846612859, 0.9846612859, 0.7432525325], [0.3511954713, 0.7432525325, 0.7432525325, 0.3511954713]]
    ]

    private static let testingFrames: [[[Double]]] = [
        [[0.9, 0.9, 0.9, 1], [0.1, 0.1, 0.1, 0.1], [0.25, 0.25, 0.25, 0.25], [0.1, 0.1, 0.1, 0.1]],
        [[0.1, 0.1, 0.1, 0.1], [0.9, 0.9, 0.9, 1], [0.25, 0.25, 0.25, 0.25], [0.1, 0.1, 0.1, 0.1]],
        [[0.1, 0.1, 0.1, 0.1], [0.1, 0.1, 0.1, 0.1], [0.9, 0.9, 0.9, 1], [0.1, 0.1, 0.1, 0.1]],
        [[0.1, 0.1, 0.1, 0.1], [0.1, 0.1, 0.1, 0.1], [0.25, 0.25, 0.25, 0.25], [0.9, 0.9, 0.9, 1]]
    ]

    private static let completedFrames: [[[Double]]] = [
        [[0, 0, 0, 0], [0, 1, 1, 0], [0, 1, 1, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 0.5, 0.5, 0], [0, 0.5, 0.5, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [1, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [1, 0, 0, 0], [0, 1, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [1, 0, 1, 0], [0, 1, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 1], [1, 0, 1, 0], [0, 1, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 0.5], [0.5, 0, 0.5, 0], [0, 0.5, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 1], [1, 0, 1, 0], [0, 1, 0, 0]]
    ]

    private static let failedFrames: [[[Double]]] = [
        [[0, 0, 0, 0], [0, 1, 1, 0], [0, 1, 1, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 0.5, 0.5, 0], [0, 0.5, 0.5, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        [[0, 0, 0, 0], [0, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 0]],
        [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[1, 0, 0, 1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        [[1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]],
        [[0.5, 0, 0, 0.5], [0, 0.5, 0.5, 0], [0, 0.5, 0.5, 0], [0.5, 0, 0, 0.5]],
        [[1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]],
        [[1, 0, 0, 1], [0, 1, 1, 0], [0, 1, 1, 0], [1, 0, 0, 1]]
    ]
}

/// CSS-provided 4x4 frame Matrix loader translated to SwiftUI.
/// Used in the compact island's leading status slot for animated
/// phases. Frame tables intentionally mirror the CSS comment data
/// instead of deriving procedural animations, so designer-provided
/// frames can be pasted in directly.
private struct MatrixStatusLoader: View {
    let preset: MatrixLoaderPreset

    @State private var frameIndex = 0

    var body: some View {
        let frames = preset.frames
        let frame = frames[frameIndex % frames.count]
        Grid(horizontalSpacing: 1, verticalSpacing: 1) {
            ForEach(0..<4, id: \.self) { row in
                GridRow {
                    ForEach(0..<4, id: \.self) { column in
                        RoundedRectangle(cornerRadius: 1.2, style: .continuous)
                            .fill(preset.offColor)
                            .overlay(
                                RoundedRectangle(cornerRadius: 1.2, style: .continuous)
                                    .fill(preset.onColor)
                                    .opacity(frame[row][column])
                            )
                            .frame(width: 3, height: 3)
                    }
                }
            }
        }
        .shadow(color: preset.onColor.opacity(0.5), radius: 4)
        .onReceive(Timer.publish(every: 1.0 / preset.fps, on: .main, in: .common).autoconnect()) { _ in
            if preset.loops {
                frameIndex = (frameIndex + 1) % frames.count
            } else {
                frameIndex = min(frameIndex + 1, frames.count - 1)
            }
        }
        .onChange(of: preset) { _ in
            frameIndex = 0
        }
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
