import Foundation

public enum BridgeProtocol {
    public static let specVersion: Int = 1
}

/// Top-level envelope type discriminator.
///
/// Keep in lock-step with `shared/protocol.md` and the Python ``EnvelopeType``.
/// Extend additively only; never reuse or rename existing raw values.
public enum EnvelopeType: String, Codable, Sendable, CaseIterable {
    // Swift → Python
    case perception
    case userMessage = "user.message"
    case userClickPet = "user.click_pet"
    case interaction

    // Python → Swift
    case intent

    // Bidirectional / lifecycle
    case ping
    case pong
    case stateSnapshotRequest = "state.snapshot.request"
    case stateSnapshot = "state.snapshot"
    case agentReady = "agent.ready"
    case agentPause = "agent.pause"

    // V10 §3.1 row 6 + row 8: Swift-side hard budget metrics
    // (wake-to-first-frame latency + frame drop ratio). Pushed by
    // the Swift shell at a configurable cadence; the Python agent
    // logs them so a perf regression surfaces in the structlog
    // stream rather than silently passing the budgets.
    case perfMetrics = "perf.metrics"
}

/// Single envelope shape for every message on the UDS bridge.
///
/// - ``traceId`` is always populated; callers may supply one to correlate an
///   end-to-end interaction (V10 L3 Instrumentation).
/// - ``payload`` stores an open JSON object so forward-compatible keys survive
///   round-trips without explicit schemas (V10 L1 forward-compat contract).
public struct BridgeEnvelope: Codable, Sendable, Equatable {
    public var specVersion: Int
    public var type: EnvelopeType
    public var traceId: String
    public var tsMs: Int?
    public var payload: [String: AnyJSONValue]

    public init(
        specVersion: Int = BridgeProtocol.specVersion,
        type: EnvelopeType,
        traceId: String = BridgeEnvelope.newTraceId(),
        tsMs: Int? = nil,
        payload: [String: AnyJSONValue] = [:]
    ) {
        self.specVersion = specVersion
        self.type = type
        self.traceId = traceId
        self.tsMs = tsMs
        self.payload = payload
    }

    /// Convenience constructor that doesn't force callers to repeat defaults.
    public static func of(
        _ type: EnvelopeType,
        payload: [String: AnyJSONValue] = [:],
        traceId: String? = nil,
        tsMs: Int? = nil
    ) -> BridgeEnvelope {
        BridgeEnvelope(
            type: type,
            traceId: traceId ?? newTraceId(),
            tsMs: tsMs,
            payload: payload
        )
    }

    /// 32-char lowercase hex, matching the Python ``new_trace_id`` format.
    public static func newTraceId() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    private enum CodingKeys: String, CodingKey {
        case specVersion = "spec_version"
        case type
        case traceId = "trace_id"
        case tsMs = "ts_ms"
        case payload
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.specVersion = try c.decodeIfPresent(Int.self, forKey: .specVersion)
            ?? BridgeProtocol.specVersion
        self.type = try c.decode(EnvelopeType.self, forKey: .type)
        self.traceId = try c.decodeIfPresent(String.self, forKey: .traceId)
            ?? BridgeEnvelope.newTraceId()
        self.tsMs = try c.decodeIfPresent(Int.self, forKey: .tsMs)
        self.payload = try c.decodeIfPresent([String: AnyJSONValue].self, forKey: .payload)
            ?? [:]
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(specVersion, forKey: .specVersion)
        try c.encode(type, forKey: .type)
        try c.encode(traceId, forKey: .traceId)
        try c.encodeIfPresent(tsMs, forKey: .tsMs)
        try c.encode(payload, forKey: .payload)
    }
}
