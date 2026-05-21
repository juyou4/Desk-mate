import Foundation
import CoreGraphics

/// Pure geometry for the Dynamic Island surface (V10 L2-#7 / L2-#8).
///
/// The window layer feeds in the current screen + the optional notch size;
/// this struct returns the island's frame and corner radius for any blend
/// between the compact resting shape and an expanded surface.
///
/// Keeping the geometry pure means multi-screen / notch-less machines can be
/// unit-tested without a live ``NSScreen`` — the window layer is responsible
/// only for sampling inputs and applying the resulting rect.
public struct IslandGeometry: Equatable, Sendable {
    public var screenFrame: CGRect
    /// Size of the physical notch (MBP 2021+). ``nil`` for notchless machines.
    public var notchSize: CGSize?
    /// Shape of the compact pill on notchless displays.
    public var compactFallbackSize: CGSize
    /// Distance between the island's top edge and the screen top.
    public var topInset: CGFloat
    public var compactCornerRadius: CGFloat
    public var expandedCornerRadius: CGFloat

    public init(
        screenFrame: CGRect,
        notchSize: CGSize? = nil,
        compactFallbackSize: CGSize = CGSize(width: 180, height: 28),
        topInset: CGFloat = 0,
        compactCornerRadius: CGFloat = 18,
        expandedCornerRadius: CGFloat = 28
    ) {
        self.screenFrame = screenFrame
        self.notchSize = notchSize
        self.compactFallbackSize = compactFallbackSize
        self.topInset = topInset
        self.compactCornerRadius = compactCornerRadius
        self.expandedCornerRadius = expandedCornerRadius
    }

    /// The compact pill rect, hugging the notch when present and falling
    /// back to a centred top-bar pill otherwise. Y is in Cocoa coordinates
    /// (origin bottom-left); the returned rect's top edge is flush with the
    /// screen top after ``topInset`` is subtracted.
    public func compactRect() -> CGRect {
        let size = notchSize ?? compactFallbackSize
        let x = screenFrame.midX - size.width / 2
        // Cocoa coordinates: y grows upward, so "top of screen" is maxY.
        let y = screenFrame.maxY - size.height - topInset
        return CGRect(x: x, y: y, width: size.width, height: size.height)
    }

    /// The fully expanded rect for a given target size. The expansion grows
    /// symmetrically around the compact pill's horizontal centre and keeps
    /// its top edge at the screen top.
    public func expandedRect(size: CGSize) -> CGRect {
        let x = screenFrame.midX - size.width / 2
        let y = screenFrame.maxY - size.height - topInset
        return CGRect(x: x, y: y, width: size.width, height: size.height)
    }

    /// Interpolated rect for smooth morph animations. ``progress`` is
    /// clamped to ``[0, 1]``.
    public func interpolatedRect(to targetSize: CGSize, progress: Double) -> CGRect {
        let p = max(0, min(1, progress))
        let from = compactRect()
        let to = expandedRect(size: targetSize)
        let w = from.width + (to.width - from.width) * CGFloat(p)
        let h = from.height + (to.height - from.height) * CGFloat(p)
        let x = screenFrame.midX - w / 2
        let y = screenFrame.maxY - h - topInset
        return CGRect(x: x, y: y, width: w, height: h)
    }

    /// Corner radius blended from the compact to expanded radius. Capped at
    /// half of the rect's shorter edge so the pill stays a pill.
    public func cornerRadius(for rect: CGRect, progress: Double) -> CGFloat {
        let p = max(0, min(1, progress))
        let blended =
            compactCornerRadius + (expandedCornerRadius - compactCornerRadius) * CGFloat(p)
        let maxRadius = min(rect.width, rect.height) / 2
        return min(blended, maxRadius)
    }
}
