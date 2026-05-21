// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "DeskmateApp",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .library(
            name: "DeskmateCore",
            targets: ["DeskmateCore"]
        ),
        .executable(
            name: "DeskmateCoreSmoke",
            targets: ["DeskmateCoreSmoke"]
        ),
        .executable(
            name: "DeskmateShellApp",
            targets: ["DeskmateShellApp"]
        ),
        .executable(
            name: "DeskmateMenuBarApp",
            targets: ["DeskmateMenuBarApp"]
        ),
    ],
    targets: [
        .target(
            name: "DeskmateCore",
            path: "Sources/DeskmateCore"
        ),
        // CLI-runnable acceptance probe that doesn't require Xcode. Mirrors
        // every assertion in `Tests/DeskmateCoreTests/` so CI and machines
        // with only Command Line Tools can still verify Phase 0.
        .executableTarget(
            name: "DeskmateCoreSmoke",
            dependencies: ["DeskmateCore"],
            path: "Sources/DeskmateCoreSmoke"
        ),
        // Minimal daemon-style binary that boots the whole DeskmateShell
        // + PerceptionSampler against the real ~/Library/Application
        // Support/Deskmate/ipc.sock path. Prints bridge / domain state
        // transitions so you can eyeball end-to-end behaviour with a
        // running Python agent. SwiftUI MenuBarExtra / pet overlay
        // windows arrive in a follow-up phase.
        .executableTarget(
            name: "DeskmateShellApp",
            dependencies: ["DeskmateCore"],
            path: "Sources/DeskmateShellApp"
        ),
        // SwiftUI MenuBarExtra front-end — the first surface the user
        // can actually click. Consumes ``DeskmateShell``'s four stores
        // (domain + session list + reminders + approvals) and fires
        // :class:`InteractionAction` s back via the same bridge. Runs
        // as ``.accessory`` so the binary shows a menu-bar icon but
        // no Dock tile.
        .executableTarget(
            name: "DeskmateMenuBarApp",
            dependencies: ["DeskmateCore"],
            path: "Sources/DeskmateMenuBarApp"
        ),
        // XCTest suites — require a full Xcode installation (the Command Line
        // Tools toolchain does not ship XCTest). Keep these for Xcode dev
        // loops; use DeskmateCoreSmoke for headless verification.
        .testTarget(
            name: "DeskmateCoreTests",
            dependencies: ["DeskmateCore"],
            path: "Tests/DeskmateCoreTests"
        ),
    ]
)
