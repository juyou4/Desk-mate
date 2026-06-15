import Foundation
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif
import DeskmateCore
#if canImport(SwiftUI)
import SwiftUI
#endif

// MARK: - Minimal test harness
//
// We don't depend on XCTest / swift-testing because the Command Line Tools
// toolchain ships neither. This harness collects failures, prints a summary,
// and exits non-zero when any check fails — the same contract CI expects.

final class SmokeRunner {
    private var current: String = "<uninitialized>"
    private var failures: [String] = []
    private var passed: Int = 0

    func test(_ name: String, _ body: () throws -> Void) {
        current = name
        do {
            try body()
            passed += 1
            print("  ok   \(name)")
        } catch {
            failures.append("\(name): \(error)")
            print("  FAIL \(name) — \(error)")
        }
    }

    func expect(_ cond: @autoclosure () -> Bool, _ message: String,
                file: StaticString = #file, line: UInt = #line) throws {
        if !cond() {
            throw SmokeError.expectation("\(file):\(line) — \(message)")
        }
    }

    func finish() -> Int32 {
        print("")
        print("Passed: \(passed)   Failed: \(failures.count)")
        if failures.isEmpty {
            print("✅ DeskmateCore Phase 0 + 2a + 2b + 3a + 3b + 4 + 5 + 6 + 7 acceptance OK")
            return 0
        }
        for f in failures { print(" · \(f)") }
        print("❌ DeskmateCore Phase 0 + 2a + 2b + 3a + 3b + 4 + 5 + 6 + 7 acceptance FAILED")
        return 1
    }
}

enum SmokeError: Error, CustomStringConvertible {
    case expectation(String)
    var description: String {
        if case .expectation(let m) = self { return m }
        return "unknown"
    }
}

// MARK: - Test groups

let runner = SmokeRunner()

// --- BridgeEnvelope (L1 / L3-D) ---------------------------------------------

let encoder: JSONEncoder = {
    let e = JSONEncoder()
    e.outputFormatting = []
    return e
}()
let decoder = JSONDecoder()

runner.test("envelope: round trip preserves trace_id") {
    let env = BridgeEnvelope.of(.userMessage, payload: ["text": .string("hi")])
    let data = try encoder.encode(env)
    let restored = try decoder.decode(BridgeEnvelope.self, from: data)
    try runner.expect(restored.traceId == env.traceId, "trace_id changed")
    try runner.expect(restored.type == .userMessage, "type mismatch")
    try runner.expect(restored.payload["text"] == .string("hi"), "payload lost")
}

runner.test("envelope: forward-compatible payload keys survive") {
    let traceId = BridgeEnvelope.newTraceId()
    let raw = """
    {
      "spec_version": 1,
      "type": "user.message",
      "trace_id": "\(traceId)",
      "payload": { "text": "hi", "future_hint": [1, 2, 3] }
    }
    """.data(using: .utf8)!
    let env = try decoder.decode(BridgeEnvelope.self, from: raw)
    try runner.expect(
        env.payload["future_hint"] == .array([.int(1), .int(2), .int(3)]),
        "future_hint did not survive decode"
    )
    let reEncoded = try encoder.encode(env)
    let second = try decoder.decode(BridgeEnvelope.self, from: reEncoded)
    try runner.expect(
        second.payload["future_hint"] == env.payload["future_hint"],
        "future_hint did not survive re-encode"
    )
}

runner.test("envelope: trace_id is hex32 lowercase") {
    let id = BridgeEnvelope.newTraceId()
    try runner.expect(id.count == 32, "length != 32")
    try runner.expect(id.allSatisfy { "0123456789abcdef".contains($0) }, "non-hex char")
}

runner.test("envelope: type raw values match shared/protocol.md") {
    let expected: [EnvelopeType: String] = [
        .perception: "perception",
        .userMessage: "user.message",
        .userClickPet: "user.click_pet",
        .interaction: "interaction",
        .intent: "intent",
        .ping: "ping",
        .pong: "pong",
        .stateSnapshotRequest: "state.snapshot.request",
        .stateSnapshot: "state.snapshot",
        .agentReady: "agent.ready",
        .agentPause: "agent.pause",
    ]
    for (kind, raw) in expected {
        try runner.expect(kind.rawValue == raw, "\(kind) raw = \(kind.rawValue), want \(raw)")
    }
}

runner.test("envelope: default payload encodes as empty object") {
    let env = BridgeEnvelope.of(.ping)
    let data = try encoder.encode(env)
    let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
    let payload = json?["payload"] as? [String: Any]
    try runner.expect(payload?.count == 0, "default payload should be {}")
}

// --- InteractionAction (L1-F / I8) ------------------------------------------

runner.test("interaction: typed action encodes dotted kind") {
    let act = InteractionAction(
        source: .island,
        target: .session,
        kind: .permissionResolve,
        payload: ["allow": .bool(true)]
    )
    let data = try encoder.encode(act)
    let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
    try runner.expect(json?["kind"] as? String == "permission.resolve", "kind raw mismatch")
    try runner.expect(json?["source"] as? String == "island", "source raw mismatch")
}

runner.test("interaction: rejects unknown kind") {
    let raw = """
    { "source": "island", "target": "session", "kind": "totally.invented", "payload": {} }
    """.data(using: .utf8)!
    var threw = false
    do {
        _ = try decoder.decode(InteractionAction.self, from: raw)
    } catch {
        threw = true
    }
    try runner.expect(threw, "decoder accepted unknown kind")
}

runner.test("interaction: preserves unknown payload keys") {
    let raw = """
    {
      "source": "pet", "target": "bubble", "kind": "pet.interact",
      "payload": { "gesture": "pat", "future_hint": 7 }
    }
    """.data(using: .utf8)!
    let act = try decoder.decode(InteractionAction.self, from: raw)
    try runner.expect(act.payload["future_hint"] == .int(7), "future_hint lost")
}

// --- SurfaceState (L1-E / I1 / I5) ------------------------------------------

runner.test("island: IslandSurfaceKind matches L1-E") {
    let raws = Set(IslandSurfaceKind.allCases.map(\.rawValue))
    try runner.expect(
        raws == ["compact", "notification_card", "session_list", "live_activity", "empty"],
        "L1-E mandates exactly these 5 kinds, got \(raws)"
    )
}

runner.test("island: default surface is compact") {
    let s = IslandSurfaceState()
    try runner.expect(s.kind == .compact, "default should be .compact")
    try runner.expect(s.sessionId == nil, "no session by default")
}

runner.test("pet: default presentation state") {
    let p = PetPresentationState()
    try runner.expect(p.anchorKind == .desktop, "anchor should default to .desktop")
    try runner.expect(p.velocity == PetVelocity(), "velocity should default to zero")
    try runner.expect(p.avatarStyle == "pixel", "avatar style should default to pixel")
    try runner.expect(p.isInteractive, "isInteractive should default true")
}

runner.test("pet: anchor + nest policy decode") {
    let anchorJSON = #"""
    { "kind": "nest", "target_nest": "notch", "future": true }
    """#.data(using: .utf8)!
    let anchor = try decoder.decode(PetAnchor.self, from: anchorJSON)
    try runner.expect(anchor.kind == .nest, "anchor kind")
    try runner.expect(anchor.targetNest == "notch", "target nest")

    let policyJSON = #"{ "should_leave_nest": true }"#.data(using: .utf8)!
    let policy = try decoder.decode(NestBehaviorPolicy.self, from: policyJSON)
    try runner.expect(policy.canEnterNest, "can_enter_nest defaults true")
    try runner.expect(policy.shouldLeaveNest, "should_leave_nest decoded")
}

runner.test("top customization: store publishes changes") {
    let store = TopSurfaceCustomizationStore()
    var seen: [TopSurfaceCustomization] = []
    let unsubscribe = store.subscribe { seen.append($0) }
    let next = TopSurfaceCustomization(
        theme: "dark",
        fontScale: 1.1,
        buddyStyle: "emoji",
        showBuddy: false,
        hardwareNotchMode: .forceNotched,
        screenGeometries: [
            ScreenGeometrySpec(
                screenId: "main",
                x: 0,
                y: 0,
                width: 1512,
                height: 982
            )
        ],
        hoverSpeed: 1.25
    )
    try runner.expect(store.apply(next), "first apply should notify")
    try runner.expect(seen == [next], "subscriber should see new state")
    try runner.expect(!store.apply(next), "same state should dedupe")
    unsubscribe()
    try runner.expect(store.subscriberCount == 0, "unsubscribe")
}

// V10 island polish: ``IslandWindowController`` subscribes to the
// store and stashes the unsubscribe closure in ``customizationUnsub``.
// On ``close()`` it fires the closure to drop the callback. This test
// locks the contract: multiple subscribers can register and each
// unsubscribe call drops exactly one callback so the controller's
// re-install / close cycle never leaks closures.
runner.test("top customization: multiple subscribers unsubscribe independently") {
    let store = TopSurfaceCustomizationStore()
    var aHits = 0
    var bHits = 0
    let unsubA = store.subscribe { _ in aHits += 1 }
    let unsubB = store.subscribe { _ in bHits += 1 }
    try runner.expect(store.subscriberCount == 2, "two subs registered")

    var next = TopSurfaceCustomization()
    next.hardwareNotchMode = .forceFlat
    try runner.expect(store.apply(next), "apply should notify both")
    try runner.expect(aHits == 1 && bHits == 1, "both subscribers fire once")

    unsubA()
    try runner.expect(store.subscriberCount == 1, "A removed, B remains")
    var next2 = next
    next2.theme = "dark"
    try runner.expect(store.apply(next2), "second apply should notify only B")
    try runner.expect(aHits == 1 && bHits == 2, "only B fires after unsubA")

    unsubB()
    try runner.expect(store.subscriberCount == 0, "all subscribers cleared")
}

// V10 island polish: hardwareNotchMode flows through the bridge as
// snake_case JSON — the menu-bar runtime decodes a customization
// payload via the same path. Lock the round-trip so a Python-side
// rename can't silently put the controller back in
// ``.automatic``.
runner.test("top customization: hardwareNotchMode JSON round-trips through decoder") {
    let json = #"""
    {
      "spec_version": 1,
      "theme": "system",
      "font_scale": 1.0,
      "buddy_style": "pixel",
      "show_buddy": true,
      "hardware_notch_mode": "force_flat",
      "screen_geometries": [],
      "hover_speed": 1.0
    }
    """#.data(using: .utf8)!
    let value = try JSONDecoder().decode(TopSurfaceCustomization.self, from: json)
    try runner.expect(
        value.hardwareNotchMode == .forceFlat,
        "force_flat should map to .forceFlat"
    )

    let encoded = try JSONEncoder().encode(value)
    let payload = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]
    try runner.expect(
        payload?["hardware_notch_mode"] as? String == "force_flat",
        "encoder should emit snake_case force_flat"
    )
}

runner.test("bubble: default decoded fields match protocol doc") {
    let json = #"{ "id": "b1", "text": "hi", "kind": "approval_hint" }"#.data(using: .utf8)!
    let spec = try decoder.decode(BubbleSpec.self, from: json)
    try runner.expect(spec.kind == .approvalHint, "kind should decode from approval_hint")
    try runner.expect(spec.ttlMs == 8000, "ttl_ms default should be 8000")
    try runner.expect(spec.priority == .p2, "priority default should be P2")
}

runner.test("domain: default state is idle P3 casual") {
    let ds = DomainState()
    try runner.expect(ds.currentPriority == .p3, "priority default")
    try runner.expect(ds.userFocus == .casual, "focus default")
    try runner.expect(ds.agentMood == .idle, "mood default")
    try runner.expect(ds.pendingApprovals.isEmpty, "approvals default empty")
}

// --- CharacterPackManifest (L1-D / I4) --------------------------------------

func manifestWithStates(_ pairs: [(String, [String])]) -> CharacterPackManifest {
    var states: [String: StateFrames] = [:]
    for (name, frames) in pairs {
        states[name] = StateFrames(fps: 4, frames: frames)
    }
    return CharacterPackManifest(id: "pixie", displayName: "Pixie", states: states)
}

runner.test("manifest: detects missing required states") {
    let m = manifestWithStates([
        ("idle", ["idle/001.png"]),
        ("working", ["working/001.png"]),
    ])
    try runner.expect(
        m.missingRequiredStates() == ["thinking", "alert"],
        "missing required wrong: \(m.missingRequiredStates())"
    )
}

runner.test("manifest: resolve_state honors default fallbacks") {
    let m = manifestWithStates([
        ("idle", ["idle/001.png"]),
        ("walking", ["walking/001.png"]),
    ])
    try runner.expect(
        m.resolveState("walking_left") == "walking",
        "walking_left should fall back to walking"
    )
    try runner.expect(m.resolveState("nonexistent") == nil, "unknown should resolve to nil")
}

runner.test("manifest: resolve_state detects cycles") {
    var m = manifestWithStates([("idle", ["idle/001.png"])])
    m.fallbacks = ["a": "b", "b": "a"]
    try runner.expect(m.resolveState("a") == nil, "cycle should terminate with nil")
}

runner.test("manifest: forward-compat unknown top-level sections") {
    // Swift's Codable defaults to ignoring unknown keys, so unknown sections
    // simply don't land on the struct but also don't break decoding — good.
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
    try runner.expect(m.displayName == "Pixie", "displayName")
    try runner.expect(m.states["idle"]?.frames == ["idle/001.png"], "frames")
}

runner.test("manifest: snake_case wire format") {
    let m = CharacterPackManifest(id: "pixie", displayName: "Pixie")
    let data = try JSONEncoder().encode(m)
    let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
    try runner.expect(json?["display_name"] != nil, "missing display_name")
    try runner.expect(json?["spec_version"] != nil, "missing spec_version")
    try runner.expect(json?["required_states"] != nil, "missing required_states")
    try runner.expect(json?["displayName"] == nil, "camelCase leaked into wire format")
}

// --- Logging (L3 Instrumentation) -------------------------------------------

runner.test("log: withTraceId restores previous value") {
    try DeskmateLog.withTraceId("outer") {
        try runner.expect(DeskmateLog.traceId == "outer", "outer binding missing")
        try DeskmateLog.withTraceId("inner") {
            try runner.expect(DeskmateLog.traceId == "inner", "inner binding missing")
        }
        try runner.expect(DeskmateLog.traceId == "outer", "outer not restored")
    }
    try runner.expect(DeskmateLog.traceId == nil, "traceId leaked out of scope")
}

// Async propagation is exercised via a simple task group so we stay CLI-only
// without XCTest; top-level await is wrapped in a Task + sync wait.
let asyncPropagationResult = DispatchSemaphore(value: 0)
var asyncResult: [String: String?] = [:]

Task {
    await DeskmateLog.withTraceId("parent-trace") {
        await withTaskGroup(of: (String, String?).self) { group in
            group.addTask { ("a", DeskmateLog.traceId) }
            group.addTask { ("b", DeskmateLog.traceId) }
            for await pair in group { asyncResult[pair.0] = pair.1 }
        }
    }
    asyncPropagationResult.signal()
}
_ = asyncPropagationResult.wait(timeout: .now() + 5)

runner.test("log: trace_id propagates across async tasks") {
    try runner.expect(asyncResult["a"] == "parent-trace", "child a did not inherit")
    try runner.expect(asyncResult["b"] == "parent-trace", "child b did not inherit")
    try runner.expect(DeskmateLog.traceId == nil, "traceId leaked outside Task")
}

// --- Phase 2a: PetStateMachine (L2-#4) --------------------------------------

func fullPackManifest() -> CharacterPackManifest {
    var states: [String: StateFrames] = [:]
    for name in [
        "idle", "running", "review", "waiting", "jumping", "failed",
        "dozing", "sleeping", "waking", "drag", "react-click",
        "working", "thinking", "alert", "happy", "nesting",
    ] {
        states[name] = StateFrames(fps: 4, frames: ["\(name)/000.png"])
    }
    return CharacterPackManifest(id: "pixie", displayName: "Pixie", states: states)
}

runner.test("petsm: mood maps directly to animation") {
    let m = fullPackManifest()
    for (mood, expected) in PetStateMachine.moodAnimation {
        let out = PetStateMachine.reduce(
            PetStateMachine.Input(domain: DomainState(agentMood: mood)),
            manifest: m
        )
        try runner.expect(
            out.animationState == expected,
            "mood \(mood) expected \(expected), got \(out.animationState)"
        )
    }
}

runner.test("petsm: pending approvals force waiting + concerned + attention=1") {
    let m = fullPackManifest()
    let domain = DomainState(
        currentPriority: .p2,
        agentMood: .happy,
        pendingApprovals: ["t1"]
    )
    let out = PetStateMachine.reduce(PetStateMachine.Input(domain: domain), manifest: m)
    try runner.expect(out.animationState == "waiting", "animation != waiting")
    try runner.expect(out.emotion == "concerned", "emotion != concerned")
    try runner.expect(out.attentionLevel >= 0.99, "attention not pinned to 1.0")
}

runner.test("petsm: animation override wins over mood") {
    let m = fullPackManifest()
    let input = PetStateMachine.Input(
        domain: DomainState(agentMood: .idle),
        animationOverride: "thinking"
    )
    let out = PetStateMachine.reduce(input, manifest: m)
    try runner.expect(out.animationState == "thinking", "override ignored")
}

runner.test("petsm: animation override wins over auto-rest") {
    let m = fullPackManifest()
    let input = PetStateMachine.Input(
        domain: DomainState(agentMood: .idle),
        idleMs: PetStateMachine.sleepingThresholdMs + 1,
        animationOverride: "react-click"
    )
    let out = PetStateMachine.reduce(input, manifest: m)
    try runner.expect(out.animationState == "react-click", "override lost to sleep")
}

runner.test("petsm: nesting switches anchor and animation") {
    let m = fullPackManifest()
    let input = PetStateMachine.Input(
        domain: DomainState(agentMood: .idle),
        isNesting: true
    )
    let out = PetStateMachine.reduce(input, manifest: m)
    try runner.expect(out.animationState == "nesting", "animation != nesting")
    try runner.expect(out.anchorKind == .nest, "anchor != nest")
}

runner.test("petsm: focused user + idle stays idle with low attention") {
    let m = fullPackManifest()
    let domain = DomainState(
        currentPriority: .p3,
        userFocus: .focused,
        agentMood: .idle
    )
    let out = PetStateMachine.reduce(PetStateMachine.Input(domain: domain), manifest: m)
    try runner.expect(out.animationState == "idle", "should stay idle when focused")
    try runner.expect(out.attentionLevel <= 0.15, "attention too high while focused")
}

runner.test("petsm: idle local inactivity dozes then sleeps") {
    let m = fullPackManifest()
    let domain = DomainState(
        currentPriority: .p3,
        userFocus: .casual,
        agentMood: .idle
    )
    let dozing = PetStateMachine.reduce(
        PetStateMachine.Input(
            domain: domain,
            idleMs: PetStateMachine.dozingThresholdMs
        ),
        manifest: m
    )
    try runner.expect(dozing.animationState == "dozing", "expected dozing")
    try runner.expect(dozing.attentionLevel == 0.12, "dozing attention wrong")

    let sleeping = PetStateMachine.reduce(
        PetStateMachine.Input(
            domain: domain,
            idleMs: PetStateMachine.sleepingThresholdMs
        ),
        manifest: m
    )
    try runner.expect(sleeping.animationState == "sleeping", "expected sleeping")
    try runner.expect(sleeping.attentionLevel == 0.05, "sleeping attention wrong")
}

runner.test("petsm: auto-rest does not hide approvals") {
    let m = fullPackManifest()
    let domain = DomainState(
        currentPriority: .p3,
        userFocus: .casual,
        agentMood: .idle,
        pendingApprovals: ["approval-1"]
    )
    let out = PetStateMachine.reduce(
        PetStateMachine.Input(
            domain: domain,
            idleMs: PetStateMachine.sleepingThresholdMs
        ),
        manifest: m
    )
    try runner.expect(out.animationState == "waiting", "approval should stay visible")
    try runner.expect(out.attentionLevel >= 0.99, "approval attention should win")
}

runner.test("petsm: userInteracting locks isInteractive=false") {
    let m = fullPackManifest()
    let input = PetStateMachine.Input(domain: DomainState(), isUserInteracting: true)
    let out = PetStateMachine.reduce(input, manifest: m)
    try runner.expect(out.isInteractive == false, "should be non-interactive while dragging")
}

runner.test("petsm: missing manifest state falls back to idle") {
    var states: [String: StateFrames] = [:]
    states["idle"] = StateFrames(fps: 4, frames: ["idle/000.png"])
    states["working"] = StateFrames(fps: 4, frames: ["working/000.png"])
    let m = CharacterPackManifest(
        id: "pixie",
        displayName: "Pixie",
        states: states,
        fallbacks: [:]
    )
    let input = PetStateMachine.Input(domain: DomainState(agentMood: .thinking))
    let out = PetStateMachine.reduce(input, manifest: m)
    try runner.expect(out.animationState == "idle", "missing state should fall back to idle")
}

// --- Phase 2a: CharacterPackLoader (L2-#2) ----------------------------------

func makeTempPackDir() throws -> URL {
    let base = FileManager.default.temporaryDirectory
    let unique = "deskmate-smoke-pack-\(UUID().uuidString.prefix(8))"
    let dir = base.appendingPathComponent(unique, isDirectory: true)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}

func writeManifest(_ json: String, to dir: URL) throws {
    try json.data(using: .utf8)!.write(to: dir.appendingPathComponent("manifest.json"))
}

func writeTouch(_ rel: String, in dir: URL) throws {
    let url = dir.appendingPathComponent(rel)
    try FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try Data([0x00]).write(to: url)
}

runner.test("pack: load fully resolved pack") {
    let dir = try makeTempPackDir()
    defer { try? FileManager.default.removeItem(at: dir) }
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
    """#, to: dir)
    for state in ["idle", "working", "thinking", "alert"] {
        try writeTouch("\(state)/000.png", in: dir)
    }
    let loaded = try CharacterPackLoader().load(from: dir)
    try runner.expect(loaded.isFullyResolved, "pack should be fully resolved")
    try runner.expect(loaded.manifest.displayName == "Pixie", "displayName mismatch")
}

runner.test("pack: missing manifest throws manifestMissing") {
    let dir = try makeTempPackDir()
    defer { try? FileManager.default.removeItem(at: dir) }
    var threw = false
    do {
        _ = try CharacterPackLoader().load(from: dir)
    } catch CharacterPackLoader.LoadError.manifestMissing {
        threw = true
    } catch {
        threw = false
    }
    try runner.expect(threw, "should throw manifestMissing")
}

runner.test("pack: invalid JSON throws manifestInvalid") {
    let dir = try makeTempPackDir()
    defer { try? FileManager.default.removeItem(at: dir) }
    try writeManifest("{ not json", to: dir)
    var threw = false
    do {
        _ = try CharacterPackLoader().load(from: dir)
    } catch CharacterPackLoader.LoadError.manifestInvalid {
        threw = true
    } catch {
        threw = false
    }
    try runner.expect(threw, "should throw manifestInvalid")
}

runner.test("pack: missing frames reported sorted") {
    let dir = try makeTempPackDir()
    defer { try? FileManager.default.removeItem(at: dir) }
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
    """#, to: dir)
    for state in ["idle", "working", "thinking", "alert"] {
        try writeTouch("\(state)/000.png", in: dir)
    }
    let loaded = try CharacterPackLoader().load(from: dir)
    try runner.expect(
        loaded.missingFrames == ["idle/001.png"],
        "missingFrames != [idle/001.png], got \(loaded.missingFrames)"
    )
}

runner.test("pack: missing required states reported") {
    let dir = try makeTempPackDir()
    defer { try? FileManager.default.removeItem(at: dir) }
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
    """#, to: dir)
    try writeTouch("idle/000.png", in: dir)
    try writeTouch("working/000.png", in: dir)
    let loaded = try CharacterPackLoader().load(from: dir)
    try runner.expect(
        loaded.missingRequiredStates == ["thinking", "alert"],
        "missingRequiredStates wrong: \(loaded.missingRequiredStates)"
    )
}

runner.test("pack: bundled built-in pack has every frame on disk") {
    // V10 L1-D / A1+A13: the shipped pack must round-trip through the
    // loader as ``isFullyResolved``. Walk up from this Swift source
    // file to the repo's built-in pack directory.
    let smokeFile = URL(fileURLWithPath: #file)
    let bundled = smokeFile
        .deletingLastPathComponent()  // DeskmateCoreSmoke
        .deletingLastPathComponent()  // Sources
        .deletingLastPathComponent()  // DeskmateApp
        .deletingLastPathComponent()  // <repo root>
        .appendingPathComponent("assets")
        .appendingPathComponent("packs")
        .appendingPathComponent(CharacterPackEnv.builtinPackId)
    try runner.expect(
        FileManager.default.fileExists(atPath: bundled.path),
        "bundled pack root missing at \(bundled.path)"
    )
    let loaded = try CharacterPackLoader().load(from: bundled)
    try runner.expect(
        loaded.manifest.id == CharacterPackEnv.builtinPackId,
        "manifest id mismatch: \(loaded.manifest.id)"
    )
    try runner.expect(
        loaded.missingFrames.isEmpty,
        "bundled pack still has missing frames: \(loaded.missingFrames)"
    )
    try runner.expect(
        loaded.missingRequiredStates.isEmpty,
        "bundled pack missing required states: \(loaded.missingRequiredStates)"
    )
    try runner.expect(
        loaded.isFullyResolved,
        "bundled built-in pack should be fully resolved"
    )
}

// --- Phase 2a: BubbleQueue (V10 I3) -----------------------------------------

func mkBubble(_ id: String, priority: Priority = .p2, ttl: Int? = 8000) -> BubbleSpec {
    BubbleSpec(id: id, kind: .chat, text: id, ttlMs: ttl, priority: priority)
}

runner.test("bq: FIFO within same priority") {
    var q = BubbleQueue(maxActive: 10)
    q.enqueue(mkBubble("a"), nowMs: 100)
    q.enqueue(mkBubble("b"), nowMs: 200)
    q.enqueue(mkBubble("c"), nowMs: 300)
    let order = [q.dequeue(nowMs: 400)?.id,
                 q.dequeue(nowMs: 401)?.id,
                 q.dequeue(nowMs: 402)?.id]
    try runner.expect(order == ["a", "b", "c"], "FIFO broke: \(order)")
}

runner.test("bq: higher priority jumps queue") {
    var q = BubbleQueue(maxActive: 10)
    q.enqueue(mkBubble("p2a", priority: .p2), nowMs: 100)
    q.enqueue(mkBubble("p0",  priority: .p0), nowMs: 200)
    q.enqueue(mkBubble("p2b", priority: .p2), nowMs: 300)
    try runner.expect(q.dequeue(nowMs: 400)?.id == "p0", "p0 should lead")
    try runner.expect(q.dequeue(nowMs: 401)?.id == "p2a", "p2a next")
    try runner.expect(q.dequeue(nowMs: 402)?.id == "p2b", "p2b last")
}

runner.test("bq: expired pruned on dequeue") {
    var q = BubbleQueue(maxActive: 10)
    q.enqueue(mkBubble("short", ttl: 100), nowMs: 0)
    q.enqueue(mkBubble("long",  ttl: 10_000), nowMs: 0)
    try runner.expect(q.dequeue(nowMs: 500)?.id == "long", "expired short should be gone")
}

runner.test("bq: overflow evicts lowest priority oldest") {
    var q = BubbleQueue(maxActive: 3)
    q.enqueue(mkBubble("p3a", priority: .p3), nowMs: 100)
    q.enqueue(mkBubble("p2a", priority: .p2), nowMs: 200)
    q.enqueue(mkBubble("p2b", priority: .p2), nowMs: 300)
    q.enqueue(mkBubble("p1",  priority: .p1), nowMs: 400)
    let ids = Set(q.allEntries.map(\.spec.id))
    try runner.expect(!ids.contains("p3a"), "p3a should have been evicted")
    try runner.expect(ids.contains("p1"), "p1 should remain")
    try runner.expect(q.count == 3, "queue over capacity: \(q.count)")
}

// --- V10 L3-B1: streaming bubble updates ------------------------------------

runner.test("bq: update rewrites existing entry text") {
    var q = BubbleQueue(maxActive: 4)
    q.enqueue(mkBubble("reply"), nowMs: 100)
    let patched = q.update(id: "reply", text: "hello", nowMs: 110)
    try runner.expect(patched, "update should report a hit")
    try runner.expect(q.peek(nowMs: 120)?.text == "hello", "text not patched")
}

runner.test("bq: update preserves enqueue order vs newer entries") {
    var q = BubbleQueue(maxActive: 5)
    q.enqueue(mkBubble("a"), nowMs: 100)
    q.enqueue(mkBubble("b"), nowMs: 200)
    _ = q.update(id: "a", text: "patched", nowMs: 250)
    let next = q.dequeue(nowMs: 300)
    try runner.expect(next?.id == "a", "patched 'a' should still lead FIFO order")
    try runner.expect(next?.text == "patched", "text mismatch after update")
}

runner.test("bq: update miss returns false and is a no-op") {
    var q = BubbleQueue(maxActive: 4)
    q.enqueue(mkBubble("only"), nowMs: 100)
    let patched = q.update(id: "ghost", text: "noop", nowMs: 110)
    try runner.expect(!patched, "update should miss")
    try runner.expect(q.peek(nowMs: 120)?.id == "only", "queue mutated unexpectedly")
}

runner.test("bq: update with refreshTtl postpones expiry") {
    var q = BubbleQueue(maxActive: 4)
    q.enqueue(mkBubble("late", ttl: 100), nowMs: 0)
    _ = q.update(id: "late", text: "still here", nowMs: 80, refreshTtl: true)
    try runner.expect(q.peek(nowMs: 150)?.id == "late", "ttl should be pushed forward")
}

// --- V10 L3-B1: CompanionIntentDispatcher patch decoding --------------------

runner.test("intent: decodeBubblePatch parses bubble_id + text") {
    let intent = CompanionIntent(
        kind: .updatePetBubble,
        payload: [
            "bubble_id": .string("reply"),
            "text": .string("hello"),
        ]
    )
    let patch = try CompanionIntentDispatcher.decodeBubblePatch(from: intent)
    try runner.expect(patch.bubbleId == "reply", "bubble_id mismatch")
    try runner.expect(patch.text == "hello", "text mismatch")
    try runner.expect(patch.markdown == nil, "markdown should default nil")
}

runner.test("intent: decodeBubblePatch surfaces missing fields") {
    let missingId = CompanionIntent(
        kind: .updatePetBubble,
        payload: ["text": .string("hi")]
    )
    do {
        _ = try CompanionIntentDispatcher.decodeBubblePatch(from: missingId)
        try runner.expect(false, "should have thrown for missing bubble_id")
    } catch {
        // expected
    }
    let missingText = CompanionIntent(
        kind: .updatePetBubble,
        payload: ["bubble_id": .string("reply")]
    )
    do {
        _ = try CompanionIntentDispatcher.decodeBubblePatch(from: missingText)
        try runner.expect(false, "should have thrown for missing text")
    } catch {
        // expected
    }
}

runner.test("intent: bindBubbleQueue routes update_pet_bubble to queue.update") {
    let queue = LiveBubbleQueue(maxActive: 4)
    let dispatcher = CompanionIntentDispatcher()
    var decodeErrors: [Error] = []
    dispatcher.bindBubbleQueue(to: queue) { decodeErrors.append($0) }

    queue.enqueue(BubbleSpec(id: "reply", kind: .chat, text: "…", ttlMs: 30_000))

    let firstToken = CompanionIntent(
        kind: .updatePetBubble,
        payload: [
            "bubble_id": .string("reply"),
            "text": .string("Hel"),
        ]
    )
    _ = dispatcher.dispatch(firstToken)
    try runner.expect(queue.peek()?.text == "Hel", "first token not patched")

    let secondToken = CompanionIntent(
        kind: .updatePetBubble,
        payload: [
            "bubble_id": .string("reply"),
            "text": .string("Hello!"),
        ]
    )
    _ = dispatcher.dispatch(secondToken)
    try runner.expect(queue.peek()?.text == "Hello!", "second token not patched")
    try runner.expect(decodeErrors.isEmpty, "no decode errors expected: \(decodeErrors)")
}

runner.test("intent: update for missing bubble id reports decode error") {
    let queue = LiveBubbleQueue(maxActive: 4)
    let dispatcher = CompanionIntentDispatcher()
    var errorCount = 0
    dispatcher.bindBubbleQueue(to: queue) { _ in errorCount += 1 }

    let ghost = CompanionIntent(
        kind: .updatePetBubble,
        payload: [
            "bubble_id": .string("never-shown"),
            "text": .string("oops"),
        ]
    )
    _ = dispatcher.dispatch(ghost)
    try runner.expect(errorCount == 1, "expected 1 decode error, got \(errorCount)")
    try runner.expect(queue.isEmpty, "queue should remain empty")
}

// --- Phase 2b: PetWindowGeometry (L2-#1) ------------------------------------

runner.test("geo: origin inside screen is preserved") {
    let g = PetWindowGeometry(
        screens: [PetScreen(id: 0, visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 900))],
        petSize: CGSize(width: 64, height: 64),
        edgeMargin: 8
    )
    let r = g.clamp(requested: CGPoint(x: 500, y: 300))
    try runner.expect(r?.origin == CGPoint(x: 500, y: 300), "should not clamp")
    try runner.expect(r?.didClamp == false, "didClamp should be false")
}

runner.test("geo: offscreen origin is clamped into margin") {
    let g = PetWindowGeometry(
        screens: [PetScreen(id: 0, visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 900))],
        petSize: CGSize(width: 64, height: 64),
        edgeMargin: 8
    )
    let r = g.clamp(requested: CGPoint(x: -500, y: -500))
    try runner.expect(r?.origin == CGPoint(x: 8, y: 8), "should clamp to margin")
    try runner.expect(r?.didClamp == true, "didClamp should flag correction")
}

runner.test("geo: picks the screen containing the pet centre") {
    let g = PetWindowGeometry(
        screens: [
            PetScreen(id: 0, visibleFrame: CGRect(x: 0, y: 0, width: 1440, height: 900)),
            PetScreen(id: 1, visibleFrame: CGRect(x: 1440, y: 0, width: 2560, height: 1440)),
        ],
        petSize: CGSize(width: 64, height: 64)
    )
    let r = g.clamp(requested: CGPoint(x: 2000, y: 500))
    try runner.expect(r?.screenId == 1, "expected screen 1 for centre (2032,532)")
}

runner.test("geo: empty screens yields nil") {
    let g = PetWindowGeometry(screens: [], petSize: CGSize(width: 64, height: 64))
    try runner.expect(g.clamp(requested: .zero) == nil, "no screens → nil")
    try runner.expect(g.defaultOrigin() == nil, "no screens → nil")
}

// --- Phase 2b: PetDragController (L2-#1) ------------------------------------

runner.test("drag: short press releases as tap") {
    var c = PetDragController()
    _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
    let out = c.apply(.mouseUp(point: CGPoint(x: 1, y: 1), tsMs: 50))
    try runner.expect(out == .tap(at: CGPoint(x: 1, y: 1)), "expected tap, got \(out)")
}

runner.test("drag: moving past threshold begins drag") {
    var c = PetDragController(clickThresholdPx: 4)
    _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
    _ = c.apply(.mouseDragged(point: CGPoint(x: 2, y: 0), tsMs: 10))  // below
    let began = c.apply(.mouseDragged(point: CGPoint(x: 10, y: 0), tsMs: 20))
    try runner.expect(began == .beganDrag(from: .zero), "expected beganDrag")
}

runner.test("drag: delta is relative to the previous point") {
    var c = PetDragController(clickThresholdPx: 1)
    _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
    _ = c.apply(.mouseDragged(point: CGPoint(x: 5, y: 5), tsMs: 10))
    let d = c.apply(.mouseDragged(point: CGPoint(x: 8, y: 7), tsMs: 20))
    try runner.expect(d == .drag(delta: CGPoint(x: 3, y: 2)), "delta wrong: \(d)")
}

runner.test("drag: mouse up after drag returns endedDrag") {
    var c = PetDragController(clickThresholdPx: 1)
    _ = c.apply(.mouseDown(point: .zero, tsMs: 0))
    _ = c.apply(.mouseDragged(point: CGPoint(x: 20, y: 0), tsMs: 10))
    let end = c.apply(.mouseUp(point: CGPoint(x: 30, y: 0), tsMs: 20))
    try runner.expect(end == .endedDrag(to: CGPoint(x: 30, y: 0)), "expected endedDrag")
}

// --- Phase 2b: PetFrameAnimator (L2-#1) -------------------------------------

runner.test("anim: frame duration derived from fps") {
    let a = PetFrameAnimator(fps: 4, frameCount: 2)
    try runner.expect(a.frameDurationMs == 250, "250ms/frame at 4fps, got \(a.frameDurationMs)")
}

runner.test("anim: frame index loops") {
    let a = PetFrameAnimator(fps: 4, frameCount: 3, loops: true)
    try runner.expect(a.frameIndex(elapsedMs: 0) == 0, "0ms → frame 0")
    try runner.expect(a.frameIndex(elapsedMs: 250) == 1, "250ms → frame 1")
    try runner.expect(a.frameIndex(elapsedMs: 500) == 2, "500ms → frame 2")
    try runner.expect(a.frameIndex(elapsedMs: 750) == 0, "wrap back to 0")
}

runner.test("anim: non-looping clamps at final frame") {
    let a = PetFrameAnimator(fps: 4, frameCount: 3, loops: false)
    try runner.expect(a.frameIndex(elapsedMs: 10_000) == 2, "should clamp to 2")
    try runner.expect(a.isFinished(elapsedMs: 750), "should be finished at 750ms")
    try runner.expect(a.totalDurationMs == 750, "totalDuration should be 750ms")
}

// --- Phase 3a: IslandGeometry (L2-#7) ---------------------------------------

let islandScreen = CGRect(x: 0, y: 0, width: 1512, height: 982)

runner.test("island-geo: compact hugs notch when present") {
    let notch = CGSize(width: 200, height: 32)
    let g = IslandGeometry(screenFrame: islandScreen, notchSize: notch)
    let r = g.compactRect()
    try runner.expect(r.size == notch, "compact size should match notch")
    try runner.expect(r.midX == islandScreen.midX, "should centre horizontally")
    try runner.expect(r.maxY == islandScreen.maxY, "top edge flush with screen top")
}

runner.test("island-geo: fallback pill for notchless displays") {
    let fallback = CGSize(width: 180, height: 28)
    let g = IslandGeometry(
        screenFrame: islandScreen,
        notchSize: nil,
        compactFallbackSize: fallback
    )
    let r = g.compactRect()
    try runner.expect(r.size == fallback, "notchless should use fallback size")
}

runner.test("island-geo: expanded centres horizontally") {
    let g = IslandGeometry(screenFrame: islandScreen)
    let target = CGSize(width: 380, height: 90)
    let r = g.expandedRect(size: target)
    try runner.expect(r.size == target, "expanded size mismatch")
    try runner.expect(r.midX == islandScreen.midX, "not centred")
}

runner.test("island-geo: interpolated rect clamps progress") {
    let g = IslandGeometry(screenFrame: islandScreen)
    let target = CGSize(width: 400, height: 120)
    let expanded = g.expandedRect(size: target)
    let tooHigh = g.interpolatedRect(to: target, progress: 5.0)
    try runner.expect(tooHigh.size == expanded.size, "progress > 1 should clamp to expanded")
    let tooLow = g.interpolatedRect(to: target, progress: -1.0)
    let compact = g.compactRect()
    try runner.expect(abs(tooLow.size.width - compact.width) < 0.01, "progress < 0 should clamp to compact")
}

runner.test("island-interaction-geo: idle state stays flush with physical notch") {
    let g = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: islandScreen,
        notchSize: CGSize(width: 210, height: 32),
        hasPhysicalNotch: true,
        hasCompactPresence: false,
        isExpanded: false
    ))
    try runner.expect(g.closedSurfaceSize.width == 210, "idle width should match notch")
    try runner.expect(g.closedSurfaceSize.height == 32, "idle height should match notch")
    try runner.expect(g.panelSize.width == 238, "idle panel should stay narrow")
    try runner.expect(g.surfaceRectInPanel.midX == g.panelSize.width / 2, "surface should centre in panel")
}

runner.test("island-interaction-geo: compact modules expand symmetrically") {
    let g = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: islandScreen,
        notchSize: CGSize(width: 210, height: 32),
        hasPhysicalNotch: true,
        hasCompactPresence: true,
        isExpanded: false,
        activeCount: 3
    ))
    try runner.expect(g.closedExpansionWidth == 132, "expected count-adjusted expansion")
    try runner.expect(g.closedSurfaceSize.width == 342, "closed width should include both side modules")
    try runner.expect(g.closedSurfaceSize.height == 32, "active compact should hug notch height")
    try runner.expect(g.panelSize.width == 370, "compact panel should not reserve expanded width")
}

runner.test("island-interaction-geo: expanded size grows with rows and caps") {
    let small = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: islandScreen,
        notchSize: CGSize(width: 210, height: 32),
        hasPhysicalNotch: true,
        hasCompactPresence: true,
        isExpanded: true,
        activeCount: 1
    ))
    let large = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: islandScreen,
        notchSize: CGSize(width: 210, height: 32),
        hasPhysicalNotch: true,
        hasCompactPresence: true,
        isExpanded: true,
        activeCount: 8
    ))
    try runner.expect(small.expandedSurfaceSize.height == 190, "one row should fit cockpit + health strip")
    try runner.expect(large.expandedSurfaceSize.height == 420, "many rows should cap expanded height")
    try runner.expect(large.surfaceRectInPanel.maxY == large.panelSize.height, "expanded surface should pin to panel top")
}

runner.test("island-interaction-geo: diagnostics includes screen notch panel surface") {
    let g = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: islandScreen,
        notchSize: CGSize(width: 210, height: 32),
        hasPhysicalNotch: true,
        hasCompactPresence: true,
        isExpanded: true,
        activeCount: 2
    ))
    let d = g.diagnostics(screenName: "Built-in")
    try runner.expect(d.contains("screen=Built-in"), "missing screen")
    try runner.expect(d.contains("notch="), "missing notch")
    try runner.expect(d.contains("panel="), "missing panel")
    try runner.expect(d.contains("surface="), "missing surface")
}

// V10 island polish: ``IslandWindowController`` builds the geometry
// off the resolved ``HardwareNotchMode`` — ``.automatic`` /
// ``.forceNotched`` keep the physical notch surface, while
// ``.forceFlat`` synthesizes a 224×28 floating bar even on a real
// MBP. Lock the geometry shape directly so the controller's branch
// stays under test even though ``IslandHostingView`` itself is
// internal to the menu-bar app target.
runner.test("island-interaction-geo: forceFlat synthesises floating bar") {
    let flat = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: islandScreen,
        notchSize: CGSize(width: 224, height: 28),
        hasPhysicalNotch: false,
        hasCompactPresence: false,
        isExpanded: false,
        activeCount: 0
    ))
    let notched = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: islandScreen,
        notchSize: CGSize(width: 210, height: 32),
        hasPhysicalNotch: true,
        hasCompactPresence: false,
        isExpanded: false,
        activeCount: 0
    ))
    // Floating-bar surface should be wider/shorter than the
    // physical notch — it is the no-notch fallback the controller
    // emits when the user pinned ``.forceFlat``.
    try runner.expect(
        flat.closedSurfaceSize.width >= notched.closedSurfaceSize.width,
        "forceFlat surface should be at least as wide as notched"
    )
    try runner.expect(
        flat.closedSurfaceSize.height != notched.closedSurfaceSize.height,
        "forceFlat surface should not pretend to match physical notch height"
    )
}

// --- V10 island polish: IslandAnimationTuning -------------------------------
//
// These tests lock the timings the menu-bar layer uses for the
// expand/collapse animation. They live here so a refactor of
// ``IslandWindowController`` can't quietly retune the asymmetric
// open/close springs without surfacing the change in CI.

runner.test("island-tuning: defaults match boring.notch / MioIsland conventions") {
    let t = IslandAnimationTuning.default
    try runner.expect(
        abs(t.hoverOpenBaseDelay - 0.20) < 0.001,
        "hover-open base should be 200 ms (Dynamic Island feel)"
    )
    try runner.expect(
        abs(t.hoverCloseDelay - 0.14) < 0.001,
        "hover-close should be 140 ms"
    )
    try runner.expect(
        abs(t.panelOpenDuration - 0.42) < 0.001,
        "open duration should be 0.42s (boring.notch spring response)"
    )
    try runner.expect(
        abs(t.panelCloseDuration - 0.30) < 0.001,
        "close duration should match SwiftUI .smooth(0.3) so the AppKit panel doesn't lag the surface"
    )
}

runner.test("island-tuning: hoverSpeed=1.0 returns base delay") {
    let t = IslandAnimationTuning.default
    let d = t.resolvedHoverOpenDelay(hoverSpeed: 1.0)
    try runner.expect(
        abs(d - t.hoverOpenBaseDelay) < 0.001,
        "1.0 should map to base delay, got \(d)"
    )
}

runner.test("island-tuning: hoverSpeed=2.0 halves the delay") {
    let t = IslandAnimationTuning.default
    let d = t.resolvedHoverOpenDelay(hoverSpeed: 2.0)
    try runner.expect(
        abs(d - t.hoverOpenBaseDelay / 2.0) < 0.001,
        "2.0 should halve the delay, got \(d)"
    )
}

runner.test("island-tuning: hoverSpeed=0 maps to instant (MioIsland parity)") {
    let t = IslandAnimationTuning.default
    let d = t.resolvedHoverOpenDelay(hoverSpeed: 0)
    try runner.expect(d == 0, "0 should return 0, got \(d)")
}

runner.test("island-tuning: hoverSpeed clamps protect against typoed values") {
    let t = IslandAnimationTuning.default
    // Way below clamp — should saturate at hoverSpeedMin (0.25)
    // and produce the longest delay we'll ever emit.
    let slow = t.resolvedHoverOpenDelay(hoverSpeed: 0.001)
    let slowExpected = t.hoverOpenBaseDelay / t.hoverSpeedMin
    try runner.expect(
        abs(slow - slowExpected) < 0.001,
        "tiny positive speed should clamp at min: got \(slow) expected \(slowExpected)"
    )
    // Way above clamp — should saturate at hoverSpeedMax (4.0).
    let fast = t.resolvedHoverOpenDelay(hoverSpeed: 1000.0)
    let fastExpected = t.hoverOpenBaseDelay / t.hoverSpeedMax
    try runner.expect(
        abs(fast - fastExpected) < 0.001,
        "huge speed should clamp at max: got \(fast) expected \(fastExpected)"
    )
}

runner.test("island-tuning: panelFrameDuration switches on direction") {
    let t = IslandAnimationTuning.default
    try runner.expect(
        t.panelFrameDuration(forceExpanded: true, animated: true) == t.panelOpenDuration,
        "open path should pick panelOpenDuration"
    )
    try runner.expect(
        t.panelFrameDuration(forceExpanded: false, animated: true) == t.panelCloseDuration,
        "close path should pick panelCloseDuration"
    )
}

runner.test("island-tuning: panelFrameDuration is 0 when animated=false") {
    let t = IslandAnimationTuning.default
    try runner.expect(
        t.panelFrameDuration(forceExpanded: true, animated: false) == 0,
        "animated=false should produce instant snap, even on open"
    )
    try runner.expect(
        t.panelFrameDuration(forceExpanded: false, animated: false) == 0,
        "animated=false should produce instant snap, even on close"
    )
}

runner.test("island-tuning: hoverSpeed JSON round-trips through customization") {
    let json = #"""
    {
      "spec_version": 1,
      "theme": "system",
      "font_scale": 1.0,
      "buddy_style": "pixel",
      "show_buddy": true,
      "hardware_notch_mode": "automatic",
      "screen_geometries": [],
      "hover_speed": 2.5
    }
    """#.data(using: .utf8)!
    let custom = try JSONDecoder().decode(TopSurfaceCustomization.self, from: json)
    try runner.expect(
        abs(custom.hoverSpeed - 2.5) < 0.001,
        "hover_speed should decode to 2.5, got \(custom.hoverSpeed)"
    )
    let t = IslandAnimationTuning.default
    let d = t.resolvedHoverOpenDelay(hoverSpeed: custom.hoverSpeed)
    let expected = t.hoverOpenBaseDelay / 2.5
    try runner.expect(
        abs(d - expected) < 0.001,
        "controller should land at \(expected) for speed=2.5, got \(d)"
    )
}

// --- Phase 3a: IslandStateMachine (L2-#7) -----------------------------------

runner.test("island-sm: present from empty is slideIn") {
    var sm = IslandStateMachine()
    let e = sm.apply(.present(
        kind: .notificationCard, sessionId: "s1", activityId: nil,
        detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
    ))
    try runner.expect(e.transition == .slideIn, "expected slideIn")
    try runner.expect(sm.surface.kind == .notificationCard, "surface not updated")
}

runner.test("island-sm: lower priority cannot replace higher") {
    // Use degradation >= 4 to skip SneakPeek so we test the pure priority gate.
    var sm = IslandStateMachine(degradationLevel: 4)
    _ = sm.apply(.present(
        kind: .notificationCard, sessionId: "s1", activityId: nil,
        detail: nil, surfaceId: nil, priority: .p1, tsMs: 100
    ))
    let e = sm.apply(.present(
        kind: .liveActivity, sessionId: nil, activityId: "act",
        detail: nil, surfaceId: nil, priority: .p3, tsMs: 200
    ))
    try runner.expect(e.transition == .none, "should reject lower priority")
    try runner.expect(sm.priority == .p1, "priority should not drop")
}

runner.test("island-sm: dismiss with matching id slides out") {
    var sm = IslandStateMachine()
    _ = sm.apply(.present(
        kind: .notificationCard, sessionId: "s1", activityId: nil,
        detail: nil, surfaceId: nil, priority: .p2, tsMs: 100
    ))
    let e = sm.apply(.dismiss(id: "s1", tsMs: 200))
    try runner.expect(e.transition == .slideOut, "expected slideOut")
    try runner.expect(sm.surface.kind == .empty, "should land on empty")
}

runner.test("island-sm: auto dismiss after tick timeout") {
    var sm = IslandStateMachine(autoDismissMs: 1_000)
    _ = sm.apply(.present(
        kind: .notificationCard, sessionId: "s1", activityId: nil,
        detail: nil, surfaceId: nil, priority: .p2, tsMs: 0
    ))
    let early = sm.apply(.tick(tsMs: 500))
    try runner.expect(early.transition == .none, "early tick should not dismiss")
    let late = sm.apply(.tick(tsMs: 2_000))
    try runner.expect(late.transition == .slideOut, "late tick should dismiss")
}

runner.test("island-sm: high priority surface is pinned") {
    // Use degradation >= 4 to skip SneakPeek so we test the pure pinning logic.
    var sm = IslandStateMachine(pinnedPriorityCeiling: .p1, autoDismissMs: 1_000, degradationLevel: 4)
    _ = sm.apply(.present(
        kind: .notificationCard, sessionId: "s1", activityId: nil,
        detail: nil, surfaceId: nil, priority: .p1, tsMs: 100
    ))
    let e = sm.apply(.tick(tsMs: 10_000))
    try runner.expect(e.transition == .none, "pinned surface should not dismiss")
    try runner.expect(sm.surface.kind == .notificationCard, "pinned kind should remain")
}

// --- Phase 3a: IslandHoverRouter (L3-A10) -----------------------------------

runner.test("island-hover: enter on compact promotes to sessionList") {
    let r = IslandHoverRouter()
    try runner.expect(
        r.decide(event: .enter(tsMs: 0), current: .compact) == .promote(to: .sessionList),
        "hover enter → sessionList"
    )
}

runner.test("island-hover: leave on sessionList returns to compact") {
    let r = IslandHoverRouter()
    try runner.expect(
        r.decide(event: .leave(tsMs: 0), current: .sessionList) == .promote(to: .compact),
        "hover leave → compact"
    )
}

runner.test("island-hover: tap on compact promotes, on sessionList dismisses") {
    let r = IslandHoverRouter()
    try runner.expect(
        r.decide(event: .tap(tsMs: 0), current: .compact) == .promote(to: .sessionList),
        "tap compact → sessionList"
    )
    try runner.expect(
        r.decide(event: .tap(tsMs: 0), current: .sessionList) == .dismiss,
        "tap sessionList → dismiss"
    )
}

// --- Phase 3b: IslandModuleRegistry (V10 I5) --------------------------------

final class SmokeStubModule: IslandModule {
    let id: String
    let claimPriority: Int
    let supportedKinds: Set<IslandSurfaceKind>
    var shouldHandle: Bool
    var handleCount: Int = 0
    var descriptor: IslandModuleRenderDescriptor?

    init(
        id: String,
        claimPriority: Int = 0,
        supportedKinds: Set<IslandSurfaceKind> = [.notificationCard],
        shouldHandle: Bool = true,
        descriptor: IslandModuleRenderDescriptor? = nil
    ) {
        self.id = id
        self.claimPriority = claimPriority
        self.supportedKinds = supportedKinds
        self.shouldHandle = shouldHandle
        self.descriptor = descriptor
    }

    func handle(_ action: InteractionAction) -> Bool {
        handleCount += 1
        return shouldHandle
    }

    func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        descriptor
    }
}

let smokeDummyAction = InteractionAction(source: .island, target: .session, kind: .taskOpenDetail)

runner.test("registry: same id replaces existing module") {
    var r = IslandModuleRegistry()
    r.register(SmokeStubModule(id: "session", claimPriority: 1))
    r.register(SmokeStubModule(id: "session", claimPriority: 10))
    try runner.expect(r.count == 1, "expected 1 module, got \(r.count)")
    try runner.expect(r.modules.first?.claimPriority == 10, "replacement priority mismatch")
}

runner.test("registry: module defaults expose plan metadata") {
    let module = SmokeStubModule(id: "session")
    try runner.expect(module.displayName == "session", "displayName default")
    try runner.expect(module.defaultSide == .center, "defaultSide")
    try runner.expect(module.defaultOrder == 0, "defaultOrder")
    try runner.expect(module.isVisible, "isVisible")
    try runner.expect(module.preferredWidth == nil, "preferredWidth")
    try runner.expect(
        module.render(state: IslandSurfaceState(kind: .sessionList)) == nil,
        "default render descriptor"
    )
}

runner.test("registry: module(for:) picks highest priority claim") {
    var r = IslandModuleRegistry()
    r.register(SmokeStubModule(id: "low", claimPriority: 1))
    r.register(SmokeStubModule(id: "high", claimPriority: 10))
    let resolved = r.module(for: IslandSurfaceState(kind: .notificationCard))
    try runner.expect(resolved?.id == "high", "expected high, got \(resolved?.id ?? "nil")")
}

runner.test("registry: renderDescriptor returns first non-empty descriptor") {
    var r = IslandModuleRegistry()
    r.register(SmokeStubModule(id: "empty", claimPriority: 10, descriptor: nil))
    r.register(
        SmokeStubModule(
            id: "rendering",
            claimPriority: 5,
            descriptor: IslandModuleRenderDescriptor(
                title: "NOTICE",
                subtitle: "Build done",
                badge: "now",
                systemImageName: "sparkle"
            )
        )
    )
    let descriptor = r.renderDescriptor(
        for: IslandSurfaceState(kind: .notificationCard)
    )
    try runner.expect(descriptor?.title == "NOTICE", "descriptor title mismatch")
    try runner.expect(descriptor?.subtitle == "Build done", "descriptor subtitle mismatch")
}

runner.test("registry: default modules render compact island states") {
    let registry = IslandModuleRegistry.deskmateDefaultModules()
    let reminder = registry.renderDescriptor(
        for: IslandSurfaceState(
            kind: .notificationCard,
            activityId: "demo-reminder",
            detail: "Reminder due now"
        )
    )
    try runner.expect(reminder?.title == "REMIND", "reminder title mismatch")
    try runner.expect(reminder?.systemImageName == "bell.fill", "reminder icon mismatch")

    let build = registry.renderDescriptor(
        for: IslandSurfaceState(
            kind: .liveActivity,
            activityId: "build-demo",
            detail: "Running tests"
        )
    )
    try runner.expect(build?.title == "BUILD", "build title mismatch")
    try runner.expect(build?.subtitle == "Running tests", "build subtitle mismatch")

    let sessions = registry.renderDescriptor(for: IslandSurfaceState(kind: .sessionList))
    try runner.expect(sessions?.title == "SESS", "session module title mismatch")

    let idle = registry.renderDescriptor(for: IslandSurfaceState(kind: .compact))
    try runner.expect(idle?.title == "DM", "idle module title mismatch")
}

runner.test("registry: registered module claims prefix and renders templates") {
    let module = RegisteredIslandModule(spec: IslandModuleSpec(
        id: "kiro.spec",
        priority: 80,
        kind: "live_activity",
        activityPrefix: "kiro-spec-",
        title: "KIRO",
        subtitle: "{detail}",
        image: "k.circle"
    ))
    let match = IslandSurfaceState(
        kind: .liveActivity,
        activityId: "kiro-spec-plan",
        detail: "Planning"
    )
    let miss = IslandSurfaceState(
        kind: .liveActivity,
        activityId: "build-demo",
        detail: "Build"
    )
    try runner.expect(module.claims(state: match), "module should claim matching prefix")
    try runner.expect(!module.claims(state: miss), "module should reject non-matching prefix")
    let descriptor = module.render(state: match)
    try runner.expect(descriptor?.title == "KIRO", "registered title mismatch")
    try runner.expect(descriptor?.subtitle == "Planning", "registered subtitle mismatch")
    try runner.expect(descriptor?.systemImageName == "k.circle", "registered icon mismatch")
}

runner.test("registry: registered module can override default live activity") {
    var registry = IslandModuleRegistry.deskmateDefaultModules()
    registry.register(RegisteredIslandModule(spec: IslandModuleSpec(
        id: "kiro.spec",
        priority: 90,
        kind: "live_activity",
        activityPrefix: "kiro-spec-",
        title: "KIRO",
        subtitle: "{activity}"
    )))
    let descriptor = registry.renderDescriptor(for: IslandSurfaceState(
        kind: .liveActivity,
        activityId: "kiro-spec-plan",
        detail: "Planning"
    ))
    try runner.expect(descriptor?.title == "KIRO", "registered module should win")
    try runner.expect(descriptor?.subtitle == "kiro-spec-plan", "activity template mismatch")
}

runner.test("registry: dispatch stops at first handler and higher priority wins") {
    var r = IslandModuleRegistry()
    let high = SmokeStubModule(id: "high", claimPriority: 10, shouldHandle: true)
    let low = SmokeStubModule(id: "low", claimPriority: 1, shouldHandle: true)
    r.register(low)
    r.register(high)
    let handled = r.dispatch(smokeDummyAction)
    try runner.expect(handled == "high", "expected high to handle, got \(handled ?? "nil")")
    try runner.expect(high.handleCount == 1, "high should have been invoked once")
    try runner.expect(low.handleCount == 0, "low should not have been invoked")
}

runner.test("registry: dispatch falls through non-handlers") {
    var r = IslandModuleRegistry()
    let first = SmokeStubModule(id: "first", claimPriority: 10, shouldHandle: false)
    let second = SmokeStubModule(id: "second", claimPriority: 1, shouldHandle: true)
    r.register(first)
    r.register(second)
    let handled = r.dispatch(smokeDummyAction)
    try runner.expect(handled == "second", "expected fall-through to second")
    try runner.expect(first.handleCount == 1, "first should still be asked")
    try runner.expect(second.handleCount == 1, "second should be asked after first declined")
}

runner.test("registry: dispatch on empty registry returns nil") {
    let r = IslandModuleRegistry()
    try runner.expect(r.dispatch(smokeDummyAction) == nil, "empty registry should return nil")
}

// --- Phase 4: SessionRow + SessionListAdapter (L1-D / L2-#8) ----------------

func smokeSessionRow(
    _ id: String,
    title: String = "",
    state: SessionRow.State = .active,
    priority: Priority = .p2,
    updated: Int = 1_000,
    closed: Int? = nil,
    phase: SessionRow.Phase = .running,
    parentSessionId: String? = nil,
    subagentKind: String? = nil
) -> SessionRow {
    SessionRow(
        sessionId: id,
        title: title,
        state: state,
        priority: priority,
        updatedAtMs: updated,
        closedAtMs: closed,
        phase: phase,
        parentSessionId: parentSessionId,
        subagentKind: subagentKind
    )
}

runner.test("session-row: decodes snake_case wire format") {
    let raw = #"""
    {"session_id":"s1","title":"Deploy","state":"active","priority":"P1","updated_at_ms":2000}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.sessionId == "s1", "session_id mismatch")
    try runner.expect(row.state == .active, "state mismatch")
    try runner.expect(row.priority == .p1, "priority mismatch")
    try runner.expect(row.updatedAtMs == 2000, "updated_at_ms mismatch")
}

runner.test("session-row: unknown state falls back to .unknown") {
    let raw = #"""
    {"session_id":"s1","state":"archived"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.state == .unknown, "expected .unknown for archived")
}

runner.test("adapter: active rows come before closed") {
    let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
    let rows = [
        smokeSessionRow("closed", state: .closed, updated: 5_000, closed: 5_000),
        smokeSessionRow("active", state: .active, updated: 1_000),
    ]
    let out = adapter.display(sessions: rows, nowMs: 10_000).map(\.sessionId)
    try runner.expect(out == ["active", "closed"], "ordering wrong: \(out)")
}

runner.test("adapter: higher priority beats newer timestamp") {
    let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
    let rows = [
        smokeSessionRow("p3-new", priority: .p3, updated: 10_000),
        smokeSessionRow("p0-old", priority: .p0, updated: 1_000),
    ]
    let out = adapter.display(sessions: rows, nowMs: 20_000).map(\.sessionId)
    try runner.expect(out == ["p0-old", "p3-new"], "expected p0 to win: \(out)")
}

runner.test("adapter: stale closed rows are hidden past TTL") {
    let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: 1_000)
    let rows = [
        smokeSessionRow("fresh", state: .closed, updated: 9_500, closed: 9_500),
        smokeSessionRow("stale", state: .closed, updated: 500, closed: 500),
    ]
    let out = adapter.display(sessions: rows, nowMs: 10_000).map(\.sessionId)
    try runner.expect(out == ["fresh"], "stale should be hidden: \(out)")
}

runner.test("adapter: max rows caps the result") {
    let adapter = SessionListAdapter(maxRows: 2, showClosedAfterMs: nil)
    let rows = (1...5).map { smokeSessionRow("s\($0)", updated: 100 * $0) }
    let out = adapter.display(sessions: rows, nowMs: 10_000)
    try runner.expect(out.count == 2, "expected 2 rows, got \(out.count)")
}

// --- L2-#4: actionable-first ordering + subagent fold ----------------------

runner.test("session-row: decodes new L2-#4 fields with snake_case") {
    let raw = #"""
    {"session_id":"s1","phase":"waiting_for_approval",
     "parent_session_id":"parent","subagent_kind":"tool_call"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.phase == .waitingForApproval, "phase mismatch")
    try runner.expect(row.parentSessionId == "parent", "parent mismatch")
    try runner.expect(row.subagentKind == "tool_call", "kind mismatch")
    try runner.expect(row.isSubagent, "isSubagent should be true")
}

runner.test("session-row: decodes hook jump-back fields") {
    let raw = #"""
    {"session_id":"s1","cwd":"/tmp/project","jump_url":"codex://session/s1",
     "future_field":"kept"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.cwd == "/tmp/project", "cwd mismatch")
    try runner.expect(row.jumpUrl == "codex://session/s1", "jump_url mismatch")
}

runner.test("session-row: decodes runtime source fields") {
    let raw = #"""
    {"session_id":"s1","source":"claude_code","kind":"cli_agent","process_id":4242}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.source == "claude_code", "source mismatch")
    try runner.expect(row.kind == "cli_agent", "kind mismatch")
    try runner.expect(row.processId == 4242, "process_id mismatch")
    try runner.expect(row.sourceLabel == "Claude", "source label mismatch")
    try runner.expect(row.canAttemptJump, "runtime rows should allow jump attempts")
}

// V10 polish: extended runtime lineup. Keep the label switch in
// ``SessionRow.sourceLabel`` aligned with
// ``deskmate_agent.agent_runtime.AgentRuntimeSource`` — Python emits
// the snake_case values, Swift maps them to display labels for the
// island session list.
runner.test("session-row: pretty labels for extended runtime sources") {
    let pairs: [(String, String)] = [
        ("aider", "Aider"),
        ("gemini", "Gemini"),
        ("kimi", "Kimi"),
        ("qwen", "Qwen"),
        ("factory_droid", "Factory Droid"),
        ("codebuddy", "CodeBuddy"),
        ("qoder", "Qoder"),
        ("zed", "Zed"),
        ("trae", "Trae"),
        ("sublime", "Sublime"),
        ("fleet", "Fleet"),
        ("nova", "Nova"),
        ("neovim", "Neovim"),
        ("github_desktop", "GitHub Desktop"),
        ("warp", "Warp"),
    ]
    for (raw, expected) in pairs {
        let row = SessionRow(sessionId: "s-\(raw)", source: raw)
        try runner.expect(
            row.sourceLabel == expected,
            "label for \(raw) wrong: got \(row.sourceLabel ?? "nil")"
        )
    }
}

// V10 polish: an unknown source falls through to the default
// PrettyPrint branch — we lock the contract so adding a Python
// source with no Swift case still produces a usable label.
runner.test("session-row: unknown source falls back to PrettyPrint") {
    let row = SessionRow(sessionId: "s1", source: "future_agent_v3")
    try runner.expect(
        row.sourceLabel == "Future Agent V3",
        "fallback label wrong: got \(row.sourceLabel ?? "nil")"
    )
}

runner.test("session-row: decodes fine-grained agent phases") {
    let raw = #"""
    {"session_id":"s1","phase":"editing"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.phase == .editing, "phase mismatch")
    try runner.expect(row.phaseLabel == "editing", "phase label mismatch")
}

runner.test("session-row: decodes extras and derives activity line") {
    let raw = #"""
    {"session_id":"s1","source":"codex","cwd":"/tmp/work","summary":"fallback",
     "extras":{"tool_name":"Bash","command":"pytest tests/test_app.py","ignored":42}}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.extras["tool_name"] == "Bash", "tool extra mismatch")
    try runner.expect(row.command == "pytest tests/test_app.py", "command mismatch")
    try runner.expect(row.extras["ignored"] == nil, "non-string extra should be ignored")
    try runner.expect(
        row.activityLine == "Codex · work · cmd: pytest tests/test_app.py",
        "activity line wrong: \(row.activityLine)"
    )
}

runner.test("session-row: decodes structured tool extras") {
    let raw = #"""
    {"session_id":"tools","source":"deskmate","phase":"completed",
     "summary":"Reminder scheduled for 1 minute: stretch.",
     "extras":{"tool_action":"deskmate_schedule_reminder","tool_target":"stretch",
     "tool_outcome":"Reminder scheduled for 1 minute: stretch.",
     "tool_needs_user":"false",
     "tool_summary":"action=deskmate_schedule_reminder; status=completed; target=stretch; outcome=Reminder scheduled for 1 minute: stretch.; needs_user=false",
     "tool_task_id":"deskmate-tool-task-default-1",
     "tool_task_status":"completed",
     "tool_task_summary":"action=deskmate_schedule_reminder; status=completed; target=stretch"}}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(
        row.toolAction == "deskmate_schedule_reminder",
        "tool action mismatch"
    )
    try runner.expect(row.toolTarget == "stretch", "tool target mismatch")
    try runner.expect(row.toolNeedsUser == false, "tool needs_user mismatch")
    try runner.expect(
        row.toolTaskId == "deskmate-tool-task-default-1",
        "tool task id mismatch"
    )
    try runner.expect(row.toolTaskStatus == "completed", "tool task status mismatch")
    try runner.expect(
        row.activityLine == "Deskmate · tool: deskmate_schedule_reminder -> stretch",
        "structured activity line wrong: \(row.activityLine)"
    )
}

runner.test("session-row: decodes approval resolution extras") {
    let raw = #"""
    {"session_id":"s1","extras":{"last_approval_id":"ap-1","last_approval_decision":"deny","last_approval_prompt":"Allow shell?","last_approval_risk_level":"high","last_approval_preview":"cmd: sudo rm -rf build/cache","last_approval_resolved_at_ms":"5000"}}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.lastApprovalId == "ap-1", "approval id mismatch")
    try runner.expect(row.lastApprovalDecision == "deny", "approval decision mismatch")
    try runner.expect(row.lastApprovalPrompt == "Allow shell?", "approval prompt mismatch")
    try runner.expect(row.lastApprovalRiskLevel == "high", "approval risk mismatch")
    try runner.expect(row.lastApprovalPreview == "cmd: sudo rm -rf build/cache", "approval preview mismatch")
    try runner.expect(row.lastApprovalResolvedAtMs == 5000, "approval resolved timestamp mismatch")
    try runner.expect(
        row.recentOutcomeLine == "Denied high approval: cmd: sudo rm -rf build/cache",
        "approval outcome line mismatch: \(String(describing: row.recentOutcomeLine))"
    )
}

runner.test("session-row: agent health summary rolls up runtime visibility") {
    let rows = [
        SessionRow(
            sessionId: "hook",
            phase: .waitingForApproval,
            source: "codex",
            kind: "hook_session",
            extras: ["phase_source": "hook"]
        ),
        SessionRow(
            sessionId: "cli",
            source: "claude_code",
            kind: "cli_agent",
            extras: ["phase_source": "unobserved"]
        ),
        SessionRow(
            sessionId: "ide",
            source: "cursor",
            kind: "gui_ide"
        ),
        SessionRow(
            sessionId: "closed",
            state: .closed,
            source: "windsurf",
            kind: "gui_ide"
        )
    ]
    let summary = AgentHealthSummary(sessions: rows)
    try runner.expect(summary.total == 4, "total wrong")
    try runner.expect(summary.active == 3, "active wrong")
    try runner.expect(summary.hookSessions == 1, "hook count wrong")
    try runner.expect(summary.cliAgents == 1, "cli count wrong")
    try runner.expect(summary.guiIDEs == 1, "ide count wrong")
    try runner.expect(summary.unobserved == 1, "unobserved count wrong")
    try runner.expect(summary.awaitingAction == 1, "action count wrong")
    try runner.expect(summary.statusLine.contains("Active 3"), "status line wrong: \(summary.statusLine)")
    try runner.expect(summary.kindLine.contains("Hook 1"), "kind line wrong: \(summary.kindLine)")
    try runner.expect(summary.sourceLine.contains("Codex 1"), "source line wrong: \(summary.sourceLine)")
    try runner.expect(summary.expandedBadgeText == "1 action", "badge wrong: \(summary.expandedBadgeText)")
}

runner.test("session-row: derives actionable display metadata") {
    let row = SessionRow(
        sessionId: "s1",
        title: "Refactor",
        summary: "Waiting on shell approval",
        phase: .waitingForApproval,
        jumpUrl: "codex://threads/s1",
        source: "codex"
    )
    try runner.expect(row.needsUserAction, "approval should need user action")
    try runner.expect(row.hasJumpTarget, "jump_url should be a jump target")
    try runner.expect(row.canAttemptJump, "jump target should allow jump attempts")
    try runner.expect(row.displayTitle == "Refactor", "title fallback wrong")
    try runner.expect(
        row.detailLine == "Codex · Waiting on shell approval",
        "detail line wrong: \(row.detailLine)"
    )
}

runner.test("session-row: can attempt jump from target or runtime metadata") {
    try runner.expect(!SessionRow(sessionId: "plain").canAttemptJump, "plain row should not jump")
    try runner.expect(SessionRow(sessionId: "cwd", cwd: "/tmp/work").canAttemptJump, "cwd row should jump")
    try runner.expect(
        SessionRow(sessionId: "source", source: "cursor").canAttemptJump,
        "source row should jump"
    )
    try runner.expect(
        SessionRow(sessionId: "kind", kind: "cli_agent").canAttemptJump,
        "kind row should jump"
    )
    try runner.expect(
        SessionRow(sessionId: "pid", processId: 42).canAttemptJump,
        "pid row should jump"
    )
}

runner.test("session-row: missing L2-#4 fields default to running top-level") {
    let raw = #"""
    {"session_id":"legacy","state":"active"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(SessionRow.self, from: raw)
    try runner.expect(row.phase == .running, "legacy phase should default to running")
    try runner.expect(row.parentSessionId == nil, "legacy parent should be nil")
    try runner.expect(!row.isSubagent, "legacy row should not look like a subagent")
}

runner.test("adapter: phase beats recency for actionable-first ordering") {
    let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
    let rows = [
        smokeSessionRow("running-recent", updated: 10_000),
        smokeSessionRow(
            "approval-stale", updated: 1_000,
            phase: .waitingForApproval
        ),
        smokeSessionRow(
            "answer", updated: 2_000, phase: .waitingForAnswer
        ),
        smokeSessionRow(
            "completed-fresh", updated: 20_000, phase: .completed
        ),
    ]
    let out = adapter.display(sessions: rows, nowMs: 30_000).map { $0.sessionId }
    try runner.expect(
        out == ["approval-stale", "answer", "running-recent", "completed-fresh"],
        "actionable-first wrong: \(out)"
    )
}

runner.test("adapter: fine-grained phase ordering is actionable-first") {
    let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
    let rows = [
        smokeSessionRow("thinking", phase: .thinking),
        smokeSessionRow("editing", phase: .editing),
        smokeSessionRow("tool", phase: .runningTool),
        smokeSessionRow("failed", phase: .failed),
    ]
    let out = adapter.display(sessions: rows, nowMs: 0).map { $0.sessionId }
    try runner.expect(
        out == ["failed", "tool", "editing", "thinking"],
        "fine phase order wrong: \(out)"
    )
}

runner.test("adapter: subagents are hidden from primary list") {
    let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
    let rows = [
        smokeSessionRow("parent"),
        smokeSessionRow(
            "child",
            parentSessionId: "parent",
            subagentKind: "tool_call"
        ),
    ]
    let out = adapter.display(sessions: rows, nowMs: 0).map { $0.sessionId }
    try runner.expect(out == ["parent"], "subagent leaked: \(out)")
}

runner.test("adapter: displayWithFold attaches counts and summaries") {
    let adapter = SessionListAdapter(
        maxRows: 10, showClosedAfterMs: nil, maxSummariesPerParent: 2
    )
    let rows = [
        smokeSessionRow("parent", title: "Refactor"),
        smokeSessionRow(
            "child-a", title: "grep loop", parentSessionId: "parent",
            subagentKind: "tool_call"
        ),
        smokeSessionRow(
            "child-b", title: "", parentSessionId: "parent",
            subagentKind: "worktree"
        ),
        smokeSessionRow(
            "child-c", title: "", parentSessionId: "parent",
            subagentKind: "worktree"
        ),
    ]
    let entries = adapter.displayWithFold(sessions: rows, nowMs: 0)
    try runner.expect(entries.count == 1, "expected 1 entry, got \(entries.count)")
    let entry = entries[0]
    try runner.expect(
        entry.row.sessionId == "parent",
        "fold root should be parent"
    )
    try runner.expect(
        entry.subagentCount == 3,
        "expected 3 children, got \(entry.subagentCount)"
    )
    // Cap honoured: only 2 summaries even though 3 kids exist.
    try runner.expect(
        entry.subagentSummaries.count == 2,
        "summary cap broken: \(entry.subagentSummaries)"
    )
    // Each entry is non-empty (title-or-kind-or-id fallback chain).
    try runner.expect(
        entry.subagentSummaries.allSatisfy { !$0.isEmpty },
        "empty summary leaked: \(entry.subagentSummaries)"
    )
}

runner.test("adapter: displayWithFold preserves actionable-first ordering of parents") {
    let adapter = SessionListAdapter(maxRows: 10, showClosedAfterMs: nil)
    let rows = [
        smokeSessionRow("running"),
        smokeSessionRow(
            "approval", phase: .waitingForApproval
        ),
        smokeSessionRow(
            "child", parentSessionId: "running"
        ),
    ]
    let ids = adapter.displayWithFold(sessions: rows, nowMs: 0)
        .map { $0.row.sessionId }
    try runner.expect(
        ids == ["approval", "running"],
        "fold should still be actionable-first: \(ids)"
    )
}

runner.test("island content projection: approval wins over build and sessions") {
    let content = IslandContentProjection.compute(
        islandState: IslandSurfaceState(
            kind: .liveActivity,
            activityId: "build-demo",
            detail: "Running tests"
        ),
        sessions: [smokeSessionRow("s1", phase: .running)],
        approvals: [ApprovalRow(approvalId: "a1", sessionId: "s1")]
    )
    guard case .approval(let session, let approval) = content else {
        throw SmokeError.expectation("expected approval content, got \(content)")
    }
    try runner.expect(session?.sessionId == "s1", "approval session mismatch")
    try runner.expect(approval.approvalId == "a1", "approval row mismatch")
}

runner.test("island content projection: build wins over notification") {
    let content = IslandContentProjection.compute(
        islandState: IslandSurfaceState(
            kind: .liveActivity,
            activityId: "build-demo",
            detail: "✅ Demo · done",
            progress: 1.0
        ),
        sessions: [smokeSessionRow("s1")],
        approvals: []
    )
    guard case .build(let activityId, _, let progress, let isDone, let isFailed) = content else {
        throw SmokeError.expectation("expected build content, got \(content)")
    }
    try runner.expect(activityId == "build-demo", "build id mismatch")
    try runner.expect(progress == 1.0, "build progress mismatch")
    try runner.expect(isDone && !isFailed, "build completion flags mismatch")
}

runner.test("island content projection: multi-session focuses actionable") {
    let content = IslandContentProjection.compute(
        islandState: nil,
        sessions: [
            smokeSessionRow("running", phase: .running),
            smokeSessionRow("answer", phase: .waitingForAnswer),
        ],
        approvals: []
    )
    guard case .multiSession(let sessions, let focus) = content else {
        throw SmokeError.expectation("expected multiSession content, got \(content)")
    }
    try runner.expect(sessions.map(\.sessionId) == ["answer", "running"], "session order changed")
    try runner.expect(focus?.sessionId == "answer", "focus should choose actionable")
}

runner.test("island content projection: recent completed closed session stays visible") {
    let content = IslandContentProjection.compute(
        islandState: nil,
        sessions: [
            smokeSessionRow(
                "done",
                state: .closed,
                updated: 9_500,
                closed: 9_500,
                phase: .completed
            ),
        ],
        approvals: [],
        nowMs: 10_000,
        showClosedAfterMs: 1_000
    )
    guard case .session(let session) = content else {
        throw SmokeError.expectation("expected completed session content, got \(content)")
    }
    try runner.expect(session.sessionId == "done", "completed session mismatch")
    try runner.expect(session.phase == .completed, "completed phase mismatch")
}

runner.test("island content projection: thinking session beats active task") {
    let content = IslandContentProjection.compute(
        islandState: nil,
        sessions: [smokeSessionRow("s1", phase: .thinking)],
        approvals: [],
        tasks: [
            TaskRow(
                taskId: "task-1",
                title: "Polish island task lane",
                status: .inProgress
            )
        ]
    )
    guard case .session(let session) = content else {
        throw SmokeError.expectation("expected session content, got \(content)")
    }
    try runner.expect(session.sessionId == "s1", "thinking session mismatch")
}

runner.test("island content projection: active task fills idle gap") {
    let content = IslandContentProjection.compute(
        islandState: nil,
        sessions: [],
        approvals: [],
        tasks: [TaskRow(taskId: "task-1", status: .inProgress)]
    )
    guard case .task(let task) = content else {
        throw SmokeError.expectation("expected task content, got \(content)")
    }
    try runner.expect(task.taskId == "task-1", "task id mismatch")
}

runner.test("island content projection: notification beats active task") {
    let content = IslandContentProjection.compute(
        islandState: IslandSurfaceState(
            kind: .notificationCard,
            activityId: "reminder-1",
            detail: "Stand up"
        ),
        sessions: [],
        approvals: [],
        tasks: [TaskRow(taskId: "task-1", status: .inProgress)]
    )
    guard case .notification(let state) = content else {
        throw SmokeError.expectation("expected notification content, got \(content)")
    }
    try runner.expect(state.activityId == "reminder-1", "notification id mismatch")
}

// --- Phase 5: ReminderRow + ReminderListAdapter (L2-#4) ---------------------

func smokeReminder(
    _ id: String,
    status: ReminderRow.Status = .pending,
    priority: Priority = .p1,
    dueAtMs: Int = 1_000,
    resolvedAtMs: Int? = nil
) -> ReminderRow {
    ReminderRow(
        reminderId: id,
        dueAtMs: dueAtMs,
        status: status,
        priority: priority,
        resolvedAtMs: resolvedAtMs
    )
}

runner.test("reminder-row: decodes snake_case wire format") {
    let raw = #"""
    {"reminder_id":"r1","text":"stretch","due_at_ms":1000,"status":"pending","priority":"P1"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(ReminderRow.self, from: raw)
    try runner.expect(row.reminderId == "r1", "reminder_id mismatch")
    try runner.expect(row.status == .pending, "status mismatch")
    try runner.expect(row.priority == .p1, "priority mismatch")
    try runner.expect(row.dueAtMs == 1000, "due_at_ms mismatch")
}

runner.test("reminder-row: unknown status falls back to .unknown") {
    let raw = #"""
    {"reminder_id":"r1","status":"snoozed"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(ReminderRow.self, from: raw)
    try runner.expect(row.status == .unknown, "should be .unknown")
}

// --- Active durable TaskRow -------------------------------------------------

runner.test("task-row: decodes active task with current step") {
    let raw = #"""
    {"task_id":"task-1","title":"Polish task lane","status":"in_progress",
     "completed_step_count":3,"total_step_count":7,
     "current_step":{"step_id":"step-2","position":2,"content":"Expose task snapshot",
       "status":"in_progress","active_form":"Exposing task snapshot"},
     "steps":[{"step_id":"step-1","content":"Read references","status":"completed"},
       {"step_id":"step-2","content":"Expose task snapshot",
        "status":"in_progress","active_form":"Exposing task snapshot"}]}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(TaskRow.self, from: raw)
    try runner.expect(row.taskId == "task-1", "task_id mismatch")
    try runner.expect(row.status == .inProgress, "status mismatch")
    try runner.expect(row.currentStepLine == "step: Exposing task snapshot",
                      "current step line mismatch")
    try runner.expect(row.steps.first?.status == .completed, "step status mismatch")
    try runner.expect(row.completedStepCount == 3, "completed step count mismatch")
    try runner.expect(row.totalStepCount == 7, "total step count mismatch")
    try runner.expect(row.stepProgressLabel == "3/7 steps", "progress label mismatch")
    try runner.expect(row.stepProgressLine == "progress: 3/7 steps",
                      "progress line mismatch")
}

runner.test("task-row: unknown status falls back and title uses id") {
    let raw = #"""
    {"task_id":"task-2","status":"blocked"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(TaskRow.self, from: raw)
    try runner.expect(row.status == .unknown, "expected unknown")
    try runner.expect(row.displayTitle == "task-2", "title fallback mismatch")
    try runner.expect(row.stepProgressLabel == nil, "empty steps should hide progress")
}

runner.test("reminder-adapter: pending > fired > terminal") {
    let a = ReminderListAdapter(maxRows: 10, hideResolvedAfterMs: nil)
    let rows = [
        smokeReminder("cancelled", status: .cancelled),
        smokeReminder("fired", status: .fired),
        smokeReminder("pending", status: .pending),
    ]
    let ids = a.display(reminders: rows, nowMs: 10_000).map(\.reminderId)
    try runner.expect(ids == ["pending", "fired", "cancelled"], "ordering wrong: \(ids)")
}

runner.test("reminder-adapter: higher priority wins within same status") {
    let a = ReminderListAdapter(maxRows: 10, hideResolvedAfterMs: nil)
    let rows = [
        smokeReminder("p3", priority: .p3),
        smokeReminder("p0", priority: .p0),
    ]
    let ids = a.display(reminders: rows, nowMs: 0).map(\.reminderId)
    try runner.expect(ids == ["p0", "p3"], "priority order wrong: \(ids)")
}

runner.test("reminder-adapter: resolved older than cutoff hidden") {
    let a = ReminderListAdapter(maxRows: 10, hideResolvedAfterMs: 1_000)
    let rows = [
        smokeReminder("fresh", status: .dismissed, resolvedAtMs: 9_500),
        smokeReminder("stale", status: .dismissed, resolvedAtMs: 500),
    ]
    let ids = a.display(reminders: rows, nowMs: 10_000).map(\.reminderId)
    try runner.expect(ids == ["fresh"], "stale should be hidden: \(ids)")
}

// --- Phase 6: ApprovalRow (L1-F) --------------------------------------------

runner.test("approval-row: decodes pending approval wire format") {
    let raw = #"""
    {"approval_id":"ap-1","prompt":"Read clipboard?","status":"pending","decision":"none","priority":"P1","created_at_ms":1000}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(ApprovalRow.self, from: raw)
    try runner.expect(row.approvalId == "ap-1", "approval_id mismatch")
    try runner.expect(row.status == .pending, "status mismatch")
    try runner.expect(row.decision == .none, "decision mismatch")
    try runner.expect(row.priority == .p1, "priority mismatch")
    try runner.expect(row.createdAtMs == 1000, "created_at_ms mismatch")
}

runner.test("approval-row: resolved + allow round-trips") {
    let raw = #"""
    {"approval_id":"ap-2","prompt":"ok?","status":"resolved","decision":"allow","resolved_at_ms":5000}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(ApprovalRow.self, from: raw)
    try runner.expect(row.status == .resolved, "should be resolved")
    try runner.expect(row.decision == .allow, "should be allow")
    try runner.expect(row.resolvedAtMs == 5000, "resolved_at_ms mismatch")
}

runner.test("approval-row: unknown status falls back") {
    let raw = #"""
    {"approval_id":"ap-3","status":"snoozed"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(ApprovalRow.self, from: raw)
    try runner.expect(row.status == .unknown, "should be .unknown")
}

runner.test("approval-row: unknown decision falls back") {
    let raw = #"""
    {"approval_id":"ap-4","decision":"maybe"}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(ApprovalRow.self, from: raw)
    try runner.expect(row.decision == .unknown, "should be .unknown")
}

runner.test("approval-row: decodes extras and derives detail line") {
    let raw = #"""
    {"approval_id":"ap-5","prompt":"Allow command?","extras":{"tool_name":"Bash","command":"pytest tests/test_app.py","approval_preview":"cmd: pytest tests/test_app.py","risk_level":"medium","risk_summary":"Shell command can affect the local workspace.","ignored":42}}
    """#.data(using: .utf8)!
    let row = try JSONDecoder().decode(ApprovalRow.self, from: raw)
    try runner.expect(row.extras["tool_name"] == "Bash", "tool_name extra mismatch")
    try runner.expect(row.extras["ignored"] == nil, "non-string extra should be ignored")
    try runner.expect(row.toolName == "Bash", "toolName mismatch")
    try runner.expect(row.command == "pytest tests/test_app.py", "command mismatch")
    try runner.expect(row.approvalPreview == "cmd: pytest tests/test_app.py", "approvalPreview mismatch")
    try runner.expect(row.riskLevel == "medium", "riskLevel mismatch")
    try runner.expect(row.riskSummary == "Shell command can affect the local workspace.", "riskSummary mismatch")
    try runner.expect(row.detailLine == "cmd: pytest tests/test_app.py", "detailLine mismatch: \(String(describing: row.detailLine))")
}

runner.test("approval-row: detail line falls back to tool context") {
    let row = ApprovalRow(
        approvalId: "ap-6",
        extras: [
            "tool_action": "deskmate_open_app",
            "tool_target": "Calendar"
        ]
    )
    try runner.expect(row.detailLine == "tool: deskmate_open_app -> Calendar", "tool detail mismatch: \(String(describing: row.detailLine))")
}

// --- Phase 7: UPDATE_DOMAIN_STATE intent kind (L1-B) ------------------------

runner.test("intent-kind: update_domain_state maps to .updateDomainState") {
    let raw = #"""
    {"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"]}}}
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    try runner.expect(intent.kind == .updateDomainState, "kind should map: got \(intent.kind)")
    guard case .object(let payload) = intent.payload["domain_state"] ?? .null,
          case .array(let pending) = payload["pending_approvals"] ?? .null,
          case .string(let first) = pending.first ?? .null else {
        try runner.expect(false, "expected {domain_state:{pending_approvals:[...]}}")
        return
    }
    try runner.expect(first == "ap-1", "first pending id should be ap-1, got \(first)")
}

runner.test("intent-kind: unknown kind falls back to .unknown") {
    // Forward-compat: if Python ships a newer intent kind, the Swift
    // dispatcher must not crash. It should decode as .unknown and be
    // silently dropped by whoever consumes the intent.
    let raw = #"""
    {"kind":"teleport_user","payload":{}}
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    try runner.expect(intent.kind == .unknown, "unknown kind should decode as .unknown")
}

runner.test("intent-kind: known kinds still decode exactly") {
    let pairs: [(String, IntentKind)] = [
        ("show_pet_bubble", .showPetBubble),
        ("dismiss_pet_bubble", .dismissPetBubble),
        ("set_pet_animation", .setPetAnimation),
        ("set_avatar_mood", .setAvatarMood),
        ("present_island", .presentIsland),
        ("update_island", .updateIsland),
        ("dismiss_island", .dismissIsland),
        ("update_domain_state", .updateDomainState),
        ("register_module", .registerModule),
    ]
    for (raw, expected) in pairs {
        let data = "{\"kind\":\"\(raw)\"}".data(using: .utf8)!
        let intent = try JSONDecoder().decode(CompanionIntent.self, from: data)
        try runner.expect(intent.kind == expected, "\(raw) should decode to \(expected), got \(intent.kind)")
    }
}

runner.test("intent-kind: dismiss_pet_bubble payload carries bubble_id") {
    let raw = #"""
    {"kind":"dismiss_pet_bubble","payload":{"bubble_id":"approval-a1","approval_id":"a1"}}
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    try runner.expect(intent.kind == .dismissPetBubble, "kind should be .dismissPetBubble")
    guard case .string(let bid) = intent.payload["bubble_id"] ?? .null else {
        try runner.expect(false, "bubble_id must be a string")
        return
    }
    try runner.expect(bid == "approval-a1", "bubble_id mismatch: \(bid)")
}

runner.test("dispatcher: decodeRegisterModule parses module spec") {
    let intent = CompanionIntent(
        kind: .registerModule,
        payload: [
            "id": .string("kiro.spec"),
            "kind": .string("live_activity"),
            "activity_prefix": .string("kiro-spec-"),
            "title": .string("KIRO"),
            "subtitle": .string("{detail}"),
            "image": .string("k.circle"),
            "priority": .int(80),
        ]
    )
    let spec = try CompanionIntentDispatcher.decodeRegisterModule(from: intent)
    try runner.expect(spec.id == "kiro.spec", "id mismatch")
    try runner.expect(spec.kind == "live_activity", "kind mismatch")
    try runner.expect(spec.activityPrefix == "kiro-spec-", "activity prefix mismatch")
    try runner.expect(spec.title == "KIRO", "title mismatch")
    try runner.expect(spec.subtitle == "{detail}", "subtitle mismatch")
    try runner.expect(spec.image == "k.circle", "image mismatch")
    try runner.expect(spec.priority == 80, "priority mismatch")
}

runner.test("dispatcher: bindModuleRegistration applies decoded module") {
    let dispatcher = CompanionIntentDispatcher()
    var captured: RegisteredIslandModule?
    dispatcher.bindModuleRegistration { module in
        captured = module
    }
    let intent = CompanionIntent(
        kind: .registerModule,
        payload: [
            "id": .string("kiro.spec"),
            "kind": .string("live_activity"),
            "activity_prefix": .string("kiro-spec-"),
            "title": .string("KIRO"),
        ]
    )
    let result = dispatcher.dispatch(intent)
    try runner.expect(result == .handled(.registerModule), "register_module should be handled")
    try runner.expect(captured?.id == "kiro.spec", "captured module mismatch")
    try runner.expect(
        captured?.claims(state: IslandSurfaceState(
            kind: .liveActivity,
            activityId: "kiro-spec-plan"
        )) == true,
        "captured module should claim matching activity"
    )
}

// --- Phase 7b: LiveDomainStateStore + CompanionIntentDispatcher -------------

runner.test("live-store: apply new state notifies subscribers once") {
    let store = LiveDomainStateStore()
    var received: [[String]] = []
    let unsub = store.subscribe { received.append($0.pendingApprovals) }
    defer { unsub() }

    _ = store.apply(DomainState(pendingApprovals: ["a"]))
    _ = store.apply(DomainState(pendingApprovals: ["a"]))  // dedupe
    _ = store.apply(DomainState(pendingApprovals: ["a", "b"]))

    try runner.expect(received == [["a"], ["a", "b"]], "expected 2 updates, got \(received)")
    try runner.expect(store.current.pendingApprovals == ["a", "b"], "current wrong")
}

runner.test("live-store: unsubscribe stops callbacks") {
    let store = LiveDomainStateStore()
    var fired = 0
    let unsub = store.subscribe { _ in fired += 1 }
    _ = store.apply(DomainState(pendingApprovals: ["x"]))
    unsub()
    _ = store.apply(DomainState(pendingApprovals: ["x", "y"]))
    try runner.expect(fired == 1, "expected 1 fire, got \(fired)")
    try runner.expect(store.subscriberCount == 0, "subscriber should be cleared")
}

runner.test("dispatcher: known intent reaches handler") {
    let dispatcher = CompanionIntentDispatcher()
    var captured: IntentKind?
    dispatcher.register(kind: .showPetBubble) { captured = $0.kind }
    let result = dispatcher.dispatch(CompanionIntent(kind: .showPetBubble))
    try runner.expect(result == .handled(.showPetBubble), "result = \(result)")
    try runner.expect(captured == .showPetBubble, "handler not invoked")
}

runner.test("dispatcher: unknown intent dropped silently") {
    let dispatcher = CompanionIntentDispatcher()
    var fired = false
    dispatcher.register(kind: .unknown) { _ in fired = true }  // rejected
    let result = dispatcher.dispatch(CompanionIntent(kind: .unknown))
    try runner.expect(result == .droppedUnknown, "result = \(result)")
    try runner.expect(!fired, "handler for .unknown must not fire")
    try runner.expect(!dispatcher.hasHandler(for: .unknown), "registration of .unknown rejected")
}

runner.test("dispatcher: unregistered kind returns .noHandler") {
    let dispatcher = CompanionIntentDispatcher()
    let result = dispatcher.dispatch(CompanionIntent(kind: .setPetAnimation))
    try runner.expect(result == .noHandler(.setPetAnimation), "result = \(result)")
}

runner.test("dispatcher: bindDomainState applies UPDATE_DOMAIN_STATE payload") {
    let dispatcher = CompanionIntentDispatcher()
    let store = LiveDomainStateStore()
    dispatcher.bindDomainState(to: store)

    let raw = #"""
    {"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"],"agent_mood":"alert"}}}
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    dispatcher.dispatch(intent)

    try runner.expect(store.current.pendingApprovals == ["ap-1"], "pending wrong: \(store.current.pendingApprovals)")
    try runner.expect(store.current.agentMood == .alert, "mood wrong: \(store.current.agentMood)")
}

runner.test("dispatcher: malformed payload surfaces via error hook") {
    let dispatcher = CompanionIntentDispatcher()
    let store = LiveDomainStateStore()
    var errors = 0
    dispatcher.bindDomainState(to: store) { _ in errors += 1 }

    let bad = CompanionIntent(kind: .updateDomainState, payload: [:])
    dispatcher.dispatch(bad)

    try runner.expect(errors == 1, "expected 1 error, got \(errors)")
    try runner.expect(store.current == DomainState(), "store must not be mutated on decode failure")
}

// --- Phase 7d.0: BubbleSpec ttl_ms nullability regression -------------------

runner.test("bubble-spec: ttl_ms absent keeps legacy 8000ms default") {
    let raw = #"""
    {"id":"b1","kind":"chat","text":"hi","priority":"P2"}
    """#.data(using: .utf8)!
    let spec = try JSONDecoder().decode(BubbleSpec.self, from: raw)
    try runner.expect(spec.ttlMs == 8000, "absent ttl_ms should default to 8000, got \(String(describing: spec.ttlMs))")
}

runner.test("bubble-spec: ttl_ms=null decodes as nil (no auto-hide)") {
    let raw = #"""
    {"id":"b1","kind":"approval_hint","text":"ok?","ttl_ms":null,"priority":"P1"}
    """#.data(using: .utf8)!
    let spec = try JSONDecoder().decode(BubbleSpec.self, from: raw)
    try runner.expect(spec.ttlMs == nil, "explicit null should decode as nil, got \(String(describing: spec.ttlMs))")
}

runner.test("bubble-spec: ttl_ms=<int> round-trips") {
    let raw = #"""
    {"id":"b1","text":"hi","ttl_ms":2500,"priority":"P2"}
    """#.data(using: .utf8)!
    let spec = try JSONDecoder().decode(BubbleSpec.self, from: raw)
    try runner.expect(spec.ttlMs == 2500, "got \(String(describing: spec.ttlMs))")
}

// --- Phase 7d: LiveBubbleQueue + dispatcher binding -------------------------

runner.test("live-bubble-queue: enqueue notifies subscribers") {
    var now = 0
    let q = LiveBubbleQueue(maxActive: 5) { now }
    var events = 0
    let unsub = q.subscribe { _ in events += 1 }
    defer { unsub() }

    q.enqueue(BubbleSpec(id: "a", ttlMs: nil))
    q.enqueue(BubbleSpec(id: "b", ttlMs: nil))
    try runner.expect(events == 2, "expected 2 notifications, got \(events)")
    try runner.expect(q.count == 2, "count wrong: \(q.count)")
}

runner.test("live-bubble-queue: dismiss unknown id is silent no-op") {
    let q = LiveBubbleQueue(maxActive: 5) { 0 }
    q.enqueue(BubbleSpec(id: "a", ttlMs: nil))
    var events = 0
    let unsub = q.subscribe { _ in events += 1 }
    defer { unsub() }

    q.dismiss(id: "missing")
    try runner.expect(events == 0, "no-op should not notify, got \(events)")
    try runner.expect(q.count == 1, "count wrong: \(q.count)")
}

runner.test("live-bubble-queue: prune drops expired bubbles and notifies") {
    var now = 0
    let q = LiveBubbleQueue(maxActive: 5) { now }
    q.enqueue(BubbleSpec(id: "short", ttlMs: 1_000))
    q.enqueue(BubbleSpec(id: "long", ttlMs: 10_000))
    var events = 0
    let unsub = q.subscribe { _ in events += 1 }
    defer { unsub() }

    now = 2_000
    q.prune()
    try runner.expect(events == 1, "prune should notify once, got \(events)")
    try runner.expect(q.count == 1, "count wrong: \(q.count)")
    try runner.expect(q.peek()?.id == "long", "survivor wrong")
}

runner.test("dispatcher: bindBubbleQueue routes show_pet_bubble") {
    let dispatcher = CompanionIntentDispatcher()
    let queue = LiveBubbleQueue(maxActive: 5) { 0 }
    dispatcher.bindBubbleQueue(to: queue)

    let raw = #"""
    {
      "kind":"show_pet_bubble",
      "payload":{
        "bubble":{"id":"approval-a1","kind":"approval_hint","text":"Allow?","ttl_ms":null,"priority":"P1","actions":[]},
        "approval_id":"a1"
      }
    }
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    dispatcher.dispatch(intent)

    try runner.expect(queue.count == 1, "expected enqueue, got \(queue.count)")
    let spec = queue.peek()
    try runner.expect(spec?.id == "approval-a1", "id wrong")
    try runner.expect(spec?.kind == .approvalHint, "kind wrong")
    try runner.expect(spec?.ttlMs == nil, "ttl should be nil for approval bubble")
}

runner.test("dispatcher: bindBubbleQueue routes dismiss_pet_bubble") {
    let dispatcher = CompanionIntentDispatcher()
    let queue = LiveBubbleQueue(maxActive: 5) { 0 }
    dispatcher.bindBubbleQueue(to: queue)
    queue.enqueue(BubbleSpec(id: "approval-a1", ttlMs: nil))
    queue.enqueue(BubbleSpec(id: "approval-a2", ttlMs: nil))

    let raw = #"""
    {"kind":"dismiss_pet_bubble","payload":{"bubble_id":"approval-a1"}}
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    dispatcher.dispatch(intent)

    try runner.expect(queue.count == 1, "expected 1 left, got \(queue.count)")
    try runner.expect(queue.peek()?.id == "approval-a2", "survivor wrong")
}

runner.test("dispatcher: bindBubbleQueue malformed show surfaces error hook") {
    let dispatcher = CompanionIntentDispatcher()
    let queue = LiveBubbleQueue(maxActive: 5) { 0 }
    var errors = 0
    dispatcher.bindBubbleQueue(to: queue) { _ in errors += 1 }

    dispatcher.dispatch(CompanionIntent(kind: .showPetBubble, payload: [:]))
    try runner.expect(errors == 1, "expected 1 error, got \(errors)")
    try runner.expect(queue.count == 0, "queue must not mutate on error")
}

runner.test("dispatcher: bindBubbleQueue malformed dismiss surfaces error hook") {
    let dispatcher = CompanionIntentDispatcher()
    let queue = LiveBubbleQueue(maxActive: 5) { 0 }
    var errors = 0
    dispatcher.bindBubbleQueue(to: queue) { _ in errors += 1 }

    dispatcher.dispatch(CompanionIntent(kind: .dismissPetBubble, payload: [:]))
    try runner.expect(errors == 1, "expected 1 error, got \(errors)")
}

// --- Phase 9: LiveIslandSurfaceStore + bindIslandSurface (L1-E) -------------

runner.test("live-island: present notifies subscriber with slideIn") {
    var now = 0
    let s = LiveIslandSurfaceStore(clock: { now })
    var transitions: [IslandStateMachine.Transition] = []
    let unsub = s.subscribe { transitions.append($0.transition) }
    defer { unsub() }

    now = 1_000
    s.present(kind: .notificationCard, sessionId: "s1", priority: .p1)
    try runner.expect(transitions == [.slideIn], "expected slideIn, got \(transitions)")
    try runner.expect(s.surface.kind == .notificationCard, "surface wrong")
    try runner.expect(s.priority == .p1, "priority wrong")
}

runner.test("live-island: lower-priority present is gated") {
    let s = LiveIslandSurfaceStore(clock: { 0 })
    s.present(kind: .notificationCard, priority: .p0)
    var events = 0
    let unsub = s.subscribe { _ in events += 1 }
    defer { unsub() }

    s.present(kind: .sessionList, priority: .p3)
    try runner.expect(events == 0, "low-prio must be gated, got \(events) events")
    try runner.expect(s.surface.kind == .notificationCard, "surface should persist")
}

runner.test("live-island: dismiss clears to empty with slideOut") {
    let s = LiveIslandSurfaceStore(clock: { 0 })
    s.present(kind: .sessionList, sessionId: "s1", priority: .p2)
    var transitions: [IslandStateMachine.Transition] = []
    let unsub = s.subscribe { transitions.append($0.transition) }
    defer { unsub() }

    s.dismiss()
    try runner.expect(transitions == [.slideOut], "expected slideOut, got \(transitions)")
    try runner.expect(s.surface.kind == .empty, "should be empty")
}

runner.test("live-island: tick auto-dismisses non-pinned surface") {
    var now = 0
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .liveActivity, priority: .p2)
    let start = s.lastTouchedMs
    var events = 0
    let unsub = s.subscribe { _ in events += 1 }
    defer { unsub() }

    now = start + 15_000
    s.tick()
    try runner.expect(events == 1, "tick should auto-dismiss, got \(events)")
    try runner.expect(s.surface.kind == .empty, "should be empty")
}

runner.test("live-island: transient peek restores steady surface") {
    var now = 1_000
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .liveActivity, activityId: "steady", priority: .p1)
    now = 1_100
    s.present(kind: .notificationCard, activityId: "tool", priority: .p1)
    try runner.expect(s.isTransientActive, "notification should be transient")
    try runner.expect(s.surface.activityId == "tool", "peek should be visible")

    now = 4_000
    s.tick()
    try runner.expect(!s.isTransientActive, "transient should expire")
    try runner.expect(s.surface.kind == .liveActivity, "steady surface should return")
    try runner.expect(s.surface.activityId == "steady", "steady activity id should return")
}

runner.test("live-island: transient peek can cover expanded session list") {
    var now = 1_000
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .sessionList, priority: .p1)
    now = 1_100
    s.present(kind: .notificationCard, activityId: "reminder", priority: .p1)
    try runner.expect(s.isTransientActive, "notification should be transient")
    try runner.expect(s.surface.kind == .notificationCard, "peek should replace session list")
    try runner.expect(s.surface.activityId == "reminder", "peek activity id wrong")

    now = 4_000
    s.tick()
    try runner.expect(s.surface.kind == .sessionList, "session list should return")
}

runner.test("live-island: transient queue drains by priority then fifo") {
    var now = 1_000
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .liveActivity, activityId: "steady", priority: .p2)
    now = 1_100
    s.present(kind: .notificationCard, activityId: "first", priority: .p2)
    now = 1_200
    s.present(kind: .notificationCard, activityId: "urgent", priority: .p1)
    now = 1_300
    s.present(kind: .notificationCard, activityId: "second", priority: .p2)
    try runner.expect(s.surface.activityId == "first", "first transient should be visible")

    now = 3_600
    s.tick()
    try runner.expect(s.isTransientActive, "queued urgent transient should be active")
    try runner.expect(s.surface.activityId == "urgent", "P1 queued transient should win")

    now = 6_500
    s.tick()
    try runner.expect(s.isTransientActive, "queued second transient should be active")
    try runner.expect(s.surface.activityId == "second", "P2 fifo transient should follow")

    now = 9_000
    s.tick()
    try runner.expect(!s.isTransientActive, "queue should drain")
    try runner.expect(s.surface.activityId == "steady", "steady surface should return")
}

runner.test("live-island: steady update during transient is restored") {
    var now = 1_000
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .liveActivity, activityId: "steady", detail: "old", priority: .p2)
    now = 1_100
    s.present(kind: .notificationCard, activityId: "peek", priority: .p2)
    now = 1_200
    s.update(activityId: "steady", detail: "new")

    now = 3_600
    s.tick()
    try runner.expect(!s.isTransientActive, "transient should expire")
    try runner.expect(s.surface.activityId == "steady", "steady should return")
    try runner.expect(s.surface.detail == "new", "updated steady detail should return")
}

runner.test("live-island: transient peek from idle expires to empty") {
    var now = 1_000
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .notificationCard, activityId: "done", priority: .p2)
    try runner.expect(s.isTransientActive, "notification should be transient")
    try runner.expect(s.surface.activityId == "done", "peek should be visible")

    now = 3_500
    s.tick()
    try runner.expect(!s.isTransientActive, "transient should expire")
    try runner.expect(s.surface.kind == .empty, "idle steady surface should return")
}

runner.test("live-island: P0 approval is pinned not transient") {
    var now = 1_000
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .notificationCard, activityId: "approval", priority: .p0)
    try runner.expect(!s.isTransientActive, "P0 must not be transient")
    now = 10_000
    s.tick()
    try runner.expect(s.surface.activityId == "approval", "P0 approval should remain")
}

runner.test("live-island: transient peek cannot replace pinned P0 approval") {
    var now = 1_000
    let s = LiveIslandSurfaceStore(clock: { now })
    s.present(kind: .notificationCard, activityId: "approval", priority: .p0)
    now = 1_100
    s.present(kind: .notificationCard, activityId: "tool", priority: .p1)
    try runner.expect(!s.isTransientActive, "blocked peek should not become transient")
    try runner.expect(s.surface.activityId == "approval", "P0 approval should stay visible")
}

runner.test("dispatcher: bindIslandSurface present with explicit priority") {
    let dispatcher = CompanionIntentDispatcher()
    let store = LiveIslandSurfaceStore(clock: { 1_000 })
    dispatcher.bindIslandSurface(to: store)

    let raw = #"""
    {"kind":"present_island","payload":{"surface":"notification_card","session_id":"abc","priority":"P1"}}
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    dispatcher.dispatch(intent)

    try runner.expect(store.surface.kind == .notificationCard, "kind wrong")
    try runner.expect(store.surface.sessionId == "abc", "session_id wrong")
    try runner.expect(store.priority == .p1, "priority wrong: \(store.priority)")
}

runner.test("dispatcher: bindIslandSurface present defaults priority to P2") {
    let dispatcher = CompanionIntentDispatcher()
    let store = LiveIslandSurfaceStore(clock: { 0 })
    dispatcher.bindIslandSurface(to: store)

    let raw = #"""
    {"kind":"present_island","payload":{"surface":"session_list"}}
    """#.data(using: .utf8)!
    let intent = try JSONDecoder().decode(CompanionIntent.self, from: raw)
    dispatcher.dispatch(intent)

    try runner.expect(store.surface.kind == .sessionList, "kind wrong")
    try runner.expect(store.priority == .p2, "priority default wrong")
}

runner.test("dispatcher: bindIslandSurface unknown surface raises decode error") {
    let dispatcher = CompanionIntentDispatcher()
    let store = LiveIslandSurfaceStore(clock: { 0 })
    var errors = 0
    dispatcher.bindIslandSurface(to: store) { _ in errors += 1 }

    dispatcher.dispatch(CompanionIntent(
        kind: .presentIsland,
        payload: ["surface": .string("teleport")]
    ))
    try runner.expect(errors == 1, "expected 1 error, got \(errors)")
    try runner.expect(store.surface.kind == .empty, "store must stay empty")
}

runner.test("dispatcher: bindIslandSurface update without activity_id raises") {
    let dispatcher = CompanionIntentDispatcher()
    let store = LiveIslandSurfaceStore(clock: { 0 })
    var errors = 0
    dispatcher.bindIslandSurface(to: store) { _ in errors += 1 }

    dispatcher.dispatch(CompanionIntent(kind: .updateIsland, payload: [:]))
    try runner.expect(errors == 1, "expected 1 error, got \(errors)")
}

runner.test("dispatcher: bindIslandSurface dismiss empty payload clears surface") {
    let dispatcher = CompanionIntentDispatcher()
    let store = LiveIslandSurfaceStore(clock: { 0 })
    dispatcher.bindIslandSurface(to: store)
    store.present(kind: .notificationCard, sessionId: "s1", priority: .p2)

    dispatcher.dispatch(CompanionIntent(kind: .dismissIsland, payload: [:]))
    try runner.expect(store.surface.kind == .empty, "empty payload should clear: \(store.surface.kind)")
}

// --- Phase 10a: EnvelopeFraming (L3-D4) -------------------------------------

func smokePingLine(_ trace: String) -> Data {
    let body = #"""
    {"spec_version":1,"type":"ping","trace_id":"\#(trace)","payload":{}}
    """#
    return Data(body.utf8) + Data([EnvelopeFraming.separator])
}

runner.test("framing: single line decodes to one envelope") {
    var framing = EnvelopeFraming()
    let out = framing.feedEnvelopes(smokePingLine("t1"))
    try runner.expect(out.count == 1, "expected 1, got \(out.count)")
    try runner.expect(out.first?.type == .ping, "type wrong")
    try runner.expect(framing.pendingByteCount == 0, "nothing should be buffered")
}

runner.test("framing: splits multi-line chunk in order") {
    var framing = EnvelopeFraming()
    var chunk = smokePingLine("a")
    chunk.append(smokePingLine("b"))
    chunk.append(smokePingLine("c"))
    let traces = framing.feedEnvelopes(chunk).map(\.traceId)
    try runner.expect(traces == ["a", "b", "c"], "order wrong: \(traces)")
}

runner.test("framing: reassembles across partial chunks") {
    var framing = EnvelopeFraming()
    let full = smokePingLine("x")
    let half = full.count / 2
    let first = framing.feedEnvelopes(full.prefix(half))
    try runner.expect(first.isEmpty, "partial chunk must not yield envelope")
    let second = framing.feedEnvelopes(full.suffix(from: half))
    try runner.expect(second.count == 1, "tail should complete it")
    try runner.expect(second.first?.traceId == "x", "trace lost")
}

runner.test("framing: empty + whitespace lines are silently dropped") {
    var framing = EnvelopeFraming()
    var chunk = Data("\n\n".utf8)
    chunk.append(smokePingLine("g"))
    chunk.append(Data("   \t \n".utf8))
    let traces = framing.feedEnvelopes(chunk).map(\.traceId)
    try runner.expect(traces == ["g"], "should keep only the good one, got \(traces)")
}

runner.test("framing: malformed JSON reports error, keeps good lines") {
    var framing = EnvelopeFraming()
    var chunk = smokePingLine("good-1")
    chunk.append(Data("{not-json}\n".utf8))
    chunk.append(smokePingLine("good-2"))
    var errorCount = 0
    let out = framing.feedEnvelopes(chunk) { _ in errorCount += 1 }
    try runner.expect(out.map(\.traceId) == ["good-1", "good-2"], "good lines lost")
    try runner.expect(errorCount == 1, "expected 1 decode error, got \(errorCount)")
}

runner.test("framing: utf-8 multibyte payload round-trips") {
    let env = BridgeEnvelope.of(
        .userMessage,
        payload: ["text": .string("你好 🌏")],
        traceId: "u8"
    )
    let encoded = try EnvelopeFraming.encode(env)
    var framing = EnvelopeFraming()
    let out = framing.feedEnvelopes(encoded)
    try runner.expect(out.count == 1, "expected 1")
    try runner.expect(out.first?.payload["text"] == .string("你好 🌏"), "payload lost")
}

runner.test("framing: encoded byte ends with newline 0x0A") {
    let env = BridgeEnvelope.of(.ping, traceId: "t")
    let data = try EnvelopeFraming.encode(env)
    try runner.expect(data.last == EnvelopeFraming.separator, "missing NL terminator")
}

runner.test("framing: byte-at-a-time streaming still completes") {
    var framing = EnvelopeFraming()
    let full = smokePingLine("stream")
    var seen: [BridgeEnvelope] = []
    for byte in full {
        seen.append(contentsOf: framing.feedEnvelopes(Data([byte])))
    }
    try runner.expect(seen.count == 1, "expected 1, got \(seen.count)")
    try runner.expect(seen.first?.traceId == "stream", "trace lost")
    try runner.expect(framing.pendingByteCount == 0, "buffer should be drained")
}

runner.test("framing: python-shaped payload (spec_version / trace_id snake case) decodes") {
    // Byte sequence replicates Python's encode_envelope output: UTF-8,
    // compact separators, snake_case keys, NL terminator. This locks in
    // cross-language wire compatibility regardless of Swift JSONEncoder
    // key ordering differences.
    let raw = #"""
    {"spec_version":1,"type":"intent","trace_id":"xyz","ts_ms":1730000000000,"payload":{"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"]}}}}
    """#
    var framing = EnvelopeFraming()
    let chunk = Data(raw.utf8) + Data([EnvelopeFraming.separator])
    let out = framing.feedEnvelopes(chunk)
    try runner.expect(out.count == 1, "expected 1")
    let env = out[0]
    try runner.expect(env.type == .intent, "type wrong: \(env.type)")
    try runner.expect(env.traceId == "xyz", "trace_id wrong")
    try runner.expect(env.tsMs == 1_730_000_000_000, "ts_ms wrong: \(String(describing: env.tsMs))")
}

// --- Phase 10b: BridgeClient over socketpair (L3-D3) ------------------------

func smokeWriteAll(fd: Int32, _ data: Data) {
    data.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var offset = 0
        while offset < data.count {
            #if canImport(Darwin)
            let n = Darwin.write(fd, base.advanced(by: offset), data.count - offset)
            #else
            let n = write(fd, base.advanced(by: offset), data.count - offset)
            #endif
            if n < 0 {
                if errno == EINTR { continue }
                return
            }
            offset += n
        }
    }
}

func smokeEncodedPing(_ trace: String) -> Data {
    try! EnvelopeFraming.encode(BridgeEnvelope.of(.ping, traceId: trace))
}

runner.test("bridge-client: receives envelope written to peer fd") {
    // Smoke tests run on a plain CLI tool (no main runloop), so
    // callbacks must fire on a background queue we can await on.
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    let sem = DispatchSemaphore(value: 0)
    var received: BridgeEnvelope?
    client.onEnvelope { env in received = env; sem.signal() }

    smokeWriteAll(fd: agentFd, smokeEncodedPing("t1"))
    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "timeout waiting for envelope")
    try runner.expect(received?.traceId == "t1", "trace wrong: \(String(describing: received?.traceId))")
}

runner.test("bridge-client: sends encoded envelope to peer") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    let env = BridgeEnvelope.of(
        .userMessage,
        payload: ["text": .string("hello")],
        traceId: "send"
    )
    try client.send(env)

    // Drain from the agent end.
    var buf = [UInt8](repeating: 0, count: 512)
    let n: Int = buf.withUnsafeMutableBufferPointer { ptr in
        #if canImport(Darwin)
        return Darwin.read(agentFd, ptr.baseAddress, ptr.count)
        #else
        return read(agentFd, ptr.baseAddress, ptr.count)
        #endif
    }
    try runner.expect(n > 0, "expected bytes on peer end, got \(n)")
    let data = Data(bytes: buf, count: n)
    try runner.expect(data.last == EnvelopeFraming.separator, "missing NL terminator")
    var framing = EnvelopeFraming()
    let envs = framing.feedEnvelopes(data)
    try runner.expect(envs.first?.traceId == "send", "round-trip trace lost")
    try runner.expect(envs.first?.payload["text"] == .string("hello"), "payload lost")
}

runner.test("bridge-client: peer close transitions to .disconnected") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop() }

    let sem = DispatchSemaphore(value: 0)
    client.onStateChange { state in
        if state == .disconnected { sem.signal() }
    }
    close(agentFd)
    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "timeout waiting for disconnect")
    try runner.expect(client.state == .disconnected, "state wrong: \(client.state)")
}

runner.test("bridge-client: stop is idempotent") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    client.stop()
    client.stop()  // must not crash
    close(agentFd)
    try runner.expect(client.state == .disconnected, "should be disconnected")
}

runner.test("bridge-client: send before start throws .notConnected") {
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    let env = BridgeEnvelope.of(.ping)
    do {
        try client.send(env)
        try runner.expect(false, "expected notConnected throw")
    } catch BridgeClient.Error.notConnected {
        // ok
    } catch {
        try runner.expect(false, "wrong error: \(error)")
    }
}

runner.test("bridge-client: malformed line triggers decode error handler") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    let sem = DispatchSemaphore(value: 0)
    client.onDecodeError { _ in sem.signal() }
    smokeWriteAll(fd: agentFd, Data("{not-json}\n".utf8))
    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "timeout waiting for decode error")
}

// --- Phase 10c: dispatcher.bind(bridge:) end-to-end --------------------------

runner.test("bridge↔dispatcher: python-shaped intent reaches registered handler") {
    // End-to-end: a byte-for-byte Python encode_envelope output arrives
    // on the socket, the BridgeClient frames + decodes it, the
    // dispatcher forwards it to a handler registered for that kind.
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let bridge = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try bridge.start(preConnectedFd: clientFd)
    defer { bridge.stop(); close(agentFd) }

    let dispatcher = CompanionIntentDispatcher()
    dispatcher.bind(bridge: bridge)

    let sem = DispatchSemaphore(value: 0)
    var capturedKind: IntentKind?
    var capturedPending: [String] = []
    dispatcher.register(kind: .updateDomainState) { intent in
        capturedKind = intent.kind
        if case .object(let ds) = intent.payload["domain_state"] ?? .null,
           case .array(let pending) = ds["pending_approvals"] ?? .null {
            capturedPending = pending.compactMap {
                if case .string(let s) = $0 { return s }
                return nil
            }
        }
        sem.signal()
    }

    let wire = #"""
    {"spec_version":1,"type":"intent","trace_id":"t1","ts_ms":1730000000000,"payload":{"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"]}}}}
    """#
    var chunk = Data(wire.utf8)
    chunk.append(EnvelopeFraming.separator)
    smokeWriteAll(fd: agentFd, chunk)

    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "timeout waiting for dispatch")
    try runner.expect(capturedKind == .updateDomainState, "kind wrong: \(String(describing: capturedKind))")
    try runner.expect(capturedPending == ["ap-1"], "pending wrong: \(capturedPending)")
}

runner.test("bridge↔dispatcher: non-intent envelopes are ignored") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let bridge = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try bridge.start(preConnectedFd: clientFd)
    defer { bridge.stop(); close(agentFd) }

    let dispatcher = CompanionIntentDispatcher()
    var dispatchCount = 0
    dispatcher.bind(bridge: bridge)
    dispatcher.register(kind: .showPetBubble) { _ in dispatchCount += 1 }

    // Send a snapshot (should be ignored) + an intent (should fire).
    let snapshot = BridgeEnvelope.of(
        .stateSnapshot, payload: ["domain_state": .object([:])], traceId: "s1"
    )
    smokeWriteAll(fd: agentFd, try EnvelopeFraming.encode(snapshot))

    let sem = DispatchSemaphore(value: 0)
    dispatcher.register(kind: .showPetBubble) { _ in
        dispatchCount += 1
        sem.signal()
    }
    let intentWire = #"""
    {"spec_version":1,"type":"intent","trace_id":"t2","payload":{"kind":"show_pet_bubble","payload":{"bubble":{"id":"b","ttl_ms":null}}}}
    """#
    var chunk = Data(intentWire.utf8)
    chunk.append(EnvelopeFraming.separator)
    smokeWriteAll(fd: agentFd, chunk)

    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "timeout waiting for intent")
    try runner.expect(dispatchCount == 1, "only the intent should dispatch, got \(dispatchCount)")
}

runner.test("bridge↔dispatcher: malformed intent payload forwarded to error hook") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let bridge = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try bridge.start(preConnectedFd: clientFd)
    defer { bridge.stop(); close(agentFd) }

    let dispatcher = CompanionIntentDispatcher()
    let sem = DispatchSemaphore(value: 0)
    dispatcher.bind(bridge: bridge) { _ in sem.signal() }

    // Envelope is valid; its payload lacks the required ``kind`` field
    // so CompanionIntent decoding must fail.
    let malformed = #"""
    {"spec_version":1,"type":"intent","trace_id":"t3","payload":{"payload":{}}}
    """#
    var chunk = Data(malformed.utf8)
    chunk.append(EnvelopeFraming.separator)
    smokeWriteAll(fd: agentFd, chunk)

    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "timeout waiting for decode error")
}

// --- Phase 11a: ReconnectingBridgeClient ------------------------------------

final class SmokeReconnectHarness: @unchecked Sendable {
    var scripted: [() throws -> BridgeClient] = []
    var peerFds: [Int32] = []
    private var callCount = 0
    private let lock = NSLock()

    func pushSuccess() {
        lock.lock(); defer { lock.unlock() }
        scripted.append { [weak self] in
            let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
            let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
            try client.start(preConnectedFd: clientFd)
            self?.lock.lock(); self?.peerFds.append(agentFd); self?.lock.unlock()
            return client
        }
    }

    func pushFailure() {
        lock.lock(); defer { lock.unlock() }
        scripted.append {
            throw BridgeClient.Error.connectFailed("smoke-forced-failure")
        }
    }

    func factory() throws -> BridgeClient {
        lock.lock()
        let idx = callCount
        callCount += 1
        let step = idx < scripted.count ? scripted[idx] : nil
        lock.unlock()
        guard let step else {
            throw BridgeClient.Error.socketFailed("harness exhausted")
        }
        return try step()
    }

    var calls: Int { lock.lock(); defer { lock.unlock() }; return callCount }
    var peerSnapshot: [Int32] { lock.lock(); defer { lock.unlock() }; return peerFds }
}

runner.test("reconnecting: first attempt success, single client reaches .connected") {
    let harness = SmokeReconnectHarness()
    harness.pushSuccess()
    let rc = ReconnectingBridgeClient(
        factory: harness.factory,
        configuration: .init(initialBackoff: 0.02, maxBackoff: 0.05, multiplier: 2, jitterFraction: 0),
        callbackQueue: .global(qos: .userInitiated)
    )
    defer { rc.stop(); for fd in harness.peerSnapshot where fd > 0 { close(fd) } }

    let sem = DispatchSemaphore(value: 0)
    rc.onStateChange { s in if s == .connected { sem.signal() } }
    rc.start()
    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "timeout waiting for first connect")
    try runner.expect(rc.state == .connected, "state wrong: \(rc.state)")
    try runner.expect(harness.calls == 1, "expected 1 factory call, got \(harness.calls)")
}

runner.test("reconnecting: first failure backs off, second attempt connects") {
    let harness = SmokeReconnectHarness()
    harness.pushFailure()
    harness.pushSuccess()
    let rc = ReconnectingBridgeClient(
        factory: harness.factory,
        configuration: .init(initialBackoff: 0.02, maxBackoff: 0.1, multiplier: 2, jitterFraction: 0),
        callbackQueue: .global(qos: .userInitiated)
    )
    defer { rc.stop(); for fd in harness.peerSnapshot where fd > 0 { close(fd) } }

    let sem = DispatchSemaphore(value: 0)
    rc.onStateChange { s in if s == .connected { sem.signal() } }
    rc.start()
    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "did not recover after failure")
    try runner.expect(harness.calls == 2, "expected 2 factory calls, got \(harness.calls)")
}

// --- Phase 11b: EnvelopeSender (high-level send helpers) --------------------

func smokeDrain(fd: Int32, timeout: TimeInterval = 1.0) -> Data {
    var out = Data()
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        var buf = [UInt8](repeating: 0, count: 1024)
        let n: Int = buf.withUnsafeMutableBufferPointer { ptr in
            #if canImport(Darwin)
            return Darwin.read(fd, ptr.baseAddress, ptr.count)
            #else
            return read(fd, ptr.baseAddress, ptr.count)
            #endif
        }
        if n > 0 {
            out.append(buf, count: n)
            if out.contains(EnvelopeFraming.separator) { return out }
        } else if n == 0 {
            return out
        } else {
            if errno == EINTR { continue }
            Thread.sleep(forTimeInterval: 0.01)
        }
    }
    return out
}

runner.test("sender: send(action:) wraps InteractionAction in .interaction envelope") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    let action = InteractionAction(
        source: .pet, target: .bubble, kind: .permissionResolve,
        payload: ["approval_id": .string("ap-1"), "allow": .bool(true)]
    )
    try client.send(action: action, traceId: "t-act")

    let bytes = smokeDrain(fd: agentFd)
    var framing = EnvelopeFraming()
    let env = framing.feedEnvelopes(bytes).first
    try runner.expect(env?.type == .interaction, "envelope type wrong")
    try runner.expect(env?.traceId == "t-act", "trace wrong")
    try runner.expect(env?.payload["kind"] == .string("permission.resolve"), "kind wrong")
    try runner.expect(env?.payload["source"] == .string("pet"), "source wrong")
    guard case .object(let inner) = env?.payload["payload"] ?? .null else {
        try runner.expect(false, "expected inner object")
        return
    }
    try runner.expect(inner["approval_id"] == .string("ap-1"), "approval_id wrong")
    try runner.expect(inner["allow"] == .bool(true), "allow wrong")
}

runner.test("sender: sendUserMessage carries text payload") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    try client.sendUserMessage("hello", traceId: "t-msg")
    let bytes = smokeDrain(fd: agentFd)
    var framing = EnvelopeFraming()
    let env = framing.feedEnvelopes(bytes).first
    try runner.expect(env?.type == .userMessage, "type wrong")
    try runner.expect(env?.payload["text"] == .string("hello"), "text wrong")
}

runner.test("sender: sendUserClickPet has empty payload") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    try client.sendUserClickPet(traceId: "t-click")
    let bytes = smokeDrain(fd: agentFd)
    var framing = EnvelopeFraming()
    let env = framing.feedEnvelopes(bytes).first
    try runner.expect(env?.type == .userClickPet, "type wrong")
    try runner.expect(env?.payload.count == 0, "payload should be empty, got \(String(describing: env?.payload))")
}

runner.test("sender: sendPerception uses snake_case keys matching Python reader") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    let snap = PerceptionSnapshot(
        userState: "active",
        focus: .focused,
        app: "com.apple.Terminal",
        title: "bash",
        idleMs: 1_500
    )
    try client.sendPerception(snap, traceId: "t-perc")

    let bytes = smokeDrain(fd: agentFd)
    var framing = EnvelopeFraming()
    let env = framing.feedEnvelopes(bytes).first
    try runner.expect(env?.type == .perception, "type wrong")
    try runner.expect(env?.payload["user_state"] == .string("active"), "user_state wrong")
    try runner.expect(env?.payload["focus"] == .string("focused"), "focus wrong")
    try runner.expect(env?.payload["app"] == .string("com.apple.Terminal"), "app wrong")
    try runner.expect(env?.payload["title"] == .string("bash"), "title wrong")
    try runner.expect(env?.payload["idle_ms"] == .int(1_500), "idle_ms wrong")
}

// --- Phase 11c: DeskmateShell (headless composition) -----------------------

final class SmokeShellHarness: @unchecked Sendable {
    var peerFds: [Int32] = []
    private let lock = NSLock()
    private var calls = 0

    func factory() throws -> BridgeClient {
        lock.lock(); calls += 1; lock.unlock()
        let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
        let c = BridgeClient(callbackQueue: .global(qos: .userInitiated))
        try c.start(preConnectedFd: clientFd)
        lock.lock(); peerFds.append(agentFd); lock.unlock()
        return c
    }

    var peerSnapshot: [Int32] {
        lock.lock(); defer { lock.unlock() }
        return peerFds
    }
}

runner.test("shell: incoming update_domain_state drives domainState store") {
    let harness = SmokeShellHarness()
    var config = DeskmateShell.Configuration(
        bridgeBackoff: .init(
            initialBackoff: 0.01, maxBackoff: 0.05,
            multiplier: 2.0, jitterFraction: 0
        )
    )
    config.clientFactory = harness.factory
    let shell = DeskmateShell(
        configuration: config,
        callbackQueue: .global(qos: .userInitiated)
    )
    defer { shell.stop(); for fd in harness.peerSnapshot where fd > 0 { close(fd) } }

    let connectedSem = DispatchSemaphore(value: 0)
    shell.bridge.onStateChange { s in if s == .connected { connectedSem.signal() } }
    shell.start()
    try runner.expect(connectedSem.wait(timeout: .now() + 2.0) == .success, "shell did not connect")

    let domainSem = DispatchSemaphore(value: 0)
    let unsub = shell.domainState.subscribe { state in
        if state.pendingApprovals == ["ap-1"] && state.agentMood == .alert {
            domainSem.signal()
        }
    }
    defer { unsub() }

    let wire = #"""
    {"spec_version":1,"type":"intent","trace_id":"t","payload":{"kind":"update_domain_state","payload":{"domain_state":{"pending_approvals":["ap-1"],"agent_mood":"alert"}}}}
    """#
    var bytes = Data(wire.utf8)
    bytes.append(EnvelopeFraming.separator)
    smokeWriteAll(fd: harness.peerSnapshot[0], bytes)

    try runner.expect(domainSem.wait(timeout: .now() + 2.0) == .success, "domainState did not update")
    try runner.expect(shell.domainState.current.pendingApprovals == ["ap-1"], "pending wrong")
    try runner.expect(shell.domainState.current.agentMood == .alert, "mood wrong")
}

// --- Phase 11d-i: PerceptionSampler + DefaultSocketPath ---------------------

final class SmokeRecordingSender: EnvelopeSender, @unchecked Sendable {
    var envelopes: [BridgeEnvelope] = []
    private let lock = NSLock()
    func send(_ envelope: BridgeEnvelope) throws {
        lock.lock(); envelopes.append(envelope); lock.unlock()
    }
    var perceptions: [PerceptionSnapshot] {
        lock.lock(); defer { lock.unlock() }
        return envelopes.compactMap { env in
            guard env.type == .perception else { return nil }
            guard let data = try? JSONEncoder().encode(env.payload)
            else { return nil }
            return try? JSONDecoder().decode(
                PerceptionSnapshot.self, from: data
            )
        }
    }
}

final class SmokeSamplerScenario: @unchecked Sendable {
    var idle: TimeInterval = 0
    var app = PerceptionSampler.FrontmostApp(
        bundleId: "com.apple.Terminal", title: "bash"
    )
    var nowMs: Int = 0
}

runner.test("perception-sampler: initial tick sends one snapshot") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            tickInterval: 60,
            heartbeatInterval: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs }
        )
    )
    s.tick()
    try runner.expect(sender.perceptions.count == 1, "expected 1, got \(sender.perceptions.count)")
    try runner.expect(sender.perceptions[0].app == "com.apple.Terminal", "app wrong")
    try runner.expect(sender.perceptions[0].focus == .focused, "focus wrong")
}

runner.test("perception-sampler: unchanged state dedupes") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            heartbeatInterval: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs }
        )
    )
    s.tick()
    scenario.nowMs = 500
    s.tick()
    try runner.expect(sender.perceptions.count == 1, "no dedupe, got \(sender.perceptions.count)")
}

runner.test("perception-sampler: app change re-sends") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs }
        )
    )
    s.tick()
    scenario.app = .init(bundleId: "com.apple.Safari", title: "apple.com")
    scenario.nowMs = 1_000
    s.tick()
    try runner.expect(sender.perceptions.count == 2, "expected 2, got \(sender.perceptions.count)")
    try runner.expect(sender.perceptions.last?.app == "com.apple.Safari", "latest app wrong")
}

runner.test("perception-sampler: heartbeat resends even with no change") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            heartbeatInterval: 1,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs }
        )
    )
    s.tick()
    scenario.nowMs = 500  // <1s
    s.tick()
    try runner.expect(sender.perceptions.count == 1, "premature heartbeat")
    scenario.nowMs = 2_000  // >1s total
    s.tick()
    try runner.expect(sender.perceptions.count == 2, "missed heartbeat, got \(sender.perceptions.count)")
}

runner.test("perception-sampler: wire keys are snake_case") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs }
        )
    )
    s.tick()
    let payload = sender.envelopes[0].payload
    try runner.expect(payload["user_state"] != nil, "missing user_state")
    try runner.expect(payload["idle_ms"] != nil, "missing idle_ms")
    try runner.expect(payload["userState"] == nil, "camelCase leaked")
}

runner.test("perception-sampler: paused sampler skips ticks until resumed") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs }
        )
    )
    s.setPaused(true)
    try runner.expect(s.isPaused == true, "sampler should report paused")
    s.tick()
    try runner.expect(sender.perceptions.isEmpty, "paused sampler should not send")
    s.setPaused(false)
    s.tick()
    try runner.expect(sender.perceptions.count == 1, "resumed sampler should send first snapshot")
}

runner.test("perception-sampler: cached frontmost provider honors ttl") {
    var now: TimeInterval = 0
    var calls = 0
    let cached = CachedFrontmostAppProvider(
        ttl: 1.0,
        clock: { now },
        provider: {
            calls += 1
            return .init(bundleId: "app.\(calls)", title: "App \(calls)")
        }
    )
    try runner.expect(cached().bundleId == "app.1", "first read wrong")
    now = 0.5
    try runner.expect(cached().bundleId == "app.1", "ttl cache missed")
    try runner.expect(calls == 1, "provider called too often: \(calls)")
    now = 1.1
    try runner.expect(cached().bundleId == "app.2", "ttl refresh missed")
    cached.invalidate()
    try runner.expect(cached().bundleId == "app.3", "invalidate should force refresh")
}

// --- V10 L3-B10 / E1 / E2: adaptive perception pacing -----------------------

runner.test("perception-pacer: maps focus tiers to monotonic intervals") {
    let pacer = PerceptionPacer()
    try runner.expect(pacer.interval(for: .focused) == 1.0, "focused 1s")
    try runner.expect(pacer.interval(for: .casual) == 2.0, "casual 2s")
    try runner.expect(pacer.interval(for: .idleBack) == 5.0, "idleBack 5s")
}

runner.test("perception-pacer: monotonic guard widens out-of-order overrides") {
    // Caller mistakenly asks for casual=0.5s (tighter than focused=1s).
    // Guard must clamp casual ≥ focused so a config typo can't make
    // the sampler tighter on idle than on active.
    let pacer = PerceptionPacer(
        focusedInterval: 1.0,
        casualInterval: 0.5,
        idleBackInterval: 0.2
    )
    try runner.expect(pacer.casualInterval == 1.0, "casual should widen to focused")
    try runner.expect(pacer.idleBackInterval == 1.0, "idleBack should widen to casual")
}

runner.test("perception-pacer: asleep returns nil regardless of focus") {
    let pacer = PerceptionPacer()
    try runner.expect(
        pacer.interval(for: .focused, isAsleep: true) == nil,
        "asleep must override focused"
    )
    try runner.expect(
        pacer.interval(for: .idleBack, isAsleep: true) == nil,
        "asleep must override idleBack"
    )
}

runner.test("perception-sampler: pacer reschedules timer after focus drift") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    // Force focused → idleBack via the focus inferrer.
    scenario.idle = 0
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            tickInterval: 60,  // ignored when pacer is set
            heartbeatInterval: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs },
            pacer: PerceptionPacer()
        )
    )
    s.tick()
    try runner.expect(
        abs(s.currentTickInterval - 1.0) < 0.01,
        "expected focused interval 1s, got \(s.currentTickInterval)"
    )

    // Drift to casual.
    scenario.idle = 30
    scenario.nowMs += 1_000
    s.tick()
    try runner.expect(
        abs(s.currentTickInterval - 2.0) < 0.01,
        "expected casual interval 2s, got \(s.currentTickInterval)"
    )

    // Drift to idleBack.
    scenario.idle = 200
    scenario.nowMs += 30_000
    s.tick()
    try runner.expect(
        abs(s.currentTickInterval - 5.0) < 0.01,
        "expected idleBack interval 5s, got \(s.currentTickInterval)"
    )
    try runner.expect(s.rescheduleCount >= 2, "expected at least 2 reschedules")
}

runner.test("perception-sampler: pacer holds steady on same focus tier") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            tickInterval: 60,
            heartbeatInterval: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs },
            pacer: PerceptionPacer()
        )
    )
    s.tick()
    let initialReschedules = s.rescheduleCount
    s.tick()
    s.tick()
    // Same focus → no extra reschedule beyond what the start-up
    // `tickLocked` may have produced.
    try runner.expect(
        s.rescheduleCount == initialReschedules,
        "expected no extra reschedules: \(s.rescheduleCount)"
    )
}

runner.test("perception-sampler: noteFrontmostChanged triggers immediate send") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            tickInterval: 60,
            heartbeatInterval: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs },
            pacer: PerceptionPacer()
        )
    )
    s.tick()
    let baseline = sender.perceptions.count

    // Simulate the user switching apps. The runtime would also
    // invalidate the cached frontmost provider, but here we just
    // mutate the scenario directly.
    scenario.app = .init(bundleId: "com.apple.Safari", title: "Safari")
    scenario.nowMs += 100
    s.noteFrontmostChanged()

    try runner.expect(
        sender.perceptions.count == baseline + 1,
        "expected one extra perception send, got \(sender.perceptions.count - baseline)"
    )
    try runner.expect(
        sender.perceptions.last?.app == "com.apple.Safari",
        "freshly switched app not on the wire"
    )
}

runner.test("perception-sampler: noteFrontmostChanged is a no-op while paused") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            tickInterval: 60,
            heartbeatInterval: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs },
            pacer: PerceptionPacer()
        )
    )
    s.setPaused(true)
    s.noteFrontmostChanged()
    try runner.expect(
        sender.perceptions.isEmpty,
        "paused sampler should never send"
    )
}

runner.test("perception-sampler: unpause reschedules to current focus") {
    let sender = SmokeRecordingSender()
    let scenario = SmokeSamplerScenario()
    let s = PerceptionSampler(
        sender: sender,
        configuration: .init(
            tickInterval: 60,
            heartbeatInterval: 30,
            idleProvider: { scenario.idle },
            frontmostAppProvider: { scenario.app },
            clock: { scenario.nowMs },
            pacer: PerceptionPacer()
        )
    )
    s.tick()
    let pacedInterval = s.currentTickInterval
    s.setPaused(true)
    s.setPaused(false)
    // After unpausing we should be back at the same paced interval
    // (no need to wait for a tick to re-arm).
    try runner.expect(
        abs(s.currentTickInterval - pacedInterval) < 0.01,
        "unpause should restore paced cadence: \(s.currentTickInterval)"
    )
}

runner.test("default-socket-path: ends with Deskmate/ipc.sock") {
    let path = DefaultSocketPath.current()
    try runner.expect(path.hasSuffix("Deskmate/ipc.sock"), "unexpected path: \(path)")
}

// --- Phase 11d-iii: SnapshotHydrator + multi-sub onEnvelope -----------------

runner.test("snapshot-hydrator: domain_state field populates live store") {
    let store = LiveDomainStateStore()
    let hydrator = SnapshotHydrator(
        domainStore: store, callbackQueue: .global(qos: .userInitiated)
    )
    let env = BridgeEnvelope.of(
        .stateSnapshot,
        payload: [
            "domain_state": .object([
                "pending_approvals": .array([.string("ap-1")]),
                "agent_mood": .string("alert"),
            ])
        ]
    )
    hydrator.handle(env)
    try runner.expect(store.current.pendingApprovals == ["ap-1"], "pending wrong")
    try runner.expect(store.current.agentMood == .alert, "mood wrong")
}

runner.test("snapshot-hydrator: non-snapshot envelope is ignored") {
    let store = LiveDomainStateStore()
    let hydrator = SnapshotHydrator(
        domainStore: store, callbackQueue: .global(qos: .userInitiated)
    )
    let env = BridgeEnvelope.of(
        .intent,
        payload: ["kind": .string("update_domain_state")]
    )
    hydrator.handle(env)
    try runner.expect(store.current == DomainState(), "store should be untouched")
}

runner.test("snapshot-hydrator: onSnapshot fires with raw payload") {
    let store = LiveDomainStateStore()
    let hydrator = SnapshotHydrator(
        domainStore: store, callbackQueue: .global(qos: .userInitiated)
    )
    let sem = DispatchSemaphore(value: 0)
    var keys: [String] = []
    hydrator.onSnapshot { payload in
        keys = Array(payload.keys).sorted()
        sem.signal()
    }
    let env = BridgeEnvelope.of(
        .stateSnapshot,
        payload: [
            "domain_state": .object([:]),
            "active_sessions": .array([]),
            "pending_reminders": .array([]),
        ]
    )
    hydrator.handle(env)
    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "raw callback did not fire")
    try runner.expect(keys == ["active_sessions", "domain_state", "pending_reminders"],
                      "unexpected keys: \(keys)")
}

runner.test("bridge-client: onEnvelope supports multiple subscribers") {
    let (agentFd, clientFd) = BridgeClient.makeTestSocketPair()
    let client = BridgeClient(callbackQueue: .global(qos: .userInitiated))
    try client.start(preConnectedFd: clientFd)
    defer { client.stop(); close(agentFd) }

    let sem = DispatchSemaphore(value: 0)
    var aHits = 0
    var bHits = 0
    let lock = NSLock()
    client.onEnvelope { _ in
        lock.lock(); aHits += 1; lock.unlock()
        if aHits == 1 && bHits == 1 { sem.signal() }
    }
    client.onEnvelope { _ in
        lock.lock(); bHits += 1; lock.unlock()
        if aHits == 1 && bHits == 1 { sem.signal() }
    }
    let data = try EnvelopeFraming.encode(
        BridgeEnvelope.of(.ping, traceId: "t")
    )
    smokeWriteAll(fd: agentFd, data)
    try runner.expect(sem.wait(timeout: .now() + 2.0) == .success, "both subs did not fire")
    try runner.expect(aHits == 1 && bHits == 1,
                      "expected 1/1 hits, got \(aHits)/\(bHits)")
}

// --- Phase 9: DomainState degradation_level round-trip --------------------

runner.test("domain-state: degradation_level decodes and clamps") {
    let rawInRange = #"""
    {"degradation_level":3}
    """#.data(using: .utf8)!
    let decoded = try JSONDecoder().decode(DomainState.self, from: rawInRange)
    try runner.expect(decoded.degradationLevel == 3, "in-range value lost")

    let rawTooBig = #"""
    {"degradation_level":999}
    """#.data(using: .utf8)!
    let clamped = try JSONDecoder().decode(DomainState.self, from: rawTooBig)
    try runner.expect(clamped.degradationLevel == 6, "999 should clamp to 6")

    let rawNegative = #"""
    {"degradation_level":-5}
    """#.data(using: .utf8)!
    let floored = try JSONDecoder().decode(DomainState.self, from: rawNegative)
    try runner.expect(floored.degradationLevel == 0, "-5 should clamp to 0")
}

runner.test("domain-state: degradation_level absent defaults to 0") {
    let raw = #"{}"#.data(using: .utf8)!
    let decoded = try JSONDecoder().decode(DomainState.self, from: raw)
    try runner.expect(decoded.degradationLevel == 0, "missing field → 0")
}

runner.test("domain-state: degradation_level round-trips through encode") {
    let s = DomainState(degradationLevel: 4)
    let data = try JSONEncoder().encode(s)
    let back = try JSONDecoder().decode(DomainState.self, from: data)
    try runner.expect(back.degradationLevel == 4, "round-trip lost")
    // Wire uses snake_case.
    let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    try runner.expect(
        obj["degradation_level"] as? Int == 4,
        "wire key wrong: \(obj["degradation_level"] ?? "nil")"
    )
}

// --- Phase 12-iv: ChatHistoryBuffer ----------------------------------------

runner.test("chat-history: recordUserMessage trims and appends") {
    let buf = ChatHistoryBuffer()
    buf.recordUserMessage("  hi  ", at: 1)
    try runner.expect(buf.entries.count == 1, "expected 1 entry, got \(buf.entries.count)")
    try runner.expect(buf.entries[0].role == .user, "role wrong")
    try runner.expect(buf.entries[0].text == "hi", "text wrong: '\(buf.entries[0].text)'")
}

runner.test("chat-history: empty user message ignored") {
    let buf = ChatHistoryBuffer()
    buf.recordUserMessage("", at: 1)
    buf.recordUserMessage("   ", at: 2)
    try runner.expect(buf.entries.isEmpty, "whitespace-only entries recorded")
}

runner.test("chat-history: bubble chat-kind recorded as pet") {
    let buf = ChatHistoryBuffer()
    let b = BubbleSpec(id: "r1", kind: .chat, text: "hey")
    try runner.expect(buf.recordBubbleIfChatLike(b, at: 10), "not recorded")
    try runner.expect(buf.entries[0].role == .pet, "role wrong")
    try runner.expect(buf.entries[0].text == "hey", "text wrong")
}

runner.test("chat-history: bubble non-chat ignored") {
    let buf = ChatHistoryBuffer()
    let b = BubbleSpec(id: "a1", kind: .approvalHint, text: "grant?")
    try runner.expect(!buf.recordBubbleIfChatLike(b, at: 10), "approvalHint must be ignored")
    try runner.expect(buf.entries.isEmpty, "entries should be empty")
}

runner.test("chat-history: placeholder '…' ignored") {
    let buf = ChatHistoryBuffer()
    let b = BubbleSpec(id: "user-msg-ack", kind: .chat, text: "…")
    try runner.expect(!buf.recordBubbleIfChatLike(b, at: 10), "placeholder must be ignored")
    try runner.expect(buf.entries.isEmpty, "entries should be empty")
}

runner.test("chat-history: same bubble id only recorded once") {
    let buf = ChatHistoryBuffer()
    let b = BubbleSpec(id: "r1", kind: .chat, text: "hey")
    _ = buf.recordBubbleIfChatLike(b, at: 10)
    try runner.expect(!buf.recordBubbleIfChatLike(b, at: 11), "dup bubble recorded")
    try runner.expect(buf.entries.count == 1, "unexpected count")
}

runner.test("chat-history: maxEntries drops oldest") {
    let buf = ChatHistoryBuffer(maxEntries: 3)
    for i in 0..<5 {
        buf.recordUserMessage("m\(i)", at: i)
    }
    try runner.expect(buf.entries.count == 3, "expected 3 after trim")
    try runner.expect(buf.entries.map(\.text) == ["m2", "m3", "m4"], "oldest not dropped")
}

runner.test("chat-history: clear resets entries and dedup state") {
    let buf = ChatHistoryBuffer()
    buf.recordUserMessage("hi", at: 1)
    let b = BubbleSpec(id: "r1", kind: .chat, text: "hey")
    _ = buf.recordBubbleIfChatLike(b, at: 2)
    buf.clear()
    try runner.expect(buf.entries.isEmpty, "entries not cleared")
    try runner.expect(
        buf.recordBubbleIfChatLike(b, at: 3),
        "dedup state not cleared"
    )
}

// --- Phase 11d-ix: InteractionActionFactory --------------------------------

runner.test("factory: resolveApproval emits snake_case payload + menu_bar source") {
    let a = InteractionActionFactory.resolveApproval(id: "ap-1", allow: true)
    try runner.expect(a.source == .menuBar, "source wrong")
    try runner.expect(a.target == .system, "target wrong")
    try runner.expect(a.kind == .permissionResolve, "kind wrong")
    try runner.expect(a.payload["approval_id"] == .string("ap-1"), "approval_id wrong")
    try runner.expect(a.payload["allow"] == .bool(true), "allow wrong")
}

runner.test("factory: resolveApproval deny maps to allow=false") {
    let a = InteractionActionFactory.resolveApproval(id: "ap-1", allow: false)
    try runner.expect(a.payload["allow"] == .bool(false), "deny must be allow=false")
}

runner.test("factory: jumpToSession targets .session with session_id payload") {
    let a = InteractionActionFactory.jumpToSession(id: "s-7")
    try runner.expect(a.target == .session, "target wrong")
    try runner.expect(a.kind == .sessionJump, "kind wrong")
    try runner.expect(a.payload["session_id"] == .string("s-7"), "session_id wrong")
}

runner.test("factory: answerQuestion targets .session with answer payload") {
    let a = InteractionActionFactory.answerQuestion(
        sessionId: "s-7",
        answer: "Use Cursor",
        source: .island
    )
    try runner.expect(a.source == .island, "source wrong")
    try runner.expect(a.target == .session, "target wrong")
    try runner.expect(a.kind == .questionAnswer, "kind wrong")
    try runner.expect(a.payload["session_id"] == .string("s-7"), "session_id wrong")
    try runner.expect(a.payload["answer"] == .string("Use Cursor"), "answer wrong")
}

runner.test("factory: openTaskDetail targets .skill with task_id payload") {
    let a = InteractionActionFactory.openTaskDetail(id: "task-7")
    try runner.expect(a.source == .menuBar, "source wrong")
    try runner.expect(a.target == .skill, "target wrong")
    try runner.expect(a.kind == .taskOpenDetail, "kind wrong")
    try runner.expect(a.payload["task_id"] == .string("task-7"), "task_id wrong")
}

runner.test("factory: task control actions target .skill with task_id payload") {
    let cases: [(InteractionAction, InteractionKind)] = [
        (InteractionActionFactory.startTask(id: "task-7"), .taskStart),
        (InteractionActionFactory.pauseTask(id: "task-7"), .taskPause),
        (InteractionActionFactory.advanceTask(id: "task-7"), .taskAdvance),
        (InteractionActionFactory.completeTask(id: "task-7"), .taskComplete),
    ]
    for (action, kind) in cases {
        try runner.expect(action.source == .menuBar, "source wrong")
        try runner.expect(action.target == .skill, "target wrong")
        try runner.expect(action.kind == kind, "kind wrong")
        try runner.expect(action.payload["task_id"] == .string("task-7"), "task_id wrong")
    }
}

runner.test("factory: dismissSurface emits typed surface.dismiss action") {
    let a = InteractionActionFactory.dismissSurface(
        surface: .sessionList,
        source: .island
    )
    try runner.expect(a.source == .island, "source")
    try runner.expect(a.target == .system, "target")
    try runner.expect(a.kind == .surfaceDismiss, "kind")
    try runner.expect(a.payload["surface"] == .string("session_list"), "surface")
}

runner.test("factory: demoTrigger emits demo.trigger system action") {
    let a = InteractionActionFactory.demoTrigger(scenario: "codex_session")
    try runner.expect(a.source == .menuBar, "source")
    try runner.expect(a.target == .system, "target")
    try runner.expect(a.kind == .demoTrigger, "kind")
    try runner.expect(a.payload["scenario"] == .string("codex_session"), "scenario")
}

runner.test("factory: petInteract default gesture=click") {
    let a = InteractionActionFactory.petInteract()
    try runner.expect(a.source == .pet, "source wrong")
    try runner.expect(a.kind == .petInteract, "kind wrong")
    try runner.expect(a.payload["gesture"] == .string("click"), "gesture wrong")
}

runner.test("factory: bubbleAction injects bubble_id + keeps payload") {
    let bubble = BubbleAction(
        label: "Snooze",
        interactionKind: InteractionKind.surfaceDismiss.rawValue,
        payload: ["reason": .string("snooze"), "until_ms": .int(1_200)]
    )
    guard let a = InteractionActionFactory.bubbleAction(bubble, bubbleId: "bb-7")
    else {
        try runner.expect(false, "factory returned nil for known kind")
        return
    }
    try runner.expect(a.payload["bubble_id"] == .string("bb-7"), "bubble_id missing")
    try runner.expect(a.payload["reason"] == .string("snooze"), "reason dropped")
    try runner.expect(a.payload["until_ms"] == .int(1_200), "until_ms dropped")
}

runner.test("factory: bubbleAction returns nil for unknown kind") {
    let bubble = BubbleAction(
        label: "Unknown",
        interactionKind: "future.verb",
        payload: [:]
    )
    try runner.expect(
        InteractionActionFactory.bubbleAction(bubble, bubbleId: "bb-1") == nil,
        "unknown kinds must degrade to nil"
    )
}

runner.test("factory: resolveApproval serializes to canonical wire shape") {
    let a = InteractionActionFactory.resolveApproval(id: "ap-42", allow: true)
    let data = try JSONEncoder().encode(a)
    let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    try runner.expect(obj["source"] as? String == "menu_bar", "source wire")
    try runner.expect(obj["kind"] as? String == "permission.resolve", "kind wire")
    let p = obj["payload"] as! [String: Any]
    try runner.expect(p["approval_id"] as? String == "ap-42", "approval_id wire")
    try runner.expect(p["allow"] as? Bool == true, "allow wire")
}

// --- Phase 11d-iv: LiveList stores + snapshot hydration --------------------

runner.test("live-list-store: initial subscribe fires once with current") {
    let store = LiveListStore<Int>(initial: [7, 8])
    var seen: [Int] = []
    _ = store.subscribe { seen = $0 }
    try runner.expect(seen == [7, 8], "initial fire wrong: \(seen)")
}

runner.test("live-list-store: apply dedupes identical lists") {
    let store = LiveListStore<Int>()
    var hits = 0
    _ = store.subscribe { _ in hits += 1 }  // initial fire → 1
    try runner.expect(store.apply([1, 2]) == true, "first apply not accepted")
    try runner.expect(store.apply([1, 2]) == false, "dedup failed")
    try runner.expect(hits == 2, "expected 2 hits (initial + first apply), got \(hits)")
}

runner.test("snapshot-hydrator: populates session + reminder + approval + task stores") {
    let domain = LiveDomainStateStore()
    let sessions = LiveSessionListStore()
    let reminders = LivePendingRemindersStore()
    let approvals = LivePendingApprovalsStore()
    let tasks = LiveActiveTasksStore()
    let h = SnapshotHydrator(
        domainStore: domain,
        sessionStore: sessions,
        reminderStore: reminders,
        approvalStore: approvals,
        taskStore: tasks,
        callbackQueue: .global(qos: .userInitiated)
    )
    let env = BridgeEnvelope.of(
        .stateSnapshot,
        payload: [
            "domain_state": .object([:]),
            "active_sessions": .array([.object([
                "session_id": .string("s-1"),
                "title": .string("Design review"),
            ])]),
            "pending_reminders": .array([.object([
                "reminder_id": .string("r-1"),
                "text": .string("stand up"),
            ])]),
            "pending_approvals_detail": .array([.object([
                "approval_id": .string("ap-1"),
                "prompt": .string("allow clipboard?"),
            ])]),
            "active_tasks": .array([.object([
                "task_id": .string("task-1"),
                "title": .string("Polish task lane"),
                "completed_step_count": .int(4),
                "total_step_count": .int(9),
                "current_step": .object([
                    "step_id": .string("step-1"),
                    "content": .string("Expose task snapshot"),
                    "status": .string("in_progress"),
                    "active_form": .string("Exposing task snapshot"),
                ]),
                "steps": .array([.object([
                    "step_id": .string("step-1"),
                    "content": .string("Expose task snapshot"),
                    "status": .string("in_progress"),
                    "active_form": .string("Exposing task snapshot"),
                ])]),
            ])]),
        ]
    )
    h.handle(env)
    try runner.expect(sessions.current.map(\.sessionId) == ["s-1"],
                      "sessions: \(sessions.current.map(\.sessionId))")
    try runner.expect(reminders.current.map(\.reminderId) == ["r-1"],
                      "reminders wrong")
    try runner.expect(approvals.current.map(\.approvalId) == ["ap-1"],
                      "approvals wrong")
    try runner.expect(tasks.current.map(\.taskId) == ["task-1"],
                      "tasks wrong")
    try runner.expect(tasks.current.first?.currentStepLine == "step: Exposing task snapshot",
                      "task current step wrong")
    try runner.expect(tasks.current.first?.stepProgressLabel == "4/9 steps",
                      "task progress wrong")
}

runner.test("snapshot-hydrator: empty list field clears a store") {
    let store = LivePendingApprovalsStore()
    store.apply([ApprovalRow(approvalId: "stale")])
    let h = SnapshotHydrator(
        domainStore: LiveDomainStateStore(),
        approvalStore: store,
        callbackQueue: .global(qos: .userInitiated)
    )
    h.handle(BridgeEnvelope.of(
        .stateSnapshot,
        payload: [
            "domain_state": .object([:]),
            "pending_approvals_detail": .array([]),
        ]
    ))
    try runner.expect(store.current.isEmpty, "empty list must clear store")
}

runner.test("shell: state.snapshot hydrates all four list stores") {
    let harness = SmokeShellHarness()
    var config = DeskmateShell.Configuration(
        bridgeBackoff: .init(
            initialBackoff: 0.01, maxBackoff: 0.05,
            multiplier: 2.0, jitterFraction: 0
        )
    )
    config.clientFactory = harness.factory
    let shell = DeskmateShell(
        configuration: config,
        callbackQueue: .global(qos: .userInitiated)
    )
    defer { shell.stop(); for fd in harness.peerSnapshot where fd > 0 { close(fd) } }

    let connectedSem = DispatchSemaphore(value: 0)
    shell.bridge.onStateChange { s in if s == .connected { connectedSem.signal() } }
    shell.start()
    try runner.expect(connectedSem.wait(timeout: .now() + 2.0) == .success,
                      "bridge not connected")

    let sessionHit = DispatchSemaphore(value: 0)
    let reminderHit = DispatchSemaphore(value: 0)
    let approvalHit = DispatchSemaphore(value: 0)
    let taskHit = DispatchSemaphore(value: 0)
    let us1 = shell.sessionList.subscribe { rows in
        if rows.first?.sessionId == "s-42" { sessionHit.signal() }
    }
    let us2 = shell.pendingReminders.subscribe { rows in
        if rows.first?.reminderId == "r-42" { reminderHit.signal() }
    }
    let us3 = shell.pendingApprovals.subscribe { rows in
        if rows.first?.approvalId == "ap-42" { approvalHit.signal() }
    }
    let us4 = shell.activeTasks.subscribe { rows in
        if rows.first?.taskId == "task-42" { taskHit.signal() }
    }
    defer { us1(); us2(); us3(); us4() }

    let snap = BridgeEnvelope.of(
        .stateSnapshot,
        payload: [
            "domain_state": .object([:]),
            "active_sessions": .array([.object([
                "session_id": .string("s-42"),
            ])]),
            "pending_reminders": .array([.object([
                "reminder_id": .string("r-42"),
            ])]),
            "pending_approvals_detail": .array([.object([
                "approval_id": .string("ap-42"),
            ])]),
            "active_tasks": .array([.object([
                "task_id": .string("task-42"),
            ])]),
        ]
    )
    smokeWriteAll(fd: harness.peerSnapshot[0], try EnvelopeFraming.encode(snap))
    try runner.expect(sessionHit.wait(timeout: .now() + 2.0) == .success, "sessions")
    try runner.expect(reminderHit.wait(timeout: .now() + 2.0) == .success, "reminders")
    try runner.expect(approvalHit.wait(timeout: .now() + 2.0) == .success, "approvals")
    try runner.expect(taskHit.wait(timeout: .now() + 2.0) == .success, "tasks")
}

runner.test("shell: state.snapshot envelope hydrates domainState") {
    let harness = SmokeShellHarness()
    var config = DeskmateShell.Configuration(
        bridgeBackoff: .init(
            initialBackoff: 0.01, maxBackoff: 0.05,
            multiplier: 2.0, jitterFraction: 0
        )
    )
    config.clientFactory = harness.factory
    let shell = DeskmateShell(
        configuration: config,
        callbackQueue: .global(qos: .userInitiated)
    )
    defer { shell.stop(); for fd in harness.peerSnapshot where fd > 0 { close(fd) } }

    let connectedSem = DispatchSemaphore(value: 0)
    shell.bridge.onStateChange { s in if s == .connected { connectedSem.signal() } }
    shell.start()
    try runner.expect(connectedSem.wait(timeout: .now() + 2.0) == .success, "not connected")

    let hydrated = DispatchSemaphore(value: 0)
    let unsub = shell.domainState.subscribe { state in
        if state.pendingApprovals == ["ap-X"]
            && state.agentMood == .alert {
            hydrated.signal()
        }
    }
    defer { unsub() }

    let snapshot = BridgeEnvelope.of(
        .stateSnapshot,
        payload: [
            "domain_state": .object([
                "pending_approvals": .array([.string("ap-X")]),
                "agent_mood": .string("alert"),
            ])
        ]
    )
    smokeWriteAll(fd: harness.peerSnapshot[0], try EnvelopeFraming.encode(snapshot))
    try runner.expect(hydrated.wait(timeout: .now() + 2.0) == .success, "domainState not hydrated")
}

runner.test("shell: send(action:) lands on peer as interaction envelope") {
    let harness = SmokeShellHarness()
    var config = DeskmateShell.Configuration(
        bridgeBackoff: .init(
            initialBackoff: 0.01, maxBackoff: 0.05,
            multiplier: 2.0, jitterFraction: 0
        )
    )
    config.clientFactory = harness.factory
    let shell = DeskmateShell(
        configuration: config,
        callbackQueue: .global(qos: .userInitiated)
    )
    defer { shell.stop(); for fd in harness.peerSnapshot where fd > 0 { close(fd) } }

    let connectedSem = DispatchSemaphore(value: 0)
    shell.bridge.onStateChange { s in if s == .connected { connectedSem.signal() } }
    shell.start()
    try runner.expect(connectedSem.wait(timeout: .now() + 2.0) == .success, "shell did not connect")

    let action = InteractionAction(
        source: .pet, target: .bubble, kind: .permissionResolve,
        payload: ["approval_id": .string("ap-1"), "allow": .bool(true)]
    )
    try shell.send(action: action, traceId: "shell-trace")

    let bytes = smokeDrain(fd: harness.peerSnapshot[0])
    var framing = EnvelopeFraming()
    let env = framing.feedEnvelopes(bytes).first
    try runner.expect(env?.type == .interaction, "type wrong")
    try runner.expect(env?.traceId == "shell-trace", "trace wrong")
    try runner.expect(env?.payload["kind"] == .string("permission.resolve"), "kind wrong")
}

runner.test("reconnecting: peer close triggers fresh client and re-delivers envelopes") {
    let harness = SmokeReconnectHarness()
    harness.pushSuccess()
    harness.pushSuccess()
    let rc = ReconnectingBridgeClient(
        factory: harness.factory,
        configuration: .init(initialBackoff: 0.02, maxBackoff: 0.1, multiplier: 2, jitterFraction: 0),
        callbackQueue: .global(qos: .userInitiated)
    )
    defer { rc.stop(); for fd in harness.peerSnapshot where fd > 0 { close(fd) } }

    let connectedSem = DispatchSemaphore(value: 0)
    var connectedCount = 0
    rc.onStateChange { s in
        if s == .connected { connectedCount += 1; connectedSem.signal() }
    }
    var received: [String] = []
    let recvLock = NSLock()
    let env2Sem = DispatchSemaphore(value: 0)
    rc.onEnvelope { env in
        recvLock.lock(); received.append(env.traceId); recvLock.unlock()
        if env.traceId == "t2" { env2Sem.signal() }
    }
    rc.start()

    // First connection.
    try runner.expect(connectedSem.wait(timeout: .now() + 2.0) == .success, "first connect failed")

    // Close first peer to trigger reconnect.
    let peers1 = harness.peerSnapshot
    close(peers1[0])
    // The reconnection kicks off a second factory call; wait for it.
    try runner.expect(connectedSem.wait(timeout: .now() + 2.0) == .success, "no reconnect")
    try runner.expect(connectedCount == 2, "expected 2 connects, got \(connectedCount)")

    // Now send an envelope from the new peer.
    let peers2 = harness.peerSnapshot
    try runner.expect(peers2.count == 2, "should have a second peer fd")
    let envData = try EnvelopeFraming.encode(
        BridgeEnvelope.of(.ping, traceId: "t2")
    )
    smokeWriteAll(fd: peers2[1], envData)
    try runner.expect(env2Sem.wait(timeout: .now() + 2.0) == .success, "did not receive post-reconnect envelope")
    try runner.expect(received.contains("t2"), "envelope lost across reconnect")
}

// --- Phase 7: AvatarRenderer -------------------------------------------------

runner.test("avatar: unknown style falls back to pixel") {
    let spec = AvatarRenderer.resolve(style: "sprite3d", mood: .idle)
    try runner.expect(spec.style == .pixel, "unknown style should become .pixel")
    try runner.expect(spec.emoji.isEmpty == false, "emoji fallback must be populated")
}

runner.test("avatar: emoji style decodes from manifest value") {
    let spec = AvatarRenderer.resolve(
        style: "emoji", mood: .happy, emotion: "cheerful", attentionLevel: 0.4
    )
    try runner.expect(spec.style == .emoji, "style should be .emoji")
    try runner.expect(spec.emoji == "🎉", "cheerful emotion should win over mood")
    try runner.expect(abs(spec.glow - 0.4) < 0.0001, "glow should mirror attentionLevel")
}

runner.test("avatar: mood drives palette deterministically") {
    let a = AvatarRenderer.resolve(style: "pixel", mood: .working)
    let b = AvatarRenderer.resolve(style: "pixel", mood: .working)
    try runner.expect(a.primary == b.primary, "palette should be deterministic")
    try runner.expect(a.aura == a.primary, "aura should track primary")
}

runner.test("avatar: pixel mask is 8x8 bounded") {
    let mask = AvatarRenderer.pixelMask()
    try runner.expect(mask.count == 8, "mask height != 8")
    for row in mask {
        try runner.expect(row.count == 8, "mask row width != 8")
        for cell in row {
            try runner.expect((0...2).contains(cell), "cell out of 0...2")
        }
    }
}

runner.test("avatar: glow clamps out-of-range attention") {
    let over = AvatarRenderer.resolve(style: "pixel", mood: .alert, attentionLevel: 3.0)
    try runner.expect(abs(over.glow - 1.0) < 0.0001, "glow should clamp to 1.0")
    let under = AvatarRenderer.resolve(style: "pixel", mood: .alert, attentionLevel: -1.0)
    try runner.expect(abs(under.glow - 0.0) < 0.0001, "glow should clamp to 0.0")
}

// --- Phase 8: CharacterPackRegistry -----------------------------------------

func makePackOnDisk(
    id: String, in root: URL, displayName: String? = nil,
    stateNames: [String] = ["idle", "working", "thinking", "alert"],
    requiredStates: [String]? = nil,
    avatarStyle: String = "pixel"
) throws -> URL {
    // ``stateNames`` controls which state folders + frame files get
    // written. ``requiredStates`` (if passed) is what lands in the
    // manifest's ``required_states`` field — letting a caller build
    // a deliberately-broken pack where required > states.
    let packDir = root.appendingPathComponent(id)
    try FileManager.default.createDirectory(
        at: packDir, withIntermediateDirectories: true
    )
    var states: [String: [String: Any]] = [:]
    for state in stateNames {
        let frameName = "\(state)/000.png"
        let frameURL = packDir.appendingPathComponent(frameName)
        try FileManager.default.createDirectory(
            at: frameURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        FileManager.default.createFile(
            atPath: frameURL.path, contents: Data()
        )
        states[state] = ["fps": 4, "frames": [frameName]]
    }
    let manifest: [String: Any] = [
        "spec_version": 1,
        "id": id,
        "display_name": displayName ?? id,
        "avatar": [
            "default_style": avatarStyle,
            "supported_styles": ["pixel", "emoji"],
        ],
        "states": states,
        "required_states": requiredStates
            ?? ["idle", "working", "thinking", "alert"],
    ]
    let data = try JSONSerialization.data(withJSONObject: manifest, options: [])
    try data.write(to: packDir.appendingPathComponent("manifest.json"))
    return packDir
}

func makePacksRoot() throws -> URL {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("deskmate-packs-\(UUID().uuidString)")
    try FileManager.default.createDirectory(
        at: root, withIntermediateDirectories: true
    )
    return root
}

runner.test("pack_registry: discover empty root is empty result") {
    let missing = FileManager.default.temporaryDirectory
        .appendingPathComponent("nope-\(UUID().uuidString)")
    let result = CharacterPackDiscovery.discoverPacks(in: missing)
    try runner.expect(result.packs.isEmpty, "packs should be empty")
    try runner.expect(result.skipped.isEmpty, "skipped should be empty")
}

runner.test("pack_registry: discover loads every valid pack sorted") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    for id in ["zebra", "mango", "apple"] {
        _ = try makePackOnDisk(id: id, in: root)
    }
    let result = CharacterPackDiscovery.discoverPacks(in: root)
    let ids = result.packs.map { $0.manifest.id }
    try runner.expect(ids == ["apple", "mango", "zebra"], "ids: \(ids)")
    try runner.expect(result.skipped.isEmpty, "no skips expected")
}

runner.test("pack_registry: discover skips broken packs with reason") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    _ = try makePackOnDisk(id: "ok", in: root)
    // Broken pack — manifest requires the full set but only 'idle' is present.
    _ = try makePackOnDisk(
        id: "broken", in: root,
        stateNames: ["idle"],
        requiredStates: ["idle", "working", "thinking", "alert"]
    )
    // Directory without manifest.
    try FileManager.default.createDirectory(
        at: root.appendingPathComponent("empty"),
        withIntermediateDirectories: true
    )
    let result = CharacterPackDiscovery.discoverPacks(in: root)
    let ids = result.packs.map { $0.manifest.id }
    try runner.expect(ids == ["ok"], "only 'ok' should survive: \(ids)")
    try runner.expect(
        result.skipped.contains(where: { $0.key.hasSuffix("empty") }),
        "empty dir should be skipped"
    )
}

runner.test("pack_registry: selectActivePack prefers explicit id") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    _ = try makePackOnDisk(id: CharacterPackEnv.builtinPackId, in: root)
    _ = try makePackOnDisk(id: "custom", in: root)
    let registry = CharacterPackRegistry(
        CharacterPackDiscovery.discoverPacks(in: root).packs
    )
    let picked = registry.selectActivePack(preferred: "custom")
    try runner.expect(picked?.manifest.id == "custom", "preferred ignored")
}

runner.test("pack_registry: selectActivePack falls back to builtin") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    _ = try makePackOnDisk(id: "extra", in: root)
    _ = try makePackOnDisk(id: CharacterPackEnv.builtinPackId, in: root)
    let registry = CharacterPackRegistry(
        CharacterPackDiscovery.discoverPacks(in: root).packs
    )
    let picked = registry.selectActivePack(preferred: "ghost")
    try runner.expect(
        picked?.manifest.id == CharacterPackEnv.builtinPackId,
        "should fall back to builtin, got \(String(describing: picked?.manifest.id))"
    )
}

runner.test("pack_registry: selectActivePack falls back to legacy pixel pack") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    _ = try makePackOnDisk(id: "extra", in: root)
    _ = try makePackOnDisk(id: CharacterPackEnv.legacyPixelPackId, in: root)
    let registry = CharacterPackRegistry(
        CharacterPackDiscovery.discoverPacks(in: root).packs
    )
    let picked = registry.selectActivePack(preferred: "ghost")
    try runner.expect(
        picked?.manifest.id == CharacterPackEnv.legacyPixelPackId,
        "should fall back to legacy pixel pack, got \(String(describing: picked?.manifest.id))"
    )
}

runner.test("pack_registry: selectActivePack returns nil when empty") {
    let registry = CharacterPackRegistry()
    try runner.expect(registry.selectActivePack() == nil, "empty should be nil")
}

runner.test("pack_registry: resolveActivePack honours env override") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    _ = try makePackOnDisk(id: "a", in: root)
    _ = try makePackOnDisk(id: "b", in: root)
    let registry = CharacterPackRegistry(
        CharacterPackDiscovery.discoverPacks(in: root).packs
    )
    let env = [CharacterPackEnv.activePackVar: "b"]
    let picked = CharacterPackActivation.resolveActivePack(
        in: registry, environment: env
    )
    try runner.expect(
        picked?.manifest.id == "b",
        "env override ignored, got \(String(describing: picked?.manifest.id))"
    )
}

runner.test("pack_registry: preferred arg beats env var") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    _ = try makePackOnDisk(id: "a", in: root)
    _ = try makePackOnDisk(id: "b", in: root)
    let registry = CharacterPackRegistry(
        CharacterPackDiscovery.discoverPacks(in: root).packs
    )
    let env = [CharacterPackEnv.activePackVar: "b"]
    let picked = CharacterPackActivation.resolveActivePack(
        in: registry, preferred: "a", environment: env
    )
    try runner.expect(picked?.manifest.id == "a", "preferred should win")
}

runner.test("pack_registry: resolveAvatarStyle uses active manifest first") {
    let root = try makePacksRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    _ = try makePackOnDisk(
        id: "custom",
        in: root,
        avatarStyle: "emoji"
    )
    let registry = CharacterPackRegistry(
        CharacterPackDiscovery.discoverPacks(in: root).packs
    )
    let style = CharacterPackActivation.resolveAvatarStyle(
        in: registry,
        environment: [
            CharacterPackEnv.activePackVar: "custom",
            CharacterPackEnv.avatarStyleVar: "pixel",
        ]
    )
    try runner.expect(style == "emoji", "manifest style should beat legacy env")
}

runner.test("pack_registry: resolveAvatarStyle falls back to legacy env") {
    let registry = CharacterPackRegistry()
    let style = CharacterPackActivation.resolveAvatarStyle(
        in: registry,
        environment: [CharacterPackEnv.avatarStyleVar: "emoji"]
    )
    try runner.expect(style == "emoji", "legacy style fallback")
}

runner.test("pack_registry: buildDefaultRegistry reads extra roots") {
    let primary = try makePacksRoot()
    let extra = try makePacksRoot()
    defer {
        try? FileManager.default.removeItem(at: primary)
        try? FileManager.default.removeItem(at: extra)
    }
    _ = try makePackOnDisk(id: "one", in: primary)
    _ = try makePackOnDisk(id: "two", in: extra)
    let env = [CharacterPackEnv.packsDirVar: primary.path]
    let registry = CharacterPackActivation.buildDefaultRegistry(
        extraRoots: [extra], environment: env
    )
    let ids = Set(registry.ids)
    try runner.expect(ids == Set(["one", "two"]), "unexpected ids: \(ids)")
}

// --- V10 Phase 9 · §4: DegradationPolicy ----------------------------------

runner.test("degradation: level 0 is the no-op default") {
    let p = DegradationPolicy()
    try runner.expect(p.level == 0, "default level should be 0, got \(p.level)")
    try runner.expect(p.isDegraded == false, "level 0 must not be marked degraded")
    try runner.expect(p.hideHUD == false, "level 0 must keep HUD visible")
    try runner.expect(p.islandOrderOut == false, "level 0 must keep island visible")
    try runner.expect(p.cameraOff == false, "level 0 must keep camera on")
    try runner.expect(p.effectiveFPS(base: 12) == 12, "level 0 must keep base fps")
}

runner.test("degradation: level 1 halves base fps but keeps HUD/island/camera") {
    let p = DegradationPolicy(level: 1)
    try runner.expect(p.effectiveFPS(base: 12) == 6, "12 fps → 6 fps at level 1")
    try runner.expect(p.effectiveFPS(base: 24) == 12, "24 fps → 12 fps at level 1")
    try runner.expect(p.effectiveFPS(base: 1) == 1, "fps must clamp at 1, got \(p.effectiveFPS(base: 1))")
    try runner.expect(p.hideHUD == false, "level 1 must not hide HUD yet")
    try runner.expect(p.islandOrderOut == false, "level 1 must not orderOut island yet")
}

runner.test("degradation: level 4 hides HUD, island still visible") {
    let p = DegradationPolicy(level: 4)
    try runner.expect(p.hideHUD == true, "level 4 must hide HUD")
    try runner.expect(p.islandOrderOut == false, "level 4 must keep island visible")
    try runner.expect(p.cameraOff == false, "level 4 must keep camera on")
}

runner.test("degradation: level 5 hides HUD AND orderOut island") {
    let p = DegradationPolicy(level: 5)
    try runner.expect(p.hideHUD == true, "level 5 must hide HUD (monotonic)")
    try runner.expect(p.islandOrderOut == true, "level 5 must orderOut island")
    try runner.expect(p.cameraOff == false, "level 5 must keep camera on")
}

runner.test("degradation: level 6 disables camera AND keeps prior steps") {
    let p = DegradationPolicy(level: 6)
    try runner.expect(p.cameraOff == true, "level 6 must disable camera")
    try runner.expect(p.islandOrderOut == true, "level 6 must orderOut island (monotonic)")
    try runner.expect(p.hideHUD == true, "level 6 must hide HUD (monotonic)")
    try runner.expect(p.effectiveFPS(base: 12) == 6, "level 6 must keep step-1 FPS down (monotonic)")
}

runner.test("degradation: out-of-range levels clamp to 0...6") {
    let lo = DegradationPolicy(level: -3)
    try runner.expect(lo.level == 0, "negative level should clamp to 0, got \(lo.level)")
    let hi = DegradationPolicy(level: 99)
    try runner.expect(hi.level == 6, "huge level should clamp to 6, got \(hi.level)")
    try runner.expect(hi.cameraOff == true, "clamped 99 should still be cameraOff")
}

runner.test("degradation: equality follows the clamped level") {
    let a = DegradationPolicy(level: 5)
    let b = DegradationPolicy(level: 5)
    let c = DegradationPolicy(level: 99) // clamps to 6
    try runner.expect(a == b, "same level must be equal")
    try runner.expect(a != c, "level 5 and clamped 99 (=6) must differ")
}

runner.test("degradation: apply(to:) is identity at level 0") {
    let base = PetFrameAnimator(fps: 12, frameCount: 4, loops: true)
    let normal = DegradationPolicy(level: 0).apply(to: base)
    try runner.expect(normal == base, "level 0 must return the same animator")
    try runner.expect(normal.fps == 12, "level 0 must preserve fps")
}

runner.test("degradation: apply(to:) halves fps at level 1+ and preserves frames/loops") {
    let base = PetFrameAnimator(fps: 12, frameCount: 4, loops: false)
    let degraded = DegradationPolicy(level: 1).apply(to: base)
    try runner.expect(degraded.fps == 6, "12 fps base → 6 fps when degraded, got \(degraded.fps)")
    try runner.expect(degraded.frameCount == 4, "frame count must be preserved")
    try runner.expect(degraded.loops == false, "loop policy must be preserved")
    try runner.expect(degraded.frameDurationMs == 166, "duration must reflect new fps, got \(degraded.frameDurationMs)")
}

runner.test("degradation: apply(to:) clamps animator fps to 1 minimum") {
    let base = PetFrameAnimator(fps: 1, frameCount: 2, loops: true)
    let degraded = DegradationPolicy(level: 5).apply(to: base)
    try runner.expect(degraded.fps == 1, "minimum fps must clamp to 1, got \(degraded.fps)")
}

runner.test("degradation: derives correctly from DomainState.degradationLevel") {
    // End-to-end: a domain snapshot with a non-zero degradation level
    // should yield a policy that the UI can read directly.
    let raw = """
    {
      "spec_version": 1,
      "current_priority": "P3",
      "user_focus": "casual",
      "agent_mood": "idle",
      "pending_approvals": [],
      "coding_today_ms": 0,
      "coding_today_by_ide": {},
      "degradation_level": 5
    }
    """.data(using: .utf8)!
    let ds = try JSONDecoder().decode(DomainState.self, from: raw)
    let policy = DegradationPolicy(level: ds.degradationLevel)
    try runner.expect(policy.islandOrderOut, "level 5 round-trip must be islandOrderOut")
    try runner.expect(policy.hideHUD, "level 5 round-trip must be hideHUD")
}

runner.test("degradation: combines domain and local power levels by max") {
    let normal = DegradationPolicy.combined(domainLevel: 0, localLevel: 0)
    try runner.expect(normal.level == 0, "normal levels should stay 0")
    let power = DegradationPolicy.combined(domainLevel: 0, localLevel: 1)
    try runner.expect(power.level == 1, "local power level should raise policy")
    let domain = DegradationPolicy.combined(domainLevel: 5, localLevel: 1)
    try runner.expect(domain.level == 5, "domain level should win when higher")
}

runner.test("degradation: power trigger maps low power or low battery to fps down") {
    let trigger = PowerDegradationTrigger(lowBatteryThreshold: 0.25)
    try runner.expect(
        trigger.level(isLowPowerModeEnabled: true, batteryFraction: 0.9) == 1,
        "low power mode should force FPS-down"
    )
    try runner.expect(
        trigger.level(isLowPowerModeEnabled: false, batteryFraction: 0.2) == 1,
        "low battery should force FPS-down"
    )
    try runner.expect(
        trigger.level(isLowPowerModeEnabled: false, batteryFraction: 0.8) == 0,
        "healthy battery should keep normal level"
    )
}

// --- PerfMetricsCollector (V10 §3.1 row 6 + row 8) -------------------------

runner.test("perf metrics: first frame without wake records nothing") {
    let c = PerfMetricsCollector()
    c.markFirstFrame(at: 100.0)
    try runner.expect(c.snapshot().lastWakeSeconds == nil, "no wake → nil wake_s")
    try runner.expect(!c.isAwaitingFirstFrame, "no wake should leave nothing pending")
}

runner.test("perf metrics: wake then first frame latches elapsed seconds") {
    let c = PerfMetricsCollector()
    c.recordWake(at: 100.0)
    try runner.expect(c.isAwaitingFirstFrame, "wake should mark pending")
    c.markFirstFrame(at: 100.42)
    try runner.expect(
        abs((c.snapshot().lastWakeSeconds ?? -1) - 0.42) < 1e-9,
        "expected ~0.42s wake_s"
    )
    try runner.expect(!c.isAwaitingFirstFrame, "first frame should clear pending")
}

runner.test("perf metrics: repeated wakes collapse to latest before frame") {
    let c = PerfMetricsCollector()
    c.recordWake(at: 50.0)
    c.recordWake(at: 60.0)
    c.markFirstFrame(at: 60.10)
    try runner.expect(
        abs((c.snapshot().lastWakeSeconds ?? -1) - 0.10) < 1e-9,
        "latest wake should win"
    )
}

runner.test("perf metrics: frame latch ignores frames after pending cleared") {
    let c = PerfMetricsCollector()
    c.recordWake(at: 0)
    c.markFirstFrame(at: 0.3)
    c.markFirstFrame(at: 5.0)  // no pending → must not overwrite
    try runner.expect(
        abs((c.snapshot().lastWakeSeconds ?? -1) - 0.3) < 1e-9,
        "subsequent frame without wake must not change wake_s"
    )
}

runner.test("perf metrics: negative wake interval clamps to zero") {
    let c = PerfMetricsCollector()
    c.recordWake(at: 100.0)
    c.markFirstFrame(at: 99.9)
    try runner.expect(c.snapshot().lastWakeSeconds == 0.0, "negative skew clamps to 0")
}

runner.test("perf metrics: first tick does not count toward total") {
    let c = PerfMetricsCollector()
    c.recordFrameTick(at: 0, expectedPeriod: 1.0/60.0)
    try runner.expect(c.snapshot().totalFrames == 0, "first tick has no baseline")
    try runner.expect(c.snapshot().droppedFrames == 0, "no drops yet")
}

runner.test("perf metrics: on-time ticks are not dropped") {
    let c = PerfMetricsCollector()
    let period = 1.0/60.0
    var t = 0.0
    for _ in 0..<60 {
        c.recordFrameTick(at: t, expectedPeriod: period)
        t += period
    }
    let snap = c.snapshot()
    try runner.expect(snap.totalFrames == 59, "59 intervals between 60 ticks")
    try runner.expect(snap.droppedFrames == 0, "exact period must not drop")
    try runner.expect(snap.frameDropPct == 0, "ratio % must be zero")
}

runner.test("perf metrics: long interval counts as one drop") {
    let c = PerfMetricsCollector(dropTolerance: 1.5)
    let period = 1.0/60.0
    c.recordFrameTick(at: 0, expectedPeriod: period)
    c.recordFrameTick(at: period * 2.0, expectedPeriod: period)
    let snap = c.snapshot()
    try runner.expect(snap.droppedFrames == 1, "2× period → 1 drop")
    try runner.expect(snap.totalFrames == 1, "one interval observed")
}

runner.test("perf metrics: drop ratio is bounded to [0,1]") {
    let c = PerfMetricsCollector(dropTolerance: 1.5)
    let period = 1.0/60.0
    c.recordFrameTick(at: 0, expectedPeriod: period)
    for i in 1...3 {
        c.recordFrameTick(at: Double(i) * period * 2.0, expectedPeriod: period)
    }
    let snap = c.snapshot()
    try runner.expect(snap.frameDropRatio == 1.0, "all intervals long → ratio 1")
    try runner.expect(snap.frameDropPct == 100.0, "100% in pct")
}

runner.test("perf metrics: zero / non-monotonic ticks are ignored") {
    let c = PerfMetricsCollector()
    c.recordFrameTick(at: 0, expectedPeriod: 0)
    c.recordFrameTick(at: 1.0, expectedPeriod: 0)
    try runner.expect(c.snapshot().totalFrames == 0, "zero period must not count")
    let c2 = PerfMetricsCollector()
    let period = 1.0/60.0
    c2.recordFrameTick(at: 1.0, expectedPeriod: period)
    c2.recordFrameTick(at: 0.5, expectedPeriod: period)  // backwards
    try runner.expect(c2.snapshot().totalFrames == 0, "backwards tick must not count")
}

runner.test("perf metrics: resetFrameStats keeps wake but clears counters") {
    let c = PerfMetricsCollector()
    c.recordWake(at: 0)
    c.markFirstFrame(at: 0.2)
    c.recordFrameTick(at: 0, expectedPeriod: 1.0/60.0)
    c.recordFrameTick(at: 1.0, expectedPeriod: 1.0/60.0)
    c.resetFrameStats()
    let snap = c.snapshot()
    try runner.expect(snap.totalFrames == 0, "reset clears total")
    try runner.expect(snap.droppedFrames == 0, "reset clears dropped")
    try runner.expect(
        abs((snap.lastWakeSeconds ?? -1) - 0.2) < 1e-9,
        "reset must preserve wake_s"
    )
}

runner.test("perf metrics: snapshot round-trips through JSON") {
    let original = PerfMetricsSnapshot(
        lastWakeSeconds: 0.42,
        totalFrames: 100,
        droppedFrames: 3,
        frameDropRatio: 0.03
    )
    let data = try JSONEncoder().encode(original)
    let decoded = try JSONDecoder().decode(PerfMetricsSnapshot.self, from: data)
    try runner.expect(decoded == original, "round trip must preserve all fields")
    try runner.expect(
        abs(decoded.frameDropPct - 3.0) < 1e-9,
        "frameDropPct must derive from ratio"
    )
}

// --- PerfMetricsBinding (NotificationCenter + envelope push wiring) -------

final class RecordingEnvelopeSender: EnvelopeSender {
    var sent: [BridgeEnvelope] = []
    func send(_ envelope: BridgeEnvelope) throws {
        sent.append(envelope)
    }
}

runner.test("perf binding: pushSnapshot encodes a perfMetrics envelope") {
    let sender = RecordingEnvelopeSender()
    var clockNow: TimeInterval = 0
    let binding = PerfMetricsBinding(
        clock: { clockNow },
        sender: { sender }
    )
    binding.collector.recordWake(at: 0)
    clockNow = 0.30
    binding.markFirstFrame()
    binding.recordFrameTick(at: 0, expectedPeriod: 1.0/60.0)
    binding.recordFrameTick(at: 1.0/60.0, expectedPeriod: 1.0/60.0)
    binding.pushSnapshot()

    try runner.expect(sender.sent.count == 1, "expected one envelope")
    let env = sender.sent[0]
    try runner.expect(env.type == .perfMetrics, "type must be perf.metrics")
    let wake = env.payload["last_wake_seconds"]
    if case .double(let w) = wake {
        try runner.expect(abs(w - 0.30) < 1e-9, "wake_s payload mismatch")
    } else {
        throw SmokeError.expectation("last_wake_seconds wasn't a double")
    }
    if case .int(let total) = env.payload["total_frames"] {
        try runner.expect(total == 1, "expected total_frames == 1")
    } else {
        throw SmokeError.expectation("total_frames wasn't an int")
    }
    if case .double(let ratio) = env.payload["frame_drop_ratio"] {
        try runner.expect(ratio == 0.0, "expected ratio 0")
    } else {
        throw SmokeError.expectation("frame_drop_ratio wasn't a double")
    }
}

runner.test("perf binding: wake notification recorded on the binding's queue") {
    let center = NotificationCenter()
    let wakeName = Notification.Name("DeskmateSmokeWake")
    var clockNow: TimeInterval = 100
    let sender = RecordingEnvelopeSender()
    let binding = PerfMetricsBinding(
        center: center,
        wakeNotificationName: wakeName,
        clock: { clockNow },
        sender: { sender }
    )
    binding.start(pushInterval: 0)  // observer only; no timer
    clockNow = 100
    center.post(name: wakeName, object: nil)
    clockNow = 100.42
    binding.markFirstFrame()
    binding.pushSnapshot()
    binding.stop()

    try runner.expect(sender.sent.count == 1, "expected one envelope")
    let env = sender.sent[0]
    if case .double(let w) = env.payload["last_wake_seconds"] {
        try runner.expect(
            abs(w - 0.42) < 1e-9,
            "wake_s should reflect post→markFirstFrame elapsed"
        )
    } else {
        throw SmokeError.expectation("last_wake_seconds wasn't a double")
    }
}

runner.test("perf binding: stop removes the wake observer") {
    let center = NotificationCenter()
    let wakeName = Notification.Name("DeskmateSmokeWakeStop")
    var clockNow: TimeInterval = 0
    let sender = RecordingEnvelopeSender()
    let binding = PerfMetricsBinding(
        center: center,
        wakeNotificationName: wakeName,
        clock: { clockNow },
        sender: { sender }
    )
    binding.start(pushInterval: 0)
    binding.stop()
    clockNow = 1.0
    center.post(name: wakeName, object: nil)
    clockNow = 1.5
    binding.markFirstFrame()
    binding.pushSnapshot()

    try runner.expect(sender.sent.count == 1, "envelope still sent on demand")
    let env = sender.sent[0]
    // No wake was observed (we stopped first) → wake_s should be null.
    if case .null = env.payload["last_wake_seconds"] {
        // ok
    } else {
        throw SmokeError.expectation(
            "expected null last_wake_seconds after stop()"
        )
    }
}

runner.test("perf binding: pushSnapshot with no wake encodes null") {
    let sender = RecordingEnvelopeSender()
    let binding = PerfMetricsBinding(
        clock: { 0 },
        sender: { sender }
    )
    binding.pushSnapshot()
    try runner.expect(sender.sent.count == 1, "expected one envelope")
    let env = sender.sent[0]
    if case .null = env.payload["last_wake_seconds"] {
        // ok
    } else {
        throw SmokeError.expectation(
            "expected null last_wake_seconds when no wake recorded"
        )
    }
}

runner.test("perf binding: missing sender silently skips pushSnapshot") {
    var senderRef: EnvelopeSender? = nil
    let binding = PerfMetricsBinding(
        clock: { 0 },
        sender: { senderRef }
    )
    binding.pushSnapshot()  // must not throw or crash
    let sender = RecordingEnvelopeSender()
    senderRef = sender
    binding.pushSnapshot()
    try runner.expect(sender.sent.count == 1, "envelope only after sender attached")
}

// --- FrameTickerSource (CVDisplayLink injection point) --------------------

final class ManualFrameTicker: FrameTickerSource {
    private(set) var started = false
    private(set) var stopped = false
    private var handler: ((TimeInterval, TimeInterval) -> Void)?

    func start(onTick: @escaping (TimeInterval, TimeInterval) -> Void) {
        started = true
        stopped = false
        handler = onTick
    }

    func stop() {
        stopped = true
        handler = nil
    }

    /// Drive a synthetic tick. Production code calls this from a
    /// CVDisplayLink output handler; the test fires it directly.
    func tick(at timestamp: TimeInterval, expectedPeriod: TimeInterval) {
        handler?(timestamp, expectedPeriod)
    }
}

runner.test("perf binding: frame ticker drives drop counter through the queue") {
    let sender = RecordingEnvelopeSender()
    let ticker = ManualFrameTicker()
    let binding = PerfMetricsBinding(
        clock: { 0 },
        sender: { sender },
        frameTickerSource: ticker
    )
    binding.start(pushInterval: 0)
    try runner.expect(ticker.started, "binding.start should start the ticker")

    let period = 1.0 / 60.0
    ticker.tick(at: 0, expectedPeriod: period)
    ticker.tick(at: period, expectedPeriod: period)            // on time
    ticker.tick(at: period * 4.0, expectedPeriod: period)      // long gap → drop

    // Allow the binding's serial queue to drain before snapshot.
    binding.pushSnapshot()
    let env = sender.sent[0]
    if case .int(let total) = env.payload["total_frames"] {
        try runner.expect(total == 2, "expected total_frames == 2")
    } else {
        throw SmokeError.expectation("total_frames wasn't an int")
    }
    if case .int(let dropped) = env.payload["dropped_frames"] {
        try runner.expect(dropped == 1, "expected dropped_frames == 1")
    } else {
        throw SmokeError.expectation("dropped_frames wasn't an int")
    }
    if case .double(let ratio) = env.payload["frame_drop_ratio"] {
        try runner.expect(ratio == 0.5, "expected ratio 0.5")
    } else {
        throw SmokeError.expectation("frame_drop_ratio wasn't a double")
    }

    binding.stop()
    try runner.expect(ticker.stopped, "binding.stop should stop the ticker")
}

runner.test("perf binding: frame ticker stop cuts off late ticks") {
    let sender = RecordingEnvelopeSender()
    let ticker = ManualFrameTicker()
    let binding = PerfMetricsBinding(
        clock: { 0 },
        sender: { sender },
        frameTickerSource: ticker
    )
    binding.start(pushInterval: 0)
    binding.stop()

    // After stop the ticker's onTick is nil; firing tick must not
    // reach the collector.
    ticker.tick(at: 0, expectedPeriod: 1.0/60.0)
    ticker.tick(at: 1.0, expectedPeriod: 1.0/60.0)

    binding.pushSnapshot()
    let env = sender.sent[0]
    if case .int(let total) = env.payload["total_frames"] {
        try runner.expect(total == 0, "ticks after stop must not count")
    } else {
        throw SmokeError.expectation("total_frames wasn't an int")
    }
}

// --- island-polish-enhancements smoke ---

runner.test("PhaseColorTable: resolve every phase without crashing") {
    for phase in SessionRow.Phase.allCases {
        let triple = PhaseColorTable.resolve(phase, scheme: .dark)
        // Verify we get a valid triple (foreground/stroke may be equal for running/unknown)
        try runner.expect(
            triple.foreground != triple.stroke || phase == .running || phase == .unknown,
            "PhaseColorTable returned identical foreground/stroke for \(phase)"
        )
    }
}

runner.test("PhaseColorTable: waiting urgency escalates at 30s and 60s") {
    let now = 100_000
    try runner.expect(
        PhaseColorTable.urgency(
            phase: .waitingForApproval,
            createdAtMs: now - PhaseColorTable.unattendedThresholdMs + 1,
            nowMs: now
        ) == .normal,
        "approval should be normal just before unattended threshold"
    )
    try runner.expect(
        PhaseColorTable.urgency(
            phase: .waitingForApproval,
            createdAtMs: now - PhaseColorTable.unattendedThresholdMs,
            nowMs: now
        ) == .unattended,
        "approval should become unattended at threshold"
    )
    try runner.expect(
        PhaseColorTable.urgency(
            phase: .waitingForAnswer,
            createdAtMs: now - PhaseColorTable.overdueThresholdMs,
            nowMs: now
        ) == .overdue,
        "question should become overdue at threshold"
    )
    try runner.expect(
        PhaseColorTable.urgency(
            phase: .runningTool,
            createdAtMs: now - PhaseColorTable.overdueThresholdMs * 2,
            nowMs: now
        ) == .normal,
        "non-waiting phase should not escalate"
    )
}

runner.test("IslandInteractionGeometry: collapsed hit band only covers top strip") {
    let bounds = CGRect(x: 0, y: 0, width: 548, height: 396)
    let band = IslandInteractionGeometry.collapsedHitBandRect(in: bounds)
    try runner.expect(band.minX == bounds.minX, "hit band minX mismatch")
    try runner.expect(band.maxY == bounds.maxY, "hit band should be pinned to top")
    try runner.expect(
        band.height == IslandInteractionGeometry.collapsedHitBandHeight,
        "hit band height mismatch"
    )
    try runner.expect(
        band.contains(CGPoint(x: bounds.midX, y: bounds.maxY - 4)),
        "top point should hit"
    )
    try runner.expect(
        !band.contains(CGPoint(x: bounds.midX, y: bounds.minY + 20)),
        "lower transparent area should pass through"
    )
}

runner.test("IslandInteractionGeometry: expanded passthrough only outside surface") {
    let geometry = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: CGRect(x: 0, y: 0, width: 1512, height: 982),
        notchSize: CGSize(width: 224, height: 28),
        hasPhysicalNotch: true,
        hasCompactPresence: true,
        isExpanded: true,
        activeCount: 3
    ))
    let inside = CGPoint(
        x: geometry.surfaceRectInPanel.midX,
        y: geometry.surfaceRectInPanel.midY
    )
    try runner.expect(
        !geometry.shouldPassthroughExpandedClick(localPoint: inside),
        "surface click should stay inside island"
    )
    try runner.expect(
        geometry.shouldPassthroughExpandedClick(localPoint: CGPoint(x: 2, y: 2)),
        "transparent expanded panel click should passthrough"
    )
    let collapsed = IslandInteractionGeometry(input: IslandInteractionInput(
        screenFrame: CGRect(x: 0, y: 0, width: 1512, height: 982),
        notchSize: CGSize(width: 224, height: 28),
        hasPhysicalNotch: true,
        hasCompactPresence: true,
        isExpanded: false
    ))
    try runner.expect(
        !collapsed.shouldPassthroughExpandedClick(localPoint: CGPoint(x: 2, y: 2)),
        "collapsed clicks should not use expanded passthrough path"
    )
}

runner.test("IslandSurfaceState: new fields decode from minimal JSON") {
    let json = #"{"kind":"compact"}"#.data(using: .utf8)!
    let state = try decoder.decode(IslandSurfaceState.self, from: json)
    try runner.expect(state.surfaceId == nil, "surfaceId should default nil")
    try runner.expect(state.progress == nil, "progress should default nil")
    try runner.expect(state.isSneakPeek == false, "isSneakPeek should default false")
}

runner.test("TopSurfaceCustomization: new fields decode from minimal JSON") {
    let json = #"{"spec_version":1}"#.data(using: .utf8)!
    let custom = try decoder.decode(TopSurfaceCustomization.self, from: json)
    try runner.expect(custom.feedback.audio == false, "feedback.audio should default false")
    try runner.expect(custom.feedback.audioName == nil, "feedback.audioName should default nil")
    try runner.expect(custom.preferredScreenId == nil, "preferredScreenId should default nil")
}

exit(runner.finish())
