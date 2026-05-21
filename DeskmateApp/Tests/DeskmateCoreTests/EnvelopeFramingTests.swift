import XCTest
@testable import DeskmateCore

final class EnvelopeFramingTests: XCTestCase {
    // MARK: - Helpers

    private func pingLine(trace: String = "abc") -> Data {
        let body = #"""
        {"spec_version":1,"type":"ping","trace_id":"\#(trace)","payload":{}}
        """#
        return Data(body.utf8) + Data([EnvelopeFraming.separator])
    }

    // MARK: - feed

    func testFeedSingleCompleteLineYieldsOneEnvelope() throws {
        var framing = EnvelopeFraming()
        let chunk = pingLine()
        let envelopes = framing.feedEnvelopes(chunk)
        XCTAssertEqual(envelopes.count, 1)
        XCTAssertEqual(envelopes.first?.type, .ping)
        XCTAssertEqual(framing.pendingByteCount, 0)
    }

    func testFeedMultipleLinesInOneChunk() throws {
        var framing = EnvelopeFraming()
        var chunk = pingLine(trace: "a")
        chunk.append(pingLine(trace: "b"))
        chunk.append(pingLine(trace: "c"))
        let envelopes = framing.feedEnvelopes(chunk)
        XCTAssertEqual(envelopes.map(\.traceId), ["a", "b", "c"])
    }

    func testFeedAssemblesPartialChunks() throws {
        var framing = EnvelopeFraming()
        let full = pingLine(trace: "x")
        let split = full.count / 2
        let first = framing.feedEnvelopes(full.prefix(split))
        XCTAssertTrue(first.isEmpty)
        XCTAssertGreaterThan(framing.pendingByteCount, 0)
        let second = framing.feedEnvelopes(full.suffix(from: split))
        XCTAssertEqual(second.count, 1)
        XCTAssertEqual(second[0].traceId, "x")
        XCTAssertEqual(framing.pendingByteCount, 0)
    }

    func testEmptyLinesBetweenValidLinesAreIgnored() throws {
        var framing = EnvelopeFraming()
        var chunk = Data("\n\n".utf8)
        chunk.append(pingLine(trace: "a"))
        chunk.append(Data("   \n".utf8))  // whitespace-only line
        chunk.append(pingLine(trace: "b"))
        let envelopes = framing.feedEnvelopes(chunk)
        XCTAssertEqual(envelopes.map(\.traceId), ["a", "b"])
    }

    func testTrailingPartialLineStaysBuffered() {
        var framing = EnvelopeFraming()
        let complete = pingLine(trace: "a")
        let partial = Data(#"{"spec_version":1,"type":"p"#.utf8)
        let chunk = complete + partial
        let envelopes = framing.feedEnvelopes(chunk)
        XCTAssertEqual(envelopes.count, 1)
        XCTAssertEqual(framing.pendingByteCount, partial.count)
    }

    func testInvalidJSONReportsErrorButKeepsGoodEnvelopes() throws {
        var framing = EnvelopeFraming()
        var chunk = pingLine(trace: "good-1")
        chunk.append(Data("{not-json}\n".utf8))
        chunk.append(pingLine(trace: "good-2"))
        var errors: [EnvelopeFraming.Error] = []
        let envelopes = framing.feedEnvelopes(chunk) { errors.append($0) }
        XCTAssertEqual(envelopes.map(\.traceId), ["good-1", "good-2"])
        XCTAssertEqual(errors.count, 1)
        if case .decodeFailed = errors[0] {
            // ok
        } else {
            XCTFail("expected .decodeFailed, got \(errors[0])")
        }
    }

    // MARK: - encode / decode round-trip

    func testEncodeAppendsNewline() throws {
        let env = BridgeEnvelope.of(.ping, traceId: "t1")
        let data = try EnvelopeFraming.encode(env)
        XCTAssertEqual(data.last, EnvelopeFraming.separator)
    }

    func testEncodeDecodeRoundTripThroughFraming() throws {
        let env = BridgeEnvelope.of(
            .userMessage,
            payload: ["text": .string("hi")],
            traceId: "trace-roundtrip"
        )
        let encoded = try EnvelopeFraming.encode(env)
        var framing = EnvelopeFraming()
        let out = framing.feedEnvelopes(encoded)
        XCTAssertEqual(out.count, 1)
        XCTAssertEqual(out[0].traceId, "trace-roundtrip")
        XCTAssertEqual(out[0].payload["text"], .string("hi"))
    }

    func testUTF8MultibytePayloadSurvives() throws {
        let env = BridgeEnvelope.of(
            .userMessage,
            payload: ["text": .string("你好,世界 🌏")],
            traceId: "utf-8"
        )
        let encoded = try EnvelopeFraming.encode(env)
        var framing = EnvelopeFraming()
        let out = framing.feedEnvelopes(encoded)
        XCTAssertEqual(out.count, 1)
        XCTAssertEqual(out[0].payload["text"], .string("你好,世界 🌏"))
    }

    func testDecodeEmptyLineThrowsEmptyLine() {
        XCTAssertThrowsError(try EnvelopeFraming.decode(Data())) { error in
            guard case EnvelopeFraming.Error.emptyLine = error else {
                return XCTFail("expected .emptyLine, got \(error)")
            }
        }
    }

    func testDecodeWhitespaceLineThrowsEmptyLine() {
        let line = Data("   \t  ".utf8)
        XCTAssertThrowsError(try EnvelopeFraming.decode(line)) { error in
            guard case EnvelopeFraming.Error.emptyLine = error else {
                return XCTFail("expected .emptyLine, got \(error)")
            }
        }
    }

    func testDecodeInvalidJSONThrowsDecodeFailed() {
        let line = Data("{not valid}".utf8)
        XCTAssertThrowsError(try EnvelopeFraming.decode(line)) { error in
            guard case EnvelopeFraming.Error.decodeFailed(let raw, _) = error else {
                return XCTFail("expected .decodeFailed, got \(error)")
            }
            XCTAssertEqual(raw, "{not valid}")
        }
    }

    func testOneCharAtATimeStreamingStillCompletes() throws {
        var framing = EnvelopeFraming()
        let full = pingLine(trace: "stream")
        var seen: [BridgeEnvelope] = []
        for byte in full {
            seen.append(contentsOf: framing.feedEnvelopes(Data([byte])))
        }
        XCTAssertEqual(seen.count, 1)
        XCTAssertEqual(seen[0].traceId, "stream")
        XCTAssertEqual(framing.pendingByteCount, 0)
    }
}
