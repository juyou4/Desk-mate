import XCTest
@testable import DeskmateCore

final class CharacterPackLoaderTests: XCTestCase {
    private var packDir: URL!

    override func setUpWithError() throws {
        let base = FileManager.default.temporaryDirectory
        let unique = "deskmate-pack-\(UUID().uuidString.prefix(8))"
        packDir = base.appendingPathComponent(unique, isDirectory: true)
        try FileManager.default.createDirectory(
            at: packDir, withIntermediateDirectories: true
        )
    }

    override func tearDownWithError() throws {
        if let dir = packDir, FileManager.default.fileExists(atPath: dir.path) {
            try FileManager.default.removeItem(at: dir)
        }
        packDir = nil
    }

    private func writeManifest(_ json: String) throws {
        let url = packDir.appendingPathComponent("manifest.json")
        try json.data(using: .utf8)!.write(to: url)
    }

    private func writeFile(_ relative: String) throws {
        let url = packDir.appendingPathComponent(relative)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data([0x00]).write(to: url)
    }

    // MARK: - Happy path

    func testLoadFullyResolvedPack() throws {
        try writeManifest(#"""
        {
          "spec_version": 1,
          "id": "pixie",
          "display_name": "Pixie",
          "states": {
            "idle":     { "fps": 4, "frames": ["idle/000.png"] },
            "working":  { "fps": 4, "frames": ["working/000.png"] },
            "thinking": { "fps": 4, "frames": ["thinking/000.png"] },
            "alert":    { "fps": 4, "frames": ["alert/000.png"] }
          }
        }
        """#)
        try writeFile("idle/000.png")
        try writeFile("working/000.png")
        try writeFile("thinking/000.png")
        try writeFile("alert/000.png")

        let loader = CharacterPackLoader()
        let loaded = try loader.load(from: packDir)

        XCTAssertEqual(loaded.manifest.displayName, "Pixie")
        XCTAssertTrue(loaded.missingFrames.isEmpty)
        XCTAssertTrue(loaded.missingRequiredStates.isEmpty)
        XCTAssertTrue(loaded.isFullyResolved)
    }

    // MARK: - Failure modes

    func testMissingManifestThrows() {
        let loader = CharacterPackLoader()
        XCTAssertThrowsError(try loader.load(from: packDir)) { error in
            guard case CharacterPackLoader.LoadError.manifestMissing = error else {
                return XCTFail("expected manifestMissing, got \(error)")
            }
        }
    }

    func testInvalidJSONThrows() throws {
        try writeManifest("{ not json")
        let loader = CharacterPackLoader()
        XCTAssertThrowsError(try loader.load(from: packDir)) { error in
            guard case CharacterPackLoader.LoadError.manifestInvalid = error else {
                return XCTFail("expected manifestInvalid, got \(error)")
            }
        }
    }

    // MARK: - Resource diagnostics

    func testMissingFramesAreReportedSorted() throws {
        try writeManifest(#"""
        {
          "spec_version": 1,
          "id": "pixie",
          "display_name": "Pixie",
          "states": {
            "idle":     { "fps": 4, "frames": ["idle/000.png", "idle/001.png"] },
            "working":  { "fps": 4, "frames": ["working/000.png"] },
            "thinking": { "fps": 4, "frames": ["thinking/000.png"] },
            "alert":    { "fps": 4, "frames": ["alert/000.png"] }
          }
        }
        """#)
        // Only the 000 frames exist — working/idle/001 are missing.
        try writeFile("idle/000.png")
        try writeFile("working/000.png")
        try writeFile("thinking/000.png")
        try writeFile("alert/000.png")

        let loader = CharacterPackLoader()
        let loaded = try loader.load(from: packDir)
        XCTAssertEqual(loaded.missingFrames, ["idle/001.png"])
        XCTAssertFalse(loaded.isFullyResolved)
    }

    func testMissingRequiredStatesAreReported() throws {
        try writeManifest(#"""
        {
          "spec_version": 1,
          "id": "pixie",
          "display_name": "Pixie",
          "states": {
            "idle":    { "fps": 4, "frames": ["idle/000.png"] },
            "working": { "fps": 4, "frames": ["working/000.png"] }
          }
        }
        """#)
        try writeFile("idle/000.png")
        try writeFile("working/000.png")

        let loader = CharacterPackLoader()
        let loaded = try loader.load(from: packDir)
        XCTAssertEqual(loaded.missingRequiredStates, ["thinking", "alert"])
    }

    func testEmptyFrameStringIsFlagged() throws {
        try writeManifest(#"""
        {
          "spec_version": 1,
          "id": "pixie",
          "display_name": "Pixie",
          "states": {
            "idle":     { "fps": 4, "frames": [""] },
            "working":  { "fps": 4, "frames": ["working/000.png"] },
            "thinking": { "fps": 4, "frames": ["thinking/000.png"] },
            "alert":    { "fps": 4, "frames": ["alert/000.png"] }
          }
        }
        """#)
        try writeFile("working/000.png")
        try writeFile("thinking/000.png")
        try writeFile("alert/000.png")

        let loader = CharacterPackLoader()
        let loaded = try loader.load(from: packDir)
        XCTAssertTrue(loaded.missingFrames.contains("idle/<empty>"))
    }
}
