import XCTest
@testable import DeskmateCore

private final class StubModule: IslandModule {
    let id: String
    let claimPriority: Int
    let supportedKinds: Set<IslandSurfaceKind>
    var handledActions: [InteractionAction] = []
    var shouldHandle: Bool
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
        handledActions.append(action)
        return shouldHandle
    }

    func render(state: IslandSurfaceState) -> IslandModuleRenderDescriptor? {
        descriptor
    }
}

final class IslandModuleRegistryTests: XCTestCase {

    private func dummyAction(kind: InteractionKind = .taskOpenDetail) -> InteractionAction {
        InteractionAction(source: .island, target: .session, kind: kind)
    }

    // MARK: - Registration

    func testRegisterAddsModule() {
        var r = IslandModuleRegistry()
        r.register(StubModule(id: "session"))
        XCTAssertEqual(r.count, 1)
        XCTAssertTrue(r.contains(id: "session"))
    }

    func testModuleDefaultsExposePlanMetadata() {
        let module = StubModule(id: "session")
        XCTAssertEqual(module.displayName, "session")
        XCTAssertEqual(module.defaultSide, .center)
        XCTAssertEqual(module.defaultOrder, 0)
        XCTAssertTrue(module.isVisible)
        XCTAssertNil(module.preferredWidth)
        XCTAssertNil(module.render(state: IslandSurfaceState(kind: .sessionList)))
    }

    func testRegisterReplacesSameId() {
        var r = IslandModuleRegistry()
        r.register(StubModule(id: "session", claimPriority: 5))
        r.register(StubModule(id: "session", claimPriority: 10))
        XCTAssertEqual(r.count, 1)
        XCTAssertEqual(r.modules.first?.claimPriority, 10)
    }

    func testUnregisterRemovesModule() {
        var r = IslandModuleRegistry()
        r.register(StubModule(id: "a"))
        r.register(StubModule(id: "b"))
        r.unregister(id: "a")
        XCTAssertEqual(r.count, 1)
        XCTAssertFalse(r.contains(id: "a"))
    }

    func testRemoveAllClearsRegistry() {
        var r = IslandModuleRegistry()
        r.register(StubModule(id: "a"))
        r.register(StubModule(id: "b"))
        r.removeAll()
        XCTAssertTrue(r.isEmpty)
    }

    // MARK: - Claim resolution

    func testModuleForStateReturnsHighestPriorityClaim() {
        var r = IslandModuleRegistry()
        r.register(StubModule(id: "low", claimPriority: 1))
        r.register(StubModule(id: "high", claimPriority: 10))
        let resolved = r.module(for: IslandSurfaceState(kind: .notificationCard))
        XCTAssertEqual(resolved?.id, "high")
    }

    func testModuleForStateSkipsUnsupportedKinds() {
        var r = IslandModuleRegistry()
        r.register(
            StubModule(id: "notifier", supportedKinds: [.notificationCard])
        )
        r.register(
            StubModule(id: "live", supportedKinds: [.liveActivity])
        )
        let resolved = r.module(for: IslandSurfaceState(kind: .liveActivity))
        XCTAssertEqual(resolved?.id, "live")
    }

    func testModuleForUnclaimedStateReturnsNil() {
        var r = IslandModuleRegistry()
        r.register(StubModule(id: "notifier", supportedKinds: [.notificationCard]))
        let resolved = r.module(for: IslandSurfaceState(kind: .liveActivity))
        XCTAssertNil(resolved)
    }

    func testRenderDescriptorReturnsFirstClaimingDescriptor() {
        var r = IslandModuleRegistry()
        r.register(
            StubModule(
                id: "empty",
                claimPriority: 10,
                descriptor: nil
            )
        )
        r.register(
            StubModule(
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

        XCTAssertEqual(descriptor?.title, "NOTICE")
        XCTAssertEqual(descriptor?.subtitle, "Build done")
        XCTAssertEqual(descriptor?.badge, "now")
        XCTAssertEqual(descriptor?.systemImageName, "sparkle")
    }

    func testDefaultModulesRenderCoreIslandStates() {
        let registry = IslandModuleRegistry.deskmateDefaultModules()

        let reminder = registry.renderDescriptor(
            for: IslandSurfaceState(
                kind: .notificationCard,
                activityId: "demo-reminder",
                detail: "Reminder due now"
            )
        )
        XCTAssertEqual(reminder?.title, "REMIND")
        XCTAssertEqual(reminder?.systemImageName, "bell.fill")

        let build = registry.renderDescriptor(
            for: IslandSurfaceState(
                kind: .liveActivity,
                activityId: "build-demo",
                detail: "Running tests"
            )
        )
        XCTAssertEqual(build?.title, "BUILD")
        XCTAssertEqual(build?.subtitle, "Running tests")

        let sessions = registry.renderDescriptor(
            for: IslandSurfaceState(kind: .sessionList)
        )
        XCTAssertEqual(sessions?.title, "SESS")

        let idle = registry.renderDescriptor(
            for: IslandSurfaceState(kind: .compact)
        )
        XCTAssertEqual(idle?.title, "DM")
    }

    // MARK: - Dispatch

    func testDispatchStopsAtFirstHandler() {
        var r = IslandModuleRegistry()
        let high = StubModule(id: "high", claimPriority: 10)
        let low = StubModule(id: "low", claimPriority: 1)
        r.register(low)
        r.register(high)

        let handled = r.dispatch(dummyAction())
        XCTAssertEqual(handled, "high")
        XCTAssertEqual(high.handledActions.count, 1)
        XCTAssertTrue(low.handledActions.isEmpty, "low should not see the action")
    }

    func testDispatchFallsThroughUntilSomeoneReturnsTrue() {
        var r = IslandModuleRegistry()
        let first = StubModule(id: "first", claimPriority: 10, shouldHandle: false)
        let second = StubModule(id: "second", claimPriority: 1, shouldHandle: true)
        r.register(first)
        r.register(second)

        let handled = r.dispatch(dummyAction())
        XCTAssertEqual(handled, "second")
        XCTAssertEqual(first.handledActions.count, 1)
        XCTAssertEqual(second.handledActions.count, 1)
    }

    func testDispatchReturnsNilWhenNoHandlerClaims() {
        var r = IslandModuleRegistry()
        r.register(StubModule(id: "a", shouldHandle: false))
        r.register(StubModule(id: "b", shouldHandle: false))
        XCTAssertNil(r.dispatch(dummyAction()))
    }

    func testEmptyRegistryDispatchReturnsNil() {
        let r = IslandModuleRegistry()
        XCTAssertNil(r.dispatch(dummyAction()))
    }
}
