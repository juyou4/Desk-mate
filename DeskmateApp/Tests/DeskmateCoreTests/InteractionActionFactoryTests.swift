import XCTest
@testable import DeskmateCore

final class InteractionActionFactoryTests: XCTestCase {
    // MARK: - resolveApproval

    func testResolveApprovalAllowProducesSnakeCasePayload() {
        let a = InteractionActionFactory.resolveApproval(
            id: "ap-1", allow: true
        )
        XCTAssertEqual(a.source, .menuBar)
        XCTAssertEqual(a.target, .system)
        XCTAssertEqual(a.kind, .permissionResolve)
        XCTAssertEqual(a.payload["approval_id"], .string("ap-1"))
        XCTAssertEqual(a.payload["allow"], .bool(true))
        XCTAssertEqual(a.payload.keys.sorted(), ["allow", "approval_id"])
    }

    func testResolveApprovalDenyProducesAllowFalse() {
        let a = InteractionActionFactory.resolveApproval(
            id: "ap-1", allow: false
        )
        XCTAssertEqual(a.payload["allow"], .bool(false))
    }

    func testResolveApprovalSourceOverride() {
        let a = InteractionActionFactory.resolveApproval(
            id: "ap-1", allow: true, source: .island
        )
        XCTAssertEqual(a.source, .island)
    }

    func testResolveApprovalSerializesToCanonicalWire() throws {
        let a = InteractionActionFactory.resolveApproval(
            id: "ap-42", allow: true
        )
        let data = try JSONEncoder().encode(a)
        let obj = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        XCTAssertEqual(obj["source"] as? String, "menu_bar")
        XCTAssertEqual(obj["target"] as? String, "system")
        XCTAssertEqual(obj["kind"] as? String, "permission.resolve")
        let payload = try XCTUnwrap(obj["payload"] as? [String: Any])
        XCTAssertEqual(payload["approval_id"] as? String, "ap-42")
        XCTAssertEqual(payload["allow"] as? Bool, true)
    }

    // MARK: - jumpToSession

    func testJumpToSession() {
        let a = InteractionActionFactory.jumpToSession(id: "s-7")
        XCTAssertEqual(a.kind, .sessionJump)
        XCTAssertEqual(a.target, .session)
        XCTAssertEqual(a.payload["session_id"], .string("s-7"))
    }

    // MARK: - answerQuestion

    func testAnswerQuestion() {
        let a = InteractionActionFactory.answerQuestion(
            sessionId: "s-7",
            answer: "Use Cursor",
            source: .island
        )
        XCTAssertEqual(a.source, .island)
        XCTAssertEqual(a.kind, .questionAnswer)
        XCTAssertEqual(a.target, .session)
        XCTAssertEqual(a.payload["session_id"], .string("s-7"))
        XCTAssertEqual(a.payload["answer"], .string("Use Cursor"))
    }

    // MARK: - demoTrigger

    func testDemoTrigger() {
        let a = InteractionActionFactory.demoTrigger(scenario: "codex_session")
        XCTAssertEqual(a.source, .menuBar)
        XCTAssertEqual(a.target, .system)
        XCTAssertEqual(a.kind, .demoTrigger)
        XCTAssertEqual(a.payload["scenario"], .string("codex_session"))
    }

    // MARK: - petInteract

    func testPetInteractDefaultsToClickGesture() {
        let a = InteractionActionFactory.petInteract()
        XCTAssertEqual(a.source, .pet)
        XCTAssertEqual(a.target, .bubble)
        XCTAssertEqual(a.kind, .petInteract)
        XCTAssertEqual(a.payload["gesture"], .string("click"))
    }

    func testPetInteractGestureOverride() {
        let a = InteractionActionFactory.petInteract(gesture: "drag")
        XCTAssertEqual(a.payload["gesture"], .string("drag"))
    }

    // MARK: - bubbleAction

    func testBubbleActionTranslatesKnownKind() throws {
        let bubble = BubbleAction(
            label: "Allow",
            interactionKind: InteractionKind.permissionResolve.rawValue,
            payload: ["approval_id": .string("ap-9"), "allow": .bool(true)]
        )
        let a = try XCTUnwrap(
            InteractionActionFactory.bubbleAction(bubble, bubbleId: "bb-1")
        )
        XCTAssertEqual(a.source, .pet)
        XCTAssertEqual(a.target, .bubble)
        XCTAssertEqual(a.kind, .permissionResolve)
        XCTAssertEqual(a.payload["approval_id"], .string("ap-9"))
        XCTAssertEqual(a.payload["allow"], .bool(true))
        XCTAssertEqual(a.payload["bubble_id"], .string("bb-1"))
    }

    func testBubbleActionReturnsNilForUnknownKind() {
        let bubble = BubbleAction(
            label: "Unknown",
            interactionKind: "future.verb.we.dont.know",
            payload: [:]
        )
        XCTAssertNil(
            InteractionActionFactory.bubbleAction(bubble, bubbleId: "bb-1"),
            "unknown kinds must degrade to nil"
        )
    }

    func testBubbleActionInjectsBubbleIdIntoPayloadWithoutLosingOriginals() throws {
        let bubble = BubbleAction(
            label: "Snooze",
            interactionKind: InteractionKind.surfaceDismiss.rawValue,
            payload: ["reason": .string("snooze"), "until_ms": .int(1_200)]
        )
        let a = try XCTUnwrap(
            InteractionActionFactory.bubbleAction(bubble, bubbleId: "bb-7")
        )
        XCTAssertEqual(a.payload["bubble_id"], .string("bb-7"))
        XCTAssertEqual(a.payload["reason"], .string("snooze"))
        XCTAssertEqual(a.payload["until_ms"], .int(1_200))
    }
}
