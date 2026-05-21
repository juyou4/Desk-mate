import Foundation

/// Disk loader for character packs (V10 I4 / L1-D).
///
/// Pack layout on disk::
///
///     PackDir/
///     ├── manifest.json
///     ├── idle/001.png
///     ├── working/001.png
///     └── ...
///
/// The loader reads the JSON manifest, walks ``CharacterPackManifest.states``
/// and flags any frame files that are missing. Required-state validation is
/// done by the manifest itself; this layer only adds *resource* validation.
public struct CharacterPackLoader {
    public struct LoadedPack: Equatable, Sendable {
        public let manifest: CharacterPackManifest
        public let rootURL: URL
        public let missingFrames: [String]
        public let missingRequiredStates: [String]

        public init(
            manifest: CharacterPackManifest,
            rootURL: URL,
            missingFrames: [String],
            missingRequiredStates: [String]
        ) {
            self.manifest = manifest
            self.rootURL = rootURL
            self.missingFrames = missingFrames
            self.missingRequiredStates = missingRequiredStates
        }

        /// True when the pack has every required state *and* every referenced
        /// frame file on disk. Packs may still be *usable* when this is
        /// false — fallbacks can cover missing states — but the shell should
        /// surface a warning so authors notice.
        public var isFullyResolved: Bool {
            missingFrames.isEmpty && missingRequiredStates.isEmpty
        }
    }

    public enum LoadError: Error, CustomStringConvertible, Equatable {
        case manifestMissing(URL)
        case manifestUnreadable(URL, String)
        case manifestInvalid(String)

        public var description: String {
            switch self {
            case .manifestMissing(let url):
                return "manifest.json not found at \(url.path)"
            case .manifestUnreadable(let url, let reason):
                return "manifest.json unreadable at \(url.path): \(reason)"
            case .manifestInvalid(let reason):
                return "manifest.json invalid: \(reason)"
            }
        }
    }

    private let fileManager: FileManager

    public init(fileManager: FileManager = .default) {
        self.fileManager = fileManager
    }

    /// Load a pack from its root directory.
    ///
    /// - Parameters:
    ///   - packDir: Directory containing ``manifest.json``.
    /// - Returns: ``LoadedPack`` with resource diagnostics.
    /// - Throws: ``LoadError`` when the manifest is missing or malformed.
    public func load(from packDir: URL) throws -> LoadedPack {
        let manifestURL = packDir.appendingPathComponent("manifest.json")
        guard fileManager.fileExists(atPath: manifestURL.path) else {
            throw LoadError.manifestMissing(manifestURL)
        }

        let data: Data
        do {
            data = try Data(contentsOf: manifestURL)
        } catch {
            throw LoadError.manifestUnreadable(manifestURL, error.localizedDescription)
        }

        let manifest: CharacterPackManifest
        do {
            manifest = try JSONDecoder().decode(CharacterPackManifest.self, from: data)
        } catch {
            throw LoadError.manifestInvalid(String(describing: error))
        }

        let missingFrames = self.missingFrames(for: manifest, in: packDir)
        let missingRequired = manifest.missingRequiredStates()

        return LoadedPack(
            manifest: manifest,
            rootURL: packDir,
            missingFrames: missingFrames,
            missingRequiredStates: missingRequired
        )
    }

    /// Return a list of ``"<state>/<frame>"`` paths referenced by the manifest
    /// but missing on disk. The returned list is deterministic (sorted).
    public func missingFrames(
        for manifest: CharacterPackManifest, in packDir: URL
    ) -> [String] {
        var missing: [String] = []
        for (stateName, frames) in manifest.states {
            for frame in frames.frames {
                // Frame paths in the manifest are treated as relative to the
                // pack root. An empty entry is a manifest bug; flag it.
                if frame.isEmpty {
                    missing.append("\(stateName)/<empty>")
                    continue
                }
                let url = packDir.appendingPathComponent(frame)
                if !fileManager.fileExists(atPath: url.path) {
                    missing.append(frame)
                }
            }
        }
        return missing.sorted()
    }
}
