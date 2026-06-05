import Foundation
import CoreGraphics

public struct IslandInteractionInput: Equatable, Sendable {
    public var screenFrame: CGRect
    public var notchSize: CGSize
    public var hasPhysicalNotch: Bool
    public var hasCompactPresence: Bool
    public var isExpanded: Bool
    public var activeCount: Int

    public init(
        screenFrame: CGRect,
        notchSize: CGSize,
        hasPhysicalNotch: Bool,
        hasCompactPresence: Bool,
        isExpanded: Bool,
        activeCount: Int = 0
    ) {
        self.screenFrame = screenFrame
        self.notchSize = notchSize
        self.hasPhysicalNotch = hasPhysicalNotch
        self.hasCompactPresence = hasCompactPresence
        self.isExpanded = isExpanded
        self.activeCount = activeCount
    }
}

public struct IslandInteractionGeometry: Equatable, Sendable {
    public var input: IslandInteractionInput

    public init(input: IslandInteractionInput) {
        self.input = input
    }

    public var closedBaseWidth: CGFloat {
        max(160, input.notchSize.width)
    }

    public var closedBaseHeight: CGFloat {
        max(20, input.notchSize.height)
    }

    public var closedExpansionWidth: CGFloat {
        guard input.hasCompactPresence else { return 0 }
        let countBoost = CGFloat(min(max(input.activeCount, 1), 5) - 1) * 10
        return min(168, 112 + countBoost)
    }

    public var closedSurfaceSize: CGSize {
        CGSize(
            width: closedBaseWidth + closedExpansionWidth,
            height: closedSurfaceHeight
        )
    }

    public var closedSurfaceHeight: CGFloat {
        closedBaseHeight
    }

    public var expandedSurfaceSize: CGSize {
        let screenWidth = input.screenFrame.width
        let maxWidth = min(max(screenWidth * 0.38, 432), 520)
        let rows = CGFloat(min(max(input.activeCount, 1), 5))
        let contentHeight = 86 + rows * 62
        return CGSize(
            width: maxWidth,
            height: min(max(contentHeight, 180), 380)
        )
    }

    public var surfaceSize: CGSize {
        input.isExpanded ? expandedSurfaceSize : closedSurfaceSize
    }

    public var panelSize: CGSize {
        let size = surfaceSize
        return CGSize(
            width: size.width + 28,
            height: size.height + 16
        )
    }

    public var panelFrame: CGRect {
        let size = panelSize
        return CGRect(
            x: input.screenFrame.midX - size.width / 2,
            y: input.screenFrame.maxY - size.height,
            width: size.width,
            height: size.height
        )
    }

    public var surfaceRectInPanel: CGRect {
        let size = surfaceSize
        return CGRect(
            x: panelSize.width / 2 - size.width / 2,
            y: panelSize.height - size.height,
            width: size.width,
            height: size.height
        )
    }

    public var expandedSurfaceRectInPanel: CGRect {
        let size = expandedSurfaceSize
        return CGRect(
            x: panelSize.width / 2 - size.width / 2,
            y: panelSize.height - size.height,
            width: size.width,
            height: size.height
        )
    }

    public var hoverActivationRectInPanel: CGRect {
        if input.isExpanded {
            return surfaceRectInPanel.insetBy(dx: -8, dy: -8)
        }
        return surfaceRectInPanel.insetBy(dx: -18, dy: -8)
    }

    public static let collapsedHitBandHeight: CGFloat = 44

    /// MioIsland-style pass-through band: when collapsed, only the
    /// top strip can accept events; everything below returns nil so
    /// regular desktop/status-bar interaction is not blocked.
    public static func collapsedHitBandRect(
        in bounds: CGRect,
        height: CGFloat = collapsedHitBandHeight
    ) -> CGRect {
        let bandHeight = min(max(0, height), bounds.height)
        return CGRect(
            x: bounds.minX,
            y: bounds.maxY - bandHeight,
            width: bounds.width,
            height: bandHeight
        )
    }

    public func hitTestRectInPanel(bounds: CGRect) -> CGRect {
        if input.isExpanded {
            return surfaceRectInPanel.insetBy(dx: -8, dy: -8)
        }
        return Self.collapsedHitBandRect(in: bounds)
    }

    /// True when an expanded-panel click landed in the transparent
    /// area around the visible island surface. Window layers use this
    /// to close the island and re-post the click to the system so
    /// menu bar / underlying app controls are not swallowed.
    public func shouldPassthroughExpandedClick(localPoint: CGPoint) -> Bool {
        input.isExpanded && !surfaceRectInPanel.contains(localPoint)
    }

    public func diagnostics(screenName: String) -> String {
        let panel = panelFrame
        let surface = surfaceRectInPanel
        let notch = CGRect(
            x: input.screenFrame.midX - input.notchSize.width / 2,
            y: input.screenFrame.maxY - input.notchSize.height,
            width: input.notchSize.width,
            height: input.notchSize.height
        )
        return [
            "screen=\(screenName)",
            "screenFrame=\(Self.format(input.screenFrame))",
            "notch=\(Self.format(notch))",
            "panel=\(Self.format(panel))",
            "surface=\(Self.format(surface))",
            "expanded=\(input.isExpanded)",
        ].joined(separator: " | ")
    }

    private static func format(_ rect: CGRect) -> String {
        "(\(Int(rect.minX)),\(Int(rect.minY)) \(Int(rect.width))x\(Int(rect.height)))"
    }
}
