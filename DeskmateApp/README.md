# DeskmateApp (Swift)

Swift side of Deskmate: shell layer (Pet / Island / MenuBar / Perception / IPC).
It ships the stable `DeskmateCore` library, a headless shell executable, and
the SwiftUI menu-bar/pet/island front end.

## Targets

| Target | Kind | Notes |
|---|---|---|
| `DeskmateCore` | library | Protocol, state stores, reducers, IPC, perception, perf metrics. |
| `DeskmateCoreSmoke` | executable | CLI-runnable acceptance probe. Requires no Xcode. |
| `DeskmateShellApp` | executable | Headless runtime against the real bridge socket. |
| `DeskmateMenuBarApp` | executable | SwiftUI menu-bar, island, and pet overlay app. |
| `DeskmateCoreTests` | testTarget | XCTest suite, requires a full Xcode installation. |

## Verify

```bash
# Works on any machine with Command Line Tools (no Xcode needed):
swift run DeskmateCoreSmoke
swift build --product DeskmateShellApp
swift build --product DeskmateMenuBarApp

# Also works if a full Xcode is installed and selected:
swift test
```

Both paths assert the same acceptance criteria:

- Envelope JSON round-trip preserves `trace_id` and unknown payload keys.
- Envelope type raw values match `shared/protocol.md` verbatim.
- `IslandSurfaceKind` exposes exactly the five L1-E kinds.
- `PetPresentationState` / `DomainState` defaults match the V10 plan.
- `BubbleSpec` defaults (ttl_ms / priority) match `shared/protocol.md`.
- `CharacterPackManifest` detects missing `required_states`, honours
  `fallbacks`, and terminates on cycles.
- `DeskmateLog.traceId` restores across `withTraceId` scopes and propagates
  across async child tasks (`@TaskLocal` contract).

## Source layout

```
Sources/
├── DeskmateCore/         # library — stable public API
├── DeskmateCoreSmoke/    # CLI acceptance probe
├── DeskmateShellApp/     # headless runtime
└── DeskmateMenuBarApp/   # user-facing SwiftUI shell
Tests/
└── DeskmateCoreTests/    # XCTest suites (Xcode-only)
```
