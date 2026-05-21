import Foundation

/// Newline-delimited envelope framing (V10 L3-D4 / Phase 10a).
///
/// Mirrors the Python :class:`LineBuffer` + ``encode_envelope`` /
/// ``decode_envelope`` helpers so Swift can read and write the same
/// byte stream the agent produces:
///
/// * UTF-8 text
/// * one :class:`BridgeEnvelope` per line, terminated by a single ``\n``
/// * blank / whitespace-only lines are ignored on the decode path
///
/// The framing is deliberately stream-friendly: ``feed(_:)`` accumulates
/// partial data across reads, returning *only* complete lines. The
/// caller is responsible for invoking ``decode(_:)`` — identical to the
/// Python pattern where :meth:`LineBuffer.feed` yields raw lines and
/// :func:`decode_envelope` does the JSON step separately. A convenience
/// :meth:`feedEnvelopes(_:onError:)` combines both steps for clients
/// that don't care about the raw line.
public struct EnvelopeFraming: Sendable {
    public static let encoding: String.Encoding = .utf8
    /// Line terminator byte. Matches Python ``b"\n"``.
    public static let separator: UInt8 = 0x0A

    public enum Error: Swift.Error, Equatable {
        case notUTF8
        case emptyLine
        case decodeFailed(rawLine: String, message: String)
        case encodeFailed(message: String)
    }

    private var buffer = Data()

    public init() {}

    /// Number of bytes buffered but not yet terminated by ``\n``.
    public var pendingByteCount: Int { buffer.count }

    /// Append ``chunk`` to the internal buffer and return every *complete*
    /// line it produced. Empty or whitespace-only lines are filtered out
    /// so the caller never sees them.
    public mutating func feed(_ chunk: Data) -> [Data] {
        buffer.append(chunk)
        var lines: [Data] = []
        while let index = buffer.firstIndex(of: Self.separator) {
            let line = buffer.subdata(in: buffer.startIndex..<index)
            buffer.removeSubrange(buffer.startIndex...index)
            if Self.isWhitespaceOnly(line) { continue }
            lines.append(line)
        }
        return lines
    }

    /// Convenience: decode each complete line as a :class:`BridgeEnvelope`
    /// and report per-line failures via ``onError``. Malformed lines
    /// never poison the batch — good envelopes in the same chunk still
    /// get through.
    public mutating func feedEnvelopes(
        _ chunk: Data,
        onError: ((Error) -> Void)? = nil
    ) -> [BridgeEnvelope] {
        var results: [BridgeEnvelope] = []
        for line in feed(chunk) {
            do {
                results.append(try Self.decode(line))
            } catch let err as Error {
                onError?(err)
            } catch {
                onError?(.decodeFailed(rawLine: "", message: "\(error)"))
            }
        }
        return results
    }

    // MARK: - Static codec

    /// Encode one envelope as a single ``\n``-terminated UTF-8 payload.
    /// Round-trips through :meth:`decode(_:)` and interoperates with the
    /// Python ``encode_envelope``.
    public static func encode(
        _ envelope: BridgeEnvelope,
        using encoder: JSONEncoder = EnvelopeFraming.defaultEncoder()
    ) throws -> Data {
        do {
            var data = try encoder.encode(envelope)
            data.append(separator)
            return data
        } catch {
            throw Error.encodeFailed(message: "\(error)")
        }
    }

    /// Decode one line into a :class:`BridgeEnvelope`. Accepts leading /
    /// trailing whitespace within the line but throws ``emptyLine`` if
    /// the line is whitespace-only.
    public static func decode(
        _ line: Data,
        using decoder: JSONDecoder = JSONDecoder()
    ) throws -> BridgeEnvelope {
        guard let raw = String(data: line, encoding: encoding) else {
            throw Error.notUTF8
        }
        let trimmed = raw.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            throw Error.emptyLine
        }
        let body = Data(trimmed.utf8)
        do {
            return try decoder.decode(BridgeEnvelope.self, from: body)
        } catch {
            throw Error.decodeFailed(rawLine: trimmed, message: "\(error)")
        }
    }

    /// JSON encoder pre-configured to match Python's compact, UTF-8,
    /// non-escaping output (``ensure_ascii=False``).
    public static func defaultEncoder() -> JSONEncoder {
        let e = JSONEncoder()
        // Python uses "(\",\", \":\")" separators → no whitespace. Swift
        // doesn't emit whitespace by default either, so no config needed
        // beyond leaving off .prettyPrinted.
        e.outputFormatting = [.withoutEscapingSlashes]
        return e
    }

    // MARK: - Internals

    /// Returns true if ``data`` is empty or contains only
    /// ASCII whitespace (space, tab, CR). Mirrors ``line.strip()``
    /// semantics on a byte slice so we don't allocate a String.
    private static func isWhitespaceOnly(_ data: Data) -> Bool {
        for byte in data {
            switch byte {
            case 0x20, 0x09, 0x0D:  // space, tab, CR
                continue
            default:
                return false
            }
        }
        return true
    }
}
