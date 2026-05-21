import XCTest
@testable import DeskmateCore

/// Acceptance for :mod:`AvatarRenderer` (V10 Phase 7).
final class AvatarRendererTests: XCTestCase {

    // MARK: - Style parsing

    func testKnownStylesParse() {
        XCTAssertEqual(AvatarStyleKind(rawStyle: "pixel"), .pixel)
        XCTAssertEqual(AvatarStyleKind(rawStyle: "emoji"), .emoji)
        XCTAssertEqual(AvatarStyleKind(rawStyle: "PIXEL"), .pixel)
        XCTAssertEqual(AvatarStyleKind(rawStyle: "  Emoji  "), .emoji)
    }

    func testUnknownStyleFallsBackToPixel() {
        // A mis-spelled / new / empty manifest entry should still
        // produce *something* on screen instead of crashing.
        XCTAssertEqual(AvatarStyleKind(rawStyle: ""), .pixel)
        XCTAssertEqual(AvatarStyleKind(rawStyle: "sprite3d"), .pixel)
        XCTAssertEqual(AvatarStyleKind(rawStyle: "🤷"), .pixel)
    }

    // MARK: - Emoji lookup

    func testEmojiDefaultsByMood() {
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .idle, emotion: "neutral"),
            "🙂"
        )
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .working, emotion: "neutral"),
            "🧠"
        )
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .thinking, emotion: "neutral"),
            "🤔"
        )
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .happy, emotion: "neutral"),
            "🎉"
        )
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .alert, emotion: "neutral"),
            "⚠️"
        )
    }

    func testEmotionOverridesMood() {
        // ``urgent`` wins over ``happy`` — the urgency signal should
        // always surface, not the background mood.
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .happy, emotion: "urgent"),
            "🚨"
        )
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .idle, emotion: "concerned"),
            "😟"
        )
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .alert, emotion: "cheerful"),
            "🎉"
        )
    }

    func testUnknownEmotionFallsBackToMood() {
        XCTAssertEqual(
            AvatarRenderer.emojiFor(mood: .working, emotion: "mystery"),
            "🧠"
        )
    }

    // MARK: - Resolve() contract

    func testResolvePopulatesAllFields() {
        let spec = AvatarRenderer.resolve(
            style: "emoji",
            mood: .alert,
            emotion: "urgent",
            attentionLevel: 0.7
        )
        XCTAssertEqual(spec.style, .emoji)
        XCTAssertEqual(spec.emoji, "🚨")
        XCTAssertEqual(spec.glow, 0.7, accuracy: 0.0001)
        XCTAssertEqual(spec.primary, .orangeBody)
        XCTAssertEqual(spec.accent, .redAccent)
        XCTAssertEqual(spec.aura, spec.primary)
    }

    func testResolveClampsGlow() {
        let hi = AvatarRenderer.resolve(
            style: "pixel", mood: .happy, attentionLevel: 2.5
        )
        XCTAssertEqual(hi.glow, 1.0, accuracy: 0.0001)
        let lo = AvatarRenderer.resolve(
            style: "pixel", mood: .happy, attentionLevel: -3.0
        )
        XCTAssertEqual(lo.glow, 0.0, accuracy: 0.0001)
    }

    func testResolveFallsBackToPixelStyle() {
        let spec = AvatarRenderer.resolve(
            style: "unknown-style",
            mood: .idle
        )
        XCTAssertEqual(spec.style, .pixel)
        // Emoji should still be populated for a11y / fallback.
        XCTAssertFalse(spec.emoji.isEmpty)
    }

    func testResolveColorPaletteIsDeterministic() {
        let a = AvatarRenderer.resolve(style: "pixel", mood: .working)
        let b = AvatarRenderer.resolve(style: "pixel", mood: .working)
        XCTAssertEqual(a.primary, b.primary)
        XCTAssertEqual(a.accent, b.accent)
    }

    // MARK: - Pixel mask

    func testPixelMaskIs8x8AndBoundedValues() {
        let mask = AvatarRenderer.pixelMask()
        XCTAssertEqual(mask.count, 8)
        for row in mask {
            XCTAssertEqual(row.count, 8)
            for cell in row {
                XCTAssertTrue(
                    (0...2).contains(cell),
                    "pixel mask cell \(cell) out of range 0...2"
                )
            }
        }
    }

    func testPixelMaskHasBothLayers() {
        let mask = AvatarRenderer.pixelMask()
        let flat = mask.flatMap { $0 }
        XCTAssertTrue(flat.contains(1), "mask should have body pixels")
        XCTAssertTrue(flat.contains(2), "mask should have accent pixels")
    }

    // MARK: - RGB clamping

    func testRgbClampsOutOfRangeChannels() {
        let c = AvatarRgbColor(r: -50, g: 500, b: 127)
        XCTAssertEqual(c.r, 0)
        XCTAssertEqual(c.g, 255)
        XCTAssertEqual(c.b, 127)
    }

    func testRgbEqualityMatchesChannels() {
        let a = AvatarRgbColor(r: 10, g: 20, b: 30)
        let b = AvatarRgbColor(r: 10, g: 20, b: 30)
        XCTAssertEqual(a, b)
    }
}
