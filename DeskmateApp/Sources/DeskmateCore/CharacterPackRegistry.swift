import Foundation

/// Character pack discovery + activation (V10 Phase 8).
///
/// :class:`CharacterPackLoader` already knows how to read and
/// validate *one* pack directory. The registry sits on top, answering
/// two questions the single-pack loader cannot:
///
/// - Given a root directory, which packs are installed right now?
/// - Given user preferences + fallback rules, which pack should be
///   active for this process?
///
/// Python's ``deskmate_agent.character_packs`` module carries the
/// same contract — same env-var names, same resolution order — so
/// both ends of the bridge reach the same conclusions without needing
/// to exchange extra handshake packets.

public enum CharacterPackEnv {
    /// Root directory to scan for packs. Defaults to
    /// ``~/.deskmate/packs`` on macOS / Linux.
    public static let packsDirVar = "DESKMATE_PACKS_DIR"

    /// Id of the pack that should be resolved active at boot.
    public static let activePackVar = "DESKMATE_CHARACTER_PACK"

    /// Optional development-time override pointing at bundled packs.
    public static let bundledPacksDirVar = "DESKMATE_BUNDLED_PACKS_DIR"

    /// Legacy direct style override. Used only when no manifest-backed
    /// character pack resolves.
    public static let avatarStyleVar = "DESKMATE_AVATAR_STYLE"

    /// Stable id of the primary built-in pack shipped with the app.
    public static let builtinPackId = "deskmate_native"
    /// Lightweight legacy fallback kept for older installs and low-resource
    /// environments.
    public static let legacyPixelPackId = "pixel_default"
}


/// Outcome of a :func:`CharacterPackLoader` directory scan.
///
/// ``skipped`` maps the offending path (either a pack directory or a
/// manifest path) to a short human-readable reason — useful for a
/// future "packs" status pane in the menu bar without needing the
/// loader to raise on the first bad entry.
public struct CharacterPackDiscoveryResult: Sendable, Equatable {
    public let packs: [CharacterPackLoader.LoadedPack]
    public let skipped: [String: String]

    public init(
        packs: [CharacterPackLoader.LoadedPack] = [],
        skipped: [String: String] = [:]
    ) {
        self.packs = packs
        self.skipped = skipped
    }
}


public enum CharacterPackDiscovery {

    /// Walk ``root`` for ``<pack_id>/manifest.json`` entries. Never
    /// throws — unreadable packs land in ``skipped``. The output is
    /// sorted by directory name so the "first registered wins"
    /// fallback used by :class:`CharacterPackRegistry` is stable.
    public static func discoverPacks(
        in root: URL,
        fileManager: FileManager = .default,
        loader: CharacterPackLoader = CharacterPackLoader()
    ) -> CharacterPackDiscoveryResult {
        var isDir: ObjCBool = false
        let exists = fileManager.fileExists(
            atPath: root.path, isDirectory: &isDir
        )
        guard exists, isDir.boolValue else {
            return CharacterPackDiscoveryResult()
        }
        let children: [URL]
        do {
            children = try fileManager.contentsOfDirectory(
                at: root,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )
        } catch {
            return CharacterPackDiscoveryResult(
                skipped: [root.path: "unreadable: \(error.localizedDescription)"]
            )
        }
        // Sort on lastPathComponent for deterministic ordering across
        // filesystems (APFS isn't guaranteed-ordered).
        let sortedChildren = children.sorted {
            $0.lastPathComponent < $1.lastPathComponent
        }

        var packsById: [String: CharacterPackLoader.LoadedPack] = [:]
        var orderedIds: [String] = []
        var skipped: [String: String] = [:]

        for child in sortedChildren {
            var childIsDir: ObjCBool = false
            _ = fileManager.fileExists(atPath: child.path, isDirectory: &childIsDir)
            if !childIsDir.boolValue {
                continue
            }
            do {
                let loaded = try loader.load(from: child)
                // Parity with Python's ``load_manifest``: treat a
                // pack whose manifest claims required states it
                // doesn't actually provide as *invalid*. The
                // single-pack ``load`` API keeps surfacing this as
                // diagnostics for callers that want the half-broken
                // pack anyway, but the registry — which is what the
                // UI / bridge consumes — only hands out usable
                // packs.
                if !loaded.missingRequiredStates.isEmpty {
                    skipped[child.path] =
                        "missing required states: \(loaded.missingRequiredStates)"
                    continue
                }
                // Later entries win on id collision — matches the
                // Python registry's dict-overwrite behaviour.
                if packsById[loaded.manifest.id] == nil {
                    orderedIds.append(loaded.manifest.id)
                }
                packsById[loaded.manifest.id] = loaded
            } catch {
                skipped[child.path] = String(describing: error)
            }
        }

        let packs = orderedIds.compactMap { packsById[$0] }
        return CharacterPackDiscoveryResult(packs: packs, skipped: skipped)
    }
}


/// In-memory catalog of loaded packs + active-pack resolver.
///
/// Mutable by design — tests register / unregister freely, and a
/// future hot-reload path may re-scan the packs dir when the user
/// drops a new pack in. Thread-safety is the caller's concern; the
/// registry assumes it runs on the main actor / single thread.
public final class CharacterPackRegistry: @unchecked Sendable {
    private var packs: [String: CharacterPackLoader.LoadedPack] = [:]
    private var order: [String] = []

    public init(_ loaded: [CharacterPackLoader.LoadedPack] = []) {
        for pack in loaded {
            register(pack)
        }
    }

    // MARK: - Mutation

    public func register(_ pack: CharacterPackLoader.LoadedPack) {
        if packs[pack.manifest.id] == nil {
            order.append(pack.manifest.id)
        }
        packs[pack.manifest.id] = pack
    }

    public func unregister(id: String) {
        packs.removeValue(forKey: id)
        order.removeAll(where: { $0 == id })
    }

    // MARK: - Read

    public var count: Int { packs.count }

    public var ids: [String] { order }

    public func all() -> [CharacterPackLoader.LoadedPack] {
        order.compactMap { packs[$0] }
    }

    public func get(id: String) -> CharacterPackLoader.LoadedPack? {
        packs[id]
    }

    public func contains(id: String) -> Bool {
        packs[id] != nil
    }

    // MARK: - Activation

    /// Pick the active pack by priority.
    ///
    /// Resolution order:
    ///
    /// 1. ``preferred`` if it resolves.
    /// 2. Each id in ``fallbackOrder`` in turn (defaults to the
    ///    built-in pixel pack).
    /// 3. The first registered pack.
    /// 4. ``nil`` if the registry is empty.
    public func selectActivePack(
        preferred: String? = nil,
        fallbackOrder: [String] = [
            CharacterPackEnv.builtinPackId,
            CharacterPackEnv.legacyPixelPackId,
        ]
    ) -> CharacterPackLoader.LoadedPack? {
        if let preferred = preferred, let match = packs[preferred] {
            return match
        }
        for candidate in fallbackOrder {
            if let match = packs[candidate] {
                return match
            }
        }
        if let first = order.first {
            return packs[first]
        }
        return nil
    }
}


public enum CharacterPackActivation {

    /// Return the packs directory, honouring :data:`CharacterPackEnv.packsDirVar`.
    public static func defaultPacksDir(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> URL {
        if let raw = environment[CharacterPackEnv.packsDirVar],
           !raw.trimmingCharacters(in: .whitespaces).isEmpty {
            return URL(fileURLWithPath: (raw as NSString).expandingTildeInPath)
        }
        return homeDirectory
            .appendingPathComponent(".deskmate")
            .appendingPathComponent("packs")
    }

    /// Convenience: build a registry from the default packs root
    /// plus any ``extraRoots`` (typically a bundled ``assets/packs``
    /// directory). Later roots override earlier ones on id collision.
    public static func buildDefaultRegistry(
        extraRoots: [URL] = [],
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default,
        loader: CharacterPackLoader = CharacterPackLoader()
    ) -> CharacterPackRegistry {
        let registry = CharacterPackRegistry()
        let roots = [defaultPacksDir(environment: environment)] + extraRoots
        for root in roots {
            let result = CharacterPackDiscovery.discoverPacks(
                in: root, fileManager: fileManager, loader: loader
            )
            for pack in result.packs {
                registry.register(pack)
            }
        }
        return registry
    }

    /// Resolve the active pack applying the process env override.
    public static func resolveActivePack(
        in registry: CharacterPackRegistry,
        preferred: String? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> CharacterPackLoader.LoadedPack? {
        let envOverride = environment[CharacterPackEnv.activePackVar]?
            .trimmingCharacters(in: .whitespaces)
        let effective = (preferred?.isEmpty == false ? preferred : nil)
            ?? (envOverride?.isEmpty == false ? envOverride : nil)
        return registry.selectActivePack(preferred: effective)
    }

    /// Resolve the visible avatar style. Manifest-backed packs win;
    /// the legacy direct style env var is only a no-pack fallback.
    public static func resolveAvatarStyle(
        in registry: CharacterPackRegistry,
        preferred: String? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> String {
        if let pack = resolveActivePack(
            in: registry,
            preferred: preferred,
            environment: environment
        ) {
            return pack.manifest.avatar.defaultStyle
        }
        let legacy = environment[CharacterPackEnv.avatarStyleVar]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return legacy?.isEmpty == false ? legacy! : "pixel"
    }
}
