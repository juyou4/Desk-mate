import Foundation

/// JSON value that preserves arbitrary payload contents verbatim.
///
/// The Python protocol layer uses ``dict[str, Any]`` for envelope/action
/// payloads so new keys can be added without breaking old clients (V10 L1
/// forward-compat contract). On the Swift side we mirror this with a Codable
/// value type that can hold any JSON scalar, array, or object without loss.
///
/// Consumers that want typed access should decode ``payload`` into a
/// purpose-built struct after checking the envelope's ``type`` discriminator.
public indirect enum AnyJSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([AnyJSONValue])
    case object([String: AnyJSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        if let value = try? container.decode(Bool.self) {
            self = .bool(value)
            return
        }
        if let value = try? container.decode(Int.self) {
            self = .int(value)
            return
        }
        if let value = try? container.decode(Double.self) {
            self = .double(value)
            return
        }
        if let value = try? container.decode(String.self) {
            self = .string(value)
            return
        }
        if let value = try? container.decode([AnyJSONValue].self) {
            self = .array(value)
            return
        }
        if let value = try? container.decode([String: AnyJSONValue].self) {
            self = .object(value)
            return
        }
        throw DecodingError.dataCorruptedError(
            in: container,
            debugDescription: "Unsupported JSON value for AnyJSONValue"
        )
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    /// Convenience accessor for objects only.
    public subscript(key: String) -> AnyJSONValue? {
        if case .object(let dict) = self { return dict[key] }
        return nil
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var intValue: Int? {
        if case .int(let value) = self { return value }
        if case .double(let value) = self, value.truncatingRemainder(dividingBy: 1) == 0 {
            return Int(value)
        }
        return nil
    }

    public var boolValue: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }
}
