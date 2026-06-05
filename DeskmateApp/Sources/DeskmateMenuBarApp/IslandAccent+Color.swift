import SwiftUI
import DeskmateCore

extension IslandAccent {
    /// Resolve the accent preset to a `Color`. ``.system`` defers to
    /// the macOS user accent so themes that try to enforce a brand
    /// palette don't override the OS-level accessibility setting.
    var color: Color {
        switch self {
        case .system: return Color.accentColor
        case .blue: return Color(red: 0.26, green: 0.45, blue: 0.86)
        case .purple: return Color(red: 0.55, green: 0.36, blue: 0.92)
        case .pink: return Color(red: 0.96, green: 0.36, blue: 0.62)
        case .orange: return Color(red: 0.95, green: 0.55, blue: 0.18)
        case .green: return Color(red: 0.29, green: 0.86, blue: 0.46)
        case .mint: return Color(red: 0.34, green: 0.85, blue: 0.78)
        case .red: return Color(red: 0.94, green: 0.28, blue: 0.28)
        }
    }
}
