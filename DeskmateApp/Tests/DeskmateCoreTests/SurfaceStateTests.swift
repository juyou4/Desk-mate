import XCTest
@testable import DeskmateCore

final class SurfaceStateTests: XCTestCase {
    func testIslandSurfaceKindMatchesL1E() {
        // V10 L1-E: exactly these five kinds. Raw values are wire format.
        let raws = Set(IslandSurfaceKind.allCases.map(\.rawValue))
        XCTAssertEqual(
            raws,
            ["compact", "notification_card", "session_list", "live_activity", "empty"]
        )
    }

    func testIslandSurfaceStateDefaultsToCompact() {
        let s = IslandSurfaceState()
        XCTAssertEqual(s.kind, .compact)
        XCTAssertNil(s.sessionId)
    }

    func testPetPresentationStateDefaults() {
        let p = PetPresentationState()
        XCTAssertEqual(p.anchorKind, .desktop)
        XCTAssertEqual(p.velocity, PetVelocity())
        XCTAssertEqual(p.avatarStyle, "pixel")
        XCTAssertTrue(p.isInteractive)
    }

    func testPetAnchorAndNestPolicyDecodeForwardCompatibly() throws {
        let json = #"""
        {
          "kind": "nest",
          "target_nest": "notch",
          "future": true
        }
        """#.data(using: .utf8)!
        let anchor = try JSONDecoder().decode(PetAnchor.self, from: json)
        XCTAssertEqual(anchor.kind, .nest)
        XCTAssertEqual(anchor.targetNest, "notch")

        let policyJson = #"{ "should_leave_nest": true }"#.data(using: .utf8)!
        let policy = try JSONDecoder().decode(
            NestBehaviorPolicy.self,
            from: policyJson
        )
        XCTAssertTrue(policy.canEnterNest)
        XCTAssertTrue(policy.shouldLeaveNest)
    }

    func testTopSurfaceCustomizationStorePublishesChanges() throws {
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
        XCTAssertTrue(store.apply(next))
        XCTAssertEqual(seen, [next])
        XCTAssertFalse(store.apply(next))
        unsubscribe()
        XCTAssertEqual(store.subscriberCount, 0)
    }

    func testBubbleSpecDefaultsMatchProtocol() throws {
        let json = #"{ "id": "b1", "text": "hi", "kind": "approval_hint" }"#.data(using: .utf8)!
        let spec = try JSONDecoder().decode(BubbleSpec.self, from: json)
        XCTAssertEqual(spec.kind, .approvalHint)
        XCTAssertEqual(spec.ttlMs, 8000)
        XCTAssertEqual(spec.priority, .p2)
    }

    func testDomainStateDefaultPriorityIsP3() {
        let ds = DomainState()
        XCTAssertEqual(ds.currentPriority, .p3)
        XCTAssertEqual(ds.agentMood, .idle)
        XCTAssertTrue(ds.pendingApprovals.isEmpty)
    }
}
