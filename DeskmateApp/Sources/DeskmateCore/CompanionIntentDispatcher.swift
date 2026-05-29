import Foundation

/// Central router for incoming :class:`CompanionIntent` values
/// (V10 Phase 7 / L1-C + L1-B).
///
/// Replaces the ad-hoc ``switch intent.kind`` that would otherwise be
/// sprinkled across Pet / Island / menu-bar view controllers. Each
/// surface registers a typed handler once; the dispatcher fans incoming
/// intents from the bridge to the right handler and silently drops
/// unknown kinds so a newer Python agent never crashes an older Swift
/// client.
public final class CompanionIntentDispatcher {
    public typealias Handler = (CompanionIntent) -> Void

    public enum DispatchResult: Equatable, Sendable {
        case handled(IntentKind)
        case noHandler(IntentKind)
        case droppedUnknown
    }

    private var handlers: [IntentKind: Handler] = [:]

    public init() {}

    /// Register a handler for ``kind``. Replaces any prior handler for
    /// the same kind so tests and owners can re-bind cleanly.
    public func register(
        kind: IntentKind, handler: @escaping Handler
    ) {
        guard kind != .unknown else { return }  // unknown is reserved
        handlers[kind] = handler
    }

    public func unregister(kind: IntentKind) {
        handlers.removeValue(forKey: kind)
    }

    public func hasHandler(for kind: IntentKind) -> Bool {
        handlers[kind] != nil
    }

    /// Route ``intent`` to the registered handler (if any). Returns
    /// what happened so callers can surface diagnostics.
    @discardableResult
    public func dispatch(_ intent: CompanionIntent) -> DispatchResult {
        if intent.kind == .unknown {
            return .droppedUnknown
        }
        guard let handler = handlers[intent.kind] else {
            return .noHandler(intent.kind)
        }
        handler(intent)
        return .handled(intent.kind)
    }
}

// MARK: - Convenience: DomainState binding

extension CompanionIntentDispatcher {
    /// Install a handler that decodes ``UPDATE_DOMAIN_STATE`` intents
    /// and writes the embedded :class:`DomainState` into ``store``.
    ///
    /// Malformed payloads are tolerated — ``onDecodeError`` is invoked
    /// so callers can log, and the store is left untouched.
    public func bindDomainState(
        to store: LiveDomainStateStore,
        onDecodeError: ((Error) -> Void)? = nil
    ) {
        register(kind: .updateDomainState) { [weak store] intent in
            guard let store else { return }
            do {
                let state = try Self.decodeDomainState(from: intent)
                store.apply(state)
            } catch {
                onDecodeError?(error)
            }
        }
    }

    /// Pure helper — pulled out so tests can assert decoding behavior
    /// without building a whole dispatcher + store.
    public static func decodeDomainState(
        from intent: CompanionIntent
    ) throws -> DomainState {
        guard let raw = intent.payload["domain_state"] else {
            throw DecodingError.keyNotFound(
                DomainStateCodingKey.domainState,
                .init(
                    codingPath: [],
                    debugDescription: "update_domain_state payload missing domain_state"
                )
            )
        }
        let data = try JSONEncoder().encode(raw)
        return try JSONDecoder().decode(DomainState.self, from: data)
    }

    private enum DomainStateCodingKey: String, CodingKey {
        case domainState = "domain_state"
    }
}

// MARK: - Convenience: BubbleQueue binding

extension CompanionIntentDispatcher {
    /// Install handlers for ``SHOW_PET_BUBBLE``, ``UPDATE_PET_BUBBLE``,
    /// and ``DISMISS_PET_BUBBLE`` intents, routing them into ``queue``.
    /// Malformed payloads are reported via ``onDecodeError`` instead of
    /// crashing the dispatcher.
    public func bindBubbleQueue(
        to queue: LiveBubbleQueue,
        onDecodeError: ((Error) -> Void)? = nil
    ) {
        register(kind: .showPetBubble) { [weak queue] intent in
            guard let queue else { return }
            do {
                let spec = try Self.decodeBubbleSpec(from: intent)
                queue.enqueue(spec)
            } catch {
                onDecodeError?(error)
            }
        }
        register(kind: .updatePetBubble) { [weak queue] intent in
            guard let queue else { return }
            do {
                let patch = try Self.decodeBubblePatch(from: intent)
                let applied = queue.update(
                    id: patch.bubbleId,
                    text: patch.text,
                    markdown: patch.markdown
                )
                if !applied {
                    // V10 L3-B1 fail-soft: a token update for a bubble
                    // that already aged out (TTL elapsed, user
                    // dismissed) is a no-op. The streaming source
                    // should re-emit a ``show_pet_bubble`` if it
                    // wants to revive the conversation.
                    onDecodeError?(DecodingError.dataCorrupted(
                        .init(
                            codingPath: [],
                            debugDescription:
                                "update_pet_bubble id not in queue: \(patch.bubbleId)"
                        )
                    ))
                }
            } catch {
                onDecodeError?(error)
            }
        }
        register(kind: .dismissPetBubble) { [weak queue] intent in
            guard let queue else { return }
            guard let value = intent.payload["bubble_id"],
                  case .string(let bubbleId) = value
            else {
                onDecodeError?(DecodingError.typeMismatch(
                    String.self,
                    .init(
                        codingPath: [],
                        debugDescription: "dismiss_pet_bubble payload missing bubble_id"
                    )
                ))
                return
            }
            queue.dismiss(id: bubbleId)
        }
    }

    /// Decode a :class:`BubbleSpec` from the ``bubble`` payload field.
    public static func decodeBubbleSpec(
        from intent: CompanionIntent
    ) throws -> BubbleSpec {
        guard let raw = intent.payload["bubble"] else {
            throw DecodingError.keyNotFound(
                BubbleSpecCodingKey.bubble,
                .init(
                    codingPath: [],
                    debugDescription: "show_pet_bubble payload missing bubble"
                )
            )
        }
        let data = try JSONEncoder().encode(raw)
        return try JSONDecoder().decode(BubbleSpec.self, from: data)
    }

    public struct BubblePatch: Equatable, Sendable {
        public let bubbleId: String
        public let text: String
        public let markdown: String?
    }

    /// Decode a streaming patch payload (``bubble_id`` + ``text``)
    /// for ``update_pet_bubble``.
    public static func decodeBubblePatch(
        from intent: CompanionIntent
    ) throws -> BubblePatch {
        guard case .string(let bubbleId)? = intent.payload["bubble_id"] else {
            throw DecodingError.typeMismatch(
                String.self,
                .init(
                    codingPath: [],
                    debugDescription: "update_pet_bubble payload missing bubble_id"
                )
            )
        }
        guard case .string(let text)? = intent.payload["text"] else {
            throw DecodingError.typeMismatch(
                String.self,
                .init(
                    codingPath: [],
                    debugDescription: "update_pet_bubble payload missing text"
                )
            )
        }
        var markdown: String? = nil
        if case .string(let md)? = intent.payload["markdown"] {
            markdown = md
        }
        return BubblePatch(bubbleId: bubbleId, text: text, markdown: markdown)
    }

    private enum BubbleSpecCodingKey: String, CodingKey {
        case bubble
    }
}

// MARK: - Convenience: Island surface binding

extension CompanionIntentDispatcher {
    /// Install handlers for ``PRESENT_ISLAND`` / ``UPDATE_ISLAND`` /
    /// ``DISMISS_ISLAND`` intents, routing them into ``store``.
    public func bindIslandSurface(
        to store: LiveIslandSurfaceStore,
        onDecodeError: ((Error) -> Void)? = nil
    ) {
        register(kind: .presentIsland) { [weak store] intent in
            guard let store else { return }
            do {
                let (kind, sessionId, activityId, detail, surfaceId, priority) =
                    try Self.decodePresentIsland(from: intent)
                store.present(
                    kind: kind,
                    sessionId: sessionId,
                    activityId: activityId,
                    detail: detail,
                    surfaceId: surfaceId,
                    priority: priority
                )
            } catch {
                onDecodeError?(error)
            }
        }
        register(kind: .updateIsland) { [weak store] intent in
            guard let store else { return }
            do {
                let (activityId, detail, progress) =
                    try Self.decodeUpdateIsland(from: intent)
                store.update(activityId: activityId, detail: detail, progress: progress)
            } catch {
                onDecodeError?(error)
            }
        }
        register(kind: .dismissIsland) { [weak store] intent in
            guard let store else { return }
            // Dismiss accepts an optional id; missing means "clear whatever".
            let id = Self.decodeDismissIsland(from: intent)
            store.dismiss(id: id)
        }
    }

    // MARK: - Typed payload decoders

    /// Decoded tuple for a ``present_island`` intent. Priority defaults
    /// to ``P2`` when the payload omits it — most callers signal P0/P1
    /// only for notification cards they really want pinned.
    public static func decodePresentIsland(
        from intent: CompanionIntent
    ) throws -> (
        kind: IslandSurfaceKind,
        sessionId: String?,
        activityId: String?,
        detail: String?,
        surfaceId: String?,
        priority: Priority
    ) {
        guard let surfaceValue = intent.payload["surface"],
              case .string(let surfaceRaw) = surfaceValue,
              let kind = IslandSurfaceKind(rawValue: surfaceRaw)
        else {
            throw DecodingError.typeMismatch(
                IslandSurfaceKind.self,
                .init(
                    codingPath: [],
                    debugDescription: "present_island payload missing valid 'surface'"
                )
            )
        }
        var sessionId: String? = nil
        if case .string(let sid) = intent.payload["session_id"] ?? .null {
            sessionId = sid
        }
        var activityId: String? = nil
        if case .string(let aid) = intent.payload["activity_id"] ?? .null {
            activityId = aid
        }
        var detail: String? = nil
        if case .string(let d) = intent.payload["detail"] ?? .null {
            detail = d
        }
        var surfaceId: String? = nil
        if case .string(let sid) = intent.payload["surface_id"] ?? .null {
            surfaceId = sid
        }
        var priority: Priority = .p2
        if case .string(let praw) = intent.payload["priority"] ?? .null,
           let p = Priority(rawValue: praw) {
            priority = p
        }
        return (kind, sessionId, activityId, detail, surfaceId, priority)
    }

    public static func decodeUpdateIsland(
        from intent: CompanionIntent
    ) throws -> (activityId: String, detail: String?, progress: Double?) {
        guard let raw = intent.payload["activity_id"],
              case .string(let aid) = raw,
              !aid.isEmpty
        else {
            throw DecodingError.typeMismatch(
                String.self,
                .init(
                    codingPath: [],
                    debugDescription: "update_island payload missing 'activity_id'"
                )
            )
        }
        var detail: String? = nil
        if case .string(let d) = intent.payload["detail"] ?? .null {
            detail = d
        }
        var progress: Double? = nil
        if case .double(let p) = intent.payload["progress"] ?? .null {
            progress = p
        } else if case .int(let i) = intent.payload["progress"] ?? .null {
            progress = Double(i)
        }
        return (aid, detail, progress)
    }

    public static func decodeDismissIsland(
        from intent: CompanionIntent
    ) -> String? {
        // id is optional; return nil for a generic dismiss. We don't
        // throw here because "dismiss whatever's showing" is a legit
        // payload-free request.
        if case .string(let id) = intent.payload["id"] ?? .null {
            return id
        }
        if case .string(let sid) = intent.payload["session_id"] ?? .null {
            return sid
        }
        if case .string(let aid) = intent.payload["activity_id"] ?? .null {
            return aid
        }
        return nil
    }
}

// MARK: - Convenience: BridgeClient binding

/// Anything that publishes :class:`BridgeEnvelope` values to one or
/// more subscribers. Lets the dispatcher bind to either a raw
/// :class:`BridgeClient` or a :class:`ReconnectingBridgeClient`
/// without the two needing a shared base class. Implementations must
/// support *multiple* concurrent subscribers so the intent dispatcher
/// + snapshot hydrator + diagnostic consumers can all coexist.
public protocol EnvelopeReceiver: AnyObject {
    @discardableResult
    func onEnvelope(
        _ cb: @escaping (BridgeEnvelope) -> Void
    ) -> () -> Void
}

extension BridgeClient: EnvelopeReceiver {}

extension CompanionIntentDispatcher {
    /// Subscribe to ``bridge`` and dispatch every ``.intent`` envelope
    /// into the registered handlers. Non-intent envelopes (snapshot,
    /// ready, pong …) are ignored and left for whatever other glue
    /// layers the caller wires up.
    ///
    /// ``onDecodeError`` fires when an envelope's payload can't be
    /// reshaped into a :class:`CompanionIntent` — the envelope is
    /// dropped but the bridge keeps running.
    public func bind(
        bridge: EnvelopeReceiver,
        onDecodeError: ((Error) -> Void)? = nil
    ) {
        bridge.onEnvelope { [weak self] envelope in
            guard envelope.type == .intent else { return }
            do {
                let intent = try Self.decodeCompanionIntent(from: envelope)
                self?.dispatch(intent)
            } catch {
                onDecodeError?(error)
            }
        }
    }

    /// Pure helper: turn an envelope's payload dict back into a
    /// :class:`CompanionIntent`. The envelope's ``payload`` *is* the
    /// serialized CompanionIntent (``{kind, payload}``) — Python's
    /// intent_sink writes it that way in :func:`App.serve_forever`.
    public static func decodeCompanionIntent(
        from envelope: BridgeEnvelope
    ) throws -> CompanionIntent {
        let encoder = JSONEncoder()
        let data = try encoder.encode(envelope.payload)
        return try JSONDecoder().decode(CompanionIntent.self, from: data)
    }
}
