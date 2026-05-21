#if canImport(SwiftUI)
import SwiftUI
import DeskmateCore

/// SwiftUI wrapper that turns an :class:`AvatarSpec` into a sprite.
///
/// Two styles are supported:
///
/// - ``.pixel`` — an 8×8 pixel-art cat face rendered through
///   ``Canvas`` (no asset catalog needed).
/// - ``.emoji`` — a single mood-driven emoji glyph scaled to the
///   full sprite bounds.
///
/// Kept separate from :class:`PetOverlay` so individual avatar
/// styles can be previewed in isolation and so the pure Swift
/// ``AvatarRenderer`` stays free of SwiftUI imports.
struct AvatarView: View {
    let spec: AvatarSpec
    var size: CGFloat = 64

    var body: some View {
        switch spec.style {
        case .pixel:
            PixelAvatarBody(spec: spec, size: size)
        case .emoji:
            EmojiAvatarBody(spec: spec, size: size)
        }
    }
}


private struct PixelAvatarBody: View {
    let spec: AvatarSpec
    let size: CGFloat

    var body: some View {
        Canvas { context, _ in
            let mask = AvatarRenderer.pixelMask()
            let rows = mask.count
            let cols = mask.first?.count ?? rows
            let maxDim = max(rows, cols)
            let px = size / CGFloat(maxDim)
            let bodyColor = Color(spec.primary)
            let accentColor = Color(spec.accent)
            for (y, row) in mask.enumerated() {
                for (x, cell) in row.enumerated() {
                    guard cell != 0 else { continue }
                    let rect = CGRect(
                        x: CGFloat(x) * px,
                        y: CGFloat(y) * px,
                        width: px,
                        height: px
                    )
                    let color = (cell == 2) ? accentColor : bodyColor
                    context.fill(Path(rect), with: .color(color))
                }
            }
        }
        .frame(width: size, height: size)
        .accessibilityLabel("Deskmate pixel avatar")
    }
}


private struct EmojiAvatarBody: View {
    let spec: AvatarSpec
    let size: CGFloat

    var body: some View {
        Text(spec.emoji)
            // Emoji glyphs need a slightly smaller font metric than
            // the frame size to avoid clipping on cap-height-heavy
            // glyphs like 🎉 / ⚠️.
            .font(.system(size: size * 0.82))
            .frame(width: size, height: size, alignment: .center)
            .accessibilityLabel("Deskmate emoji avatar: \(spec.emoji)")
    }
}


/// Convenience bridge between the pure-Swift palette type and
/// SwiftUI's ``Color``. Kept ``fileprivate`` so the conversion rule
/// (sRGB, 0..1 floats) lives next to its single use.
private extension Color {
    init(_ rgb: AvatarRgbColor) {
        self.init(
            .sRGB,
            red: Double(rgb.r) / 255.0,
            green: Double(rgb.g) / 255.0,
            blue: Double(rgb.b) / 255.0,
            opacity: 1.0
        )
    }
}
#endif
