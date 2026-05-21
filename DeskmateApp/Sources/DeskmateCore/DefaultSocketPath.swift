import Foundation

/// Canonical Unix socket location the Python agent binds and the Swift
/// shell connects to (V10 L3-D3). Matches
/// :func:`deskmate_agent.bridge.paths.default_socket_path`.
///
/// Production value on macOS:
///
/// ```
/// ~/Library/Application Support/Deskmate/ipc.sock
/// ```
///
/// The ``DESKMATE_SOCKET_PATH`` environment variable overrides the
/// resolved path so dev / e2e harnesses can target a temporary
/// socket without touching the user's real Deskmate install. This
/// mirrors the same env override on the Python side
/// (:func:`deskmate_agent.main._resolved_socket_path`).
public enum DefaultSocketPath {
    /// Env var clients can set to redirect this resolver. Empty
    /// strings are treated as "unset" so a stray ``export`` doesn't
    /// silently break the agent.
    public static let envOverrideName = "DESKMATE_SOCKET_PATH"

    /// Return the canonical path. Creates nothing on disk — the server
    /// end is responsible for making the parent directory.
    public static func current(
        fileManager: FileManager = .default,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> String {
        if let raw = environment[envOverrideName],
           !raw.trimmingCharacters(in: .whitespaces).isEmpty
        {
            return (raw as NSString).expandingTildeInPath
        }
        let appSupport = fileManager.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        ).first ?? URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Application Support")
        return appSupport
            .appendingPathComponent("Deskmate")
            .appendingPathComponent("ipc.sock")
            .path
    }
}
