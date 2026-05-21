import Foundation
import os

/// Structured logging for DeskmateCore (V10 L3 Instrumentation).
///
/// Every user-triggered event carries a ``trace_id`` through the bridge. On
/// the Swift side we propagate it via a ``@TaskLocal`` so async children
/// inherit the value automatically. Log call sites read the current trace_id
/// off ``DeskmateLog`` rather than threading it by hand.
public enum DeskmateLog {
    public static let subsystem = "com.deskmate.core"

    /// Current trace_id for the active task. Mutable only through ``withTraceId``.
    @TaskLocal public static var traceId: String?

    /// Execute ``body`` with ``traceId`` bound, restoring the previous value on exit.
    public static func withTraceId<T>(
        _ value: String?,
        operation body: () throws -> T
    ) rethrows -> T {
        try DeskmateLog.$traceId.withValue(value, operation: body)
    }

    /// Async variant.
    public static func withTraceId<T>(
        _ value: String?,
        operation body: () async throws -> T
    ) async rethrows -> T {
        try await DeskmateLog.$traceId.withValue(value, operation: body)
    }

    /// Construct a ``Logger`` scoped to the given category.
    public static func logger(category: String) -> Logger {
        Logger(subsystem: subsystem, category: category)
    }
}
