import SwiftUI
import DeskmateCore

/// Speech-bubble pop-up rendered above the pet sprite (V10 Phase
/// 11d-vii). Mirrors :class:`BubbleSpec` exactly: optional SF Symbol
/// icon, text body (markdown collapsed to plain for now), and a row
/// of action buttons whose taps travel back through
/// :class:`DeskmateMenuBarRuntime.sendBubbleAction`.
struct BubbleView: View {
    let bubble: BubbleSpec
    let onAction: (BubbleAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 8) {
                if let icon = bubble.icon {
                    Image(systemName: icon)
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(tintForKind)
                        .frame(width: 18, height: 18)
                }
                Text(bubble.text)
                    .font(.system(size: 13))
                    .lineLimit(5)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !bubble.actions.isEmpty {
                HStack(spacing: 6) {
                    ForEach(
                        Array(bubble.actions.enumerated()),
                        id: \.offset
                    ) { _, action in
                        Button(action.label) { onAction(action) }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: 260, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor).opacity(0.96))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.black.opacity(0.08), lineWidth: 0.5)
        )
        .shadow(color: .black.opacity(0.2), radius: 6, x: 0, y: 2)
    }

    private var tintForKind: Color {
        switch bubble.kind {
        case .approvalHint: return .orange
        case .reminder: return .blue
        case .chat: return .primary
        case .status: return .secondary
        case .randomReaction: return .pink
        case .system: return .gray
        }
    }
}
