import Foundation

/// Anything that can put a :class:`BridgeEnvelope` on the wire
/// (V10 Phase 11b).
///
/// The protocol exists so the high-level helpers (``sendAction``,
/// ``sendUserMessage``, ``sendPerception`` …) can be written once and
/// reused by both :class:`BridgeClient` and
/// :class:`ReconnectingBridgeClient` — callers shouldn't have to know
/// which transport flavour they hold.
public protocol EnvelopeSender: AnyObject {
    func send(_ envelope: BridgeEnvelope) throws
}

extension EnvelopeSender {
    /// Wrap a typed :class:`InteractionAction` in an ``interaction``
    /// envelope and send it.
    public func send(
        action: InteractionAction, traceId: String? = nil
    ) throws {
        let payload = try Self.encodeAsPayload(action)
        try send(BridgeEnvelope.of(
            .interaction, payload: payload, traceId: traceId
        ))
    }

    /// Send a free-form ``user.message`` envelope. Python dispatches
    /// into the reactive chain.
    public func sendUserMessage(
        _ text: String, traceId: String? = nil
    ) throws {
        try send(BridgeEnvelope.of(
            .userMessage,
            payload: ["text": .string(text)],
            traceId: traceId
        ))
    }

    /// ``user.click_pet`` has no payload; this is just a named helper
    /// so callers don't accidentally drop the envelope type.
    public func sendUserClickPet(traceId: String? = nil) throws {
        try send(BridgeEnvelope.of(.userClickPet, traceId: traceId))
    }

    /// Send a perception delta. Matches the Python
    /// ``_context_from_perception`` reader keys.
    public func sendPerception(
        _ snapshot: PerceptionSnapshot, traceId: String? = nil
    ) throws {
        let payload = try Self.encodeAsPayload(snapshot)
        try send(BridgeEnvelope.of(
            .perception, payload: payload, traceId: traceId
        ))
    }

    // MARK: - Internals

    /// Encode ``value`` to JSON bytes and decode it back as an
    /// AnyJSONValue dictionary. This is the tightest way to reuse a
    /// type's ``Codable`` output without hand-rolling encoders.
    static func encodeAsPayload<T: Encodable>(
        _ value: T
    ) throws -> [String: AnyJSONValue] {
        let encoder = JSONEncoder()
        let data = try encoder.encode(value)
        return try JSONDecoder().decode(
            [String: AnyJSONValue].self, from: data
        )
    }
}

// MARK: - Adoptions

extension BridgeClient: EnvelopeSender {}
extension ReconnectingBridgeClient: EnvelopeSender {}
