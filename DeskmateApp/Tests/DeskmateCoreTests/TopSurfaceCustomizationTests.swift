import XCTest
@testable import DeskmateCore

final class TopSurfaceCustomizationTests: XCTestCase {
    /// R12.4: New fields decode with defaults when absent from JSON.
    func testDecodeWithMissingNewFieldsUsesDefaults() throws {
        let json = """
        {"spec_version": 1, "theme": "dark", "font_scale": 1.0, "buddy_style": "pixel", "show_buddy": true, "hardware_notch_mode": "automatic", "screen_geometries": [], "hover_speed": 1.0}
        """.data(using: .utf8)!
        let decoded = try JSONDecoder().decode(TopSurfaceCustomization.self, from: json)
        XCTAssertEqual(decoded.feedback.audio, false)
        XCTAssertNil(decoded.feedback.audioName)
        XCTAssertNil(decoded.preferredScreenId)
    }

    /// R12.5: New fields decode correctly when present in JSON.
    func testDecodeWithNewFieldsPresent() throws {
        let json = """
        {"spec_version": 1, "theme": "dark", "font_scale": 1.0, "buddy_style": "pixel", "show_buddy": true, "hardware_notch_mode": "automatic", "screen_geometries": [], "hover_speed": 1.0, "feedback": {"audio": true, "audio_name": "Ping"}, "preferred_screen_id": "ABC-123"}
        """.data(using: .utf8)!
        let decoded = try JSONDecoder().decode(TopSurfaceCustomization.self, from: json)
        XCTAssertEqual(decoded.feedback.audio, true)
        XCTAssertEqual(decoded.feedback.audioName, "Ping")
        XCTAssertEqual(decoded.preferredScreenId, "ABC-123")
    }

    /// R12.5: Encode → decode round-trip preserves new fields.
    func testRoundTripPreservesNewFields() throws {
        var original = TopSurfaceCustomization()
        original.feedback = FeedbackPrefs(audio: true, audioName: "Pop")
        original.preferredScreenId = "UUID-456"
        let data = try JSONEncoder().encode(original)
        let decoded = try JSONDecoder().decode(TopSurfaceCustomization.self, from: data)
        XCTAssertEqual(decoded.feedback.audio, true)
        XCTAssertEqual(decoded.feedback.audioName, "Pop")
        XCTAssertEqual(decoded.preferredScreenId, "UUID-456")
    }
}
