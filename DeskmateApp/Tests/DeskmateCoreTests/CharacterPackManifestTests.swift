import XCTest
@testable import DeskmateCore

final class CharacterPackManifestTests: XCTestCase {
    private let decoder = JSONDecoder()

    private func manifestWithStates(_ pairs: [(String, [String])]) -> CharacterPackManifest {
        var states: [String: StateFrames] = [:]
        for (name, frames) in pairs {
            states[name] = StateFrames(fps: 4, frames: frames)
        }
        return CharacterPackManifest(id: "pixie", displayName: "Pixie", states: states)
    }

    func testDetectsMissingRequiredStates() {
        let m = manifestWithStates([
            ("idle", ["idle/001.png"]),
            ("working", ["working/001.png"]),
        ])
        XCTAssertEqual(m.missingRequiredStates(), ["thinking", "alert"])
    }

    func testResolveStateHonorsFallbacks() {
        let m = manifestWithStates([
            ("idle", ["idle/001.png"]),
            ("walking", ["walking/001.png"]),
        ])
        XCTAssertEqual(m.resolveState("walking_left"), "walking")
        XCTAssertNil(m.resolveState("nonexistent"))
    }

    func testResolveStateDetectsCycles() {
        var m = manifestWithStates([("idle", ["idle/001.png"])])
        m.fallbacks = ["a": "b", "b": "a"]
        XCTAssertNil(m.resolveState("a"))
    }

    func testForwardCompatibleUnknownSections() throws {
        let raw = """
        {
          "spec_version": 1,
          "id": "pixie",
          "display_name": "Pixie",
          "states": { "idle": { "fps": 4, "frames": ["idle/001.png"] } },
          "future_section": { "hello": "world" }
        }
        """.data(using: .utf8)!
        let m = try decoder.decode(CharacterPackManifest.self, from: raw)
        XCTAssertEqual(m.displayName, "Pixie")
        XCTAssertEqual(m.states["idle"]?.frames, ["idle/001.png"])
    }

    func testSnakeCaseCodingKeys() throws {
        let m = CharacterPackManifest(id: "pixie", displayName: "Pixie")
        let data = try JSONEncoder().encode(m)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertNotNil(json?["display_name"])
        XCTAssertNotNil(json?["spec_version"])
        XCTAssertNotNil(json?["required_states"])
        XCTAssertNil(json?["displayName"])
    }
}
