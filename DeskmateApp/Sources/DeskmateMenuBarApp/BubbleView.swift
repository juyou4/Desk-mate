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
    let onMessage: (String) -> Void

    @State private var draft = ""

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
                    .lineLimit(8)
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
            messageInput
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(width: bubbleWidth, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor).opacity(0.96))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(Color.black.opacity(0.08), lineWidth: 0.5)
        )
        .shadow(color: .black.opacity(0.2), radius: 6, x: 0, y: 2)
        .onChange(of: bubble.id) { _ in
            draft = ""
        }
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

    private var bubbleWidth: CGFloat {
        let text = bubble.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.count > 42 || !bubble.actions.isEmpty {
            return 320
        }
        return 280
    }

    private var messageInput: some View {
        HStack(spacing: 6) {
            TextField("Ask Deskmate", text: $draft)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 12))
                .onSubmit(send)
            Button(action: send) {
                Image(systemName: "paperplane.fill")
                    .font(.system(size: 11, weight: .semibold))
                    .frame(width: 18, height: 18)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.mini)
            .disabled(trimmedDraft.isEmpty)
            .help("Send")
        }
    }

    private var trimmedDraft: String {
        draft.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func send() {
        let message = trimmedDraft
        guard !message.isEmpty else { return }
        draft = ""
        onMessage(message)
    }
}
