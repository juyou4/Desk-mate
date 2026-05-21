import XCTest
@testable import DeskmateCore

final class PetStateMachineTests: XCTestCase {
    private func manifest(
        states: [String: [String]] = [
            "idle": ["idle/000.png"],
            "working": ["working/000.png"],
            "thinking": ["thinking/000.png"],
            "alert": ["alert/000.png"],
            "happy": ["happy/000.png"],
            "nesting": ["nesting/000.png"],
        ],
        fallbacks: [String: String] = CharacterPackManifest.defaultFallbacks
    ) -> CharacterPackManifest {
        var frames: [String: StateFrames] = [:]
        for (k, v) in states { frames[k] = StateFrames(fps: 4, frames: v) }
        return CharacterPackManifest(
            id: "pixie",
            displayName: "Pixie",
            states: frames,
            fallbacks: fallbacks
        )
    }

    func testMoodMapsDirectlyToAnimation() {
        let m = manifest()
        for (mood, expected) in PetStateMachine.moodAnimation {
            let input = PetStateMachine.Input(domain: DomainState(agentMood: mood))
            let out = PetStateMachine.reduce(input, manifest: m)
            XCTAssertEqual(out.animationState, expected, "mood \(mood) should map to \(expected)")
        }
    }

    func testPendingApprovalsForceAlert() {
        let m = manifest()
        let domain = DomainState(
            currentPriority: .p2,
            userFocus: .casual,
            agentMood: .happy,
            pendingApprovals: ["task-1"]
        )
        let out = PetStateMachine.reduce(
            PetStateMachine.Input(domain: domain), manifest: m
        )
        XCTAssertEqual(out.animationState, "alert")
        XCTAssertEqual(out.emotion, "concerned")
        XCTAssertEqual(out.attentionLevel, 1.0, accuracy: 0.0001)
    }

    func testAnimationOverrideWinsOverMood() {
        let m = manifest()
        let input = PetStateMachine.Input(
            domain: DomainState(agentMood: .idle),
            animationOverride: "thinking"
        )
        let out = PetStateMachine.reduce(input, manifest: m)
        XCTAssertEqual(out.animationState, "thinking")
    }

    func testAnimationOverrideWalksManifestFallbacks() {
        // Pack only has ``walking`` but override asks for ``walking_left``.
        let m = manifest(states: [
            "idle": ["idle/000.png"],
            "walking": ["walking/000.png"],
        ])
        let input = PetStateMachine.Input(
            domain: DomainState(agentMood: .idle),
            animationOverride: "walking_left"
        )
        let out = PetStateMachine.reduce(input, manifest: m)
        XCTAssertEqual(out.animationState, "walking")
    }

    func testNestingSwitchesAnchorAndAnimation() {
        let m = manifest()
        let input = PetStateMachine.Input(
            domain: DomainState(agentMood: .idle),
            isNesting: true
        )
        let out = PetStateMachine.reduce(input, manifest: m)
        XCTAssertEqual(out.animationState, "nesting")
        XCTAssertEqual(out.anchorKind, .nest)
    }

    func testFocusedUserDoesNotVolunteerHappyMood() {
        let m = manifest()
        let domain = DomainState(
            currentPriority: .p3,
            userFocus: .focused,
            agentMood: .idle
        )
        let out = PetStateMachine.reduce(
            PetStateMachine.Input(domain: domain), manifest: m
        )
        XCTAssertEqual(out.animationState, "idle")
        XCTAssertLessThanOrEqual(out.attentionLevel, 0.15)
    }

    func testUserInteractingLocksInteractivity() {
        let m = manifest()
        let input = PetStateMachine.Input(
            domain: DomainState(),
            isUserInteracting: true
        )
        let out = PetStateMachine.reduce(input, manifest: m)
        XCTAssertFalse(out.isInteractive)
    }

    func testPriorityDrivesAttentionLevel() {
        let m = manifest()
        let cases: [(Priority, ClosedRange<Double>)] = [
            (.p0, 0.99...1.0),
            (.p1, 0.75...0.85),
            (.p2, 0.45...0.55),
            (.p3, 0.25...0.35),
        ]
        for (priority, range) in cases {
            let domain = DomainState(currentPriority: priority, userFocus: .casual)
            let out = PetStateMachine.reduce(
                PetStateMachine.Input(domain: domain), manifest: m
            )
            XCTAssertTrue(
                range.contains(out.attentionLevel),
                "priority \(priority) attention \(out.attentionLevel) not in \(range)"
            )
        }
    }

    func testMissingManifestStateFallsBackToIdle() {
        // Manifest has no ``thinking`` state and no fallback for it.
        let m = manifest(
            states: [
                "idle": ["idle/000.png"],
                "working": ["working/000.png"],
            ],
            fallbacks: [:]
        )
        let input = PetStateMachine.Input(domain: DomainState(agentMood: .thinking))
        let out = PetStateMachine.reduce(input, manifest: m)
        XCTAssertEqual(out.animationState, "idle")
    }

    func testBubbleIdIsCarriedFromPreviousState() {
        let m = manifest()
        let previous = PetPresentationState(bubbleId: "bubble-xyz")
        let input = PetStateMachine.Input(domain: DomainState(agentMood: .happy))
        let out = PetStateMachine.reduce(input, manifest: m, previous: previous)
        XCTAssertEqual(out.bubbleId, "bubble-xyz")
    }
}
