import Foundation

/// Ordered collection of :type:`IslandModule` instances (V10 I5).
///
/// Modules are kept in **priority-descending** order. Queries walk the list
/// from highest to lowest priority so the first claim wins.
///
/// The registry is a ``struct`` holding references (``any IslandModule``)
/// so it's cheap to snapshot and pass around. Modules themselves hold any
/// stateful bookkeeping.
public struct IslandModuleRegistry {
    public private(set) var modules: [any IslandModule]

    public init(modules: [any IslandModule] = []) {
        self.modules = modules
        sort()
    }

    // MARK: - Mutation

    /// Register (or replace, by ``id``) a module. The registry re-sorts after
    /// every change so ``module(for:)`` / ``dispatch`` can iterate naively.
    public mutating func register(_ module: any IslandModule) {
        if let idx = modules.firstIndex(where: { $0.id == module.id }) {
            modules[idx] = module
        } else {
            modules.append(module)
        }
        sort()
    }

    public mutating func unregister(id: String) {
        modules.removeAll { $0.id == id }
    }

    public mutating func removeAll() {
        modules.removeAll()
    }

    // MARK: - Queries

    /// Return the highest-priority module that claims ``state``.
    public func module(for state: IslandSurfaceState) -> (any IslandModule)? {
        modules.first { $0.claims(state: state) }
    }

    /// Return the renderer-neutral descriptor from the highest-priority
    /// module that both claims ``state`` and can describe it.
    public func renderDescriptor(
        for state: IslandSurfaceState
    ) -> IslandModuleRenderDescriptor? {
        for module in modules where module.claims(state: state) {
            if let descriptor = module.render(state: state) {
                return descriptor
            }
        }
        return nil
    }

    /// Dispatch an interaction to modules in priority order. Returns the id
    /// of the module that handled it, or ``nil`` when nobody claimed it.
    @discardableResult
    public func dispatch(_ action: InteractionAction) -> String? {
        for module in modules where module.handle(action) {
            return module.id
        }
        return nil
    }

    public var count: Int { modules.count }
    public var isEmpty: Bool { modules.isEmpty }

    public func contains(id: String) -> Bool {
        modules.contains { $0.id == id }
    }

    // MARK: - Internals

    private mutating func sort() {
        modules.sort { $0.claimPriority > $1.claimPriority }
    }
}
