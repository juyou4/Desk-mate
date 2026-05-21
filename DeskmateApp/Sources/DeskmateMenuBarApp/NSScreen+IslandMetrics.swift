#if canImport(AppKit)
import AppKit

extension NSScreen {
    var deskmateNotchSize: CGSize {
        guard safeAreaInsets.top > 0 else {
            return CGSize(width: 224, height: deskmateIslandClosedHeight)
        }

        let leftWidth = auxiliaryTopLeftArea?.width ?? 0
        let rightWidth = auxiliaryTopRightArea?.width ?? 0
        let width = max(160, frame.width - leftWidth - rightWidth + 4)
        return CGSize(width: width, height: safeAreaInsets.top)
    }

    var deskmateTopStatusBarHeight: CGFloat {
        let reservedTopInset = max(0, frame.maxY - visibleFrame.maxY)
        if reservedTopInset > 0 {
            return reservedTopInset
        }
        if safeAreaInsets.top > 0 {
            return safeAreaInsets.top
        }
        return 24
    }

    var deskmateIslandClosedHeight: CGFloat {
        if safeAreaInsets.top > 0 {
            return safeAreaInsets.top
        }
        return deskmateTopStatusBarHeight
    }

    var deskmateHasPhysicalNotch: Bool {
        safeAreaInsets.top > 0
            || auxiliaryTopLeftArea?.isEmpty == false
            || auxiliaryTopRightArea?.isEmpty == false
    }
}
#endif
