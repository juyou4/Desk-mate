import XCTest
@testable import DeskmateCore

final class ChatHistoryBufferTests: XCTestCase {
    private func chatBubble(
        _ id: String, text: String, kind: BubbleKind = .chat
    ) -> BubbleSpec {
        BubbleSpec(id: id, kind: kind, text: text)
    }

    func testRecordUserMessageAppendsTrimmedEntry() {
        let buf = ChatHistoryBuffer()
        buf.recordUserMessage("  hello  ", at: 100)
        XCTAssertEqual(buf.entries.count, 1)
        XCTAssertEqual(buf.entries[0].role, .user)
        XCTAssertEqual(buf.entries[0].text, "hello")
        XCTAssertEqual(buf.entries[0].timestampMs, 100)
    }

    func testRecordUserMessageIgnoresWhitespaceOnly() {
        let buf = ChatHistoryBuffer()
        buf.recordUserMessage("", at: 100)
        buf.recordUserMessage("   \n\t", at: 200)
        XCTAssertTrue(buf.entries.isEmpty)
    }

    func testRecordBubbleChatKindAppendsPetEntry() {
        let buf = ChatHistoryBuffer()
        let recorded = buf.recordBubbleIfChatLike(
            chatBubble("b-1", text: "hi there"), at: 100
        )
        XCTAssertTrue(recorded)
        XCTAssertEqual(buf.entries.count, 1)
        XCTAssertEqual(buf.entries[0].role, .pet)
        XCTAssertEqual(buf.entries[0].text, "hi there")
    }

    func testRecordBubbleIgnoresNonChatKind() {
        let buf = ChatHistoryBuffer()
        let bubble = BubbleSpec(id: "a1", kind: .approvalHint, text: "grant?")
        XCTAssertFalse(buf.recordBubbleIfChatLike(bubble, at: 0))
        XCTAssertTrue(buf.entries.isEmpty)
    }

    func testRecordBubbleIgnoresPlaceholderDots() {
        let buf = ChatHistoryBuffer()
        XCTAssertFalse(
            buf.recordBubbleIfChatLike(
                chatBubble("user-msg-ack", text: "…"), at: 100
            )
        )
        XCTAssertTrue(buf.entries.isEmpty)
    }

    func testRecordBubbleIgnoresEmptyTextAfterTrim() {
        let buf = ChatHistoryBuffer()
        XCTAssertFalse(
            buf.recordBubbleIfChatLike(
                chatBubble("b-1", text: "   "), at: 100
            )
        )
        XCTAssertTrue(buf.entries.isEmpty)
    }

    func testRecordBubbleDedupsSameIdAcrossRepeatedPeeks() {
        // The runtime calls recordBubble... on every queue change,
        // which means the same bubble fires several times until the
        // queue dismisses it. The buffer must record it exactly once.
        let buf = ChatHistoryBuffer()
        let b = chatBubble("user-msg-reply", text: "hey")
        XCTAssertTrue(buf.recordBubbleIfChatLike(b, at: 100))
        XCTAssertFalse(buf.recordBubbleIfChatLike(b, at: 110))
        XCTAssertFalse(buf.recordBubbleIfChatLike(b, at: 120))
        XCTAssertEqual(buf.entries.count, 1)
    }

    func testRecordBubbleAcceptsDifferentIdAfterPriorRecord() {
        let buf = ChatHistoryBuffer()
        XCTAssertTrue(
            buf.recordBubbleIfChatLike(
                chatBubble("r1", text: "first"), at: 100
            )
        )
        XCTAssertTrue(
            buf.recordBubbleIfChatLike(
                chatBubble("r2", text: "second"), at: 200
            )
        )
        XCTAssertEqual(buf.entries.map(\.text), ["first", "second"])
    }

    func testInterleavedUserAndPetPreservesOrder() {
        let buf = ChatHistoryBuffer()
        buf.recordUserMessage("hi", at: 1)
        _ = buf.recordBubbleIfChatLike(
            chatBubble("r1", text: "hello"), at: 2
        )
        buf.recordUserMessage("how are you", at: 3)
        _ = buf.recordBubbleIfChatLike(
            chatBubble("r2", text: "great"), at: 4
        )
        XCTAssertEqual(
            buf.entries.map { ($0.role, $0.text) }.map { "\($0.0.rawValue):\($0.1)" },
            ["user:hi", "pet:hello", "user:how are you", "pet:great"]
        )
    }

    func testMaxEntriesDropsOldestFirst() {
        let buf = ChatHistoryBuffer(maxEntries: 3)
        for i in 0..<5 {
            buf.recordUserMessage("m\(i)", at: i)
        }
        XCTAssertEqual(buf.entries.count, 3)
        XCTAssertEqual(buf.entries.map(\.text), ["m2", "m3", "m4"])
    }

    func testClearResetsEntriesAndDedupState() {
        let buf = ChatHistoryBuffer()
        buf.recordUserMessage("hi", at: 1)
        let b = chatBubble("r1", text: "hey")
        _ = buf.recordBubbleIfChatLike(b, at: 2)
        buf.clear()
        XCTAssertTrue(buf.entries.isEmpty)
        // Same bubble id can now be re-recorded because dedup state
        // was wiped too.
        XCTAssertTrue(buf.recordBubbleIfChatLike(b, at: 3))
    }
}
