#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif
import Foundation

/// Unix domain socket client for the agent bridge (V10 Phase 10b /
/// L3-D3 + L3-D4).
///
/// Reads newline-delimited JSON envelopes from the socket, feeds them
/// through :class:`EnvelopeFraming`, and fans decoded
/// :class:`BridgeEnvelope` values to a single handler closure. Writes
/// encoded envelopes with ``send(_:)``.
///
/// The client deliberately stays **minimal**:
///
/// * single connection attempt (no reconnect — that's a future wrapper)
/// * no authentication / TLS (Unix socket on the same user session)
/// * state machine is just ``disconnected / connected / failed``
///
/// Testability: ``start(preConnectedFd:)`` accepts a file descriptor
/// returned by ``socketpair(2)``, so integration tests don't need the
/// filesystem. Ownership of that fd transfers to the client; ``stop()``
/// closes it.
public final class BridgeClient {

    // MARK: - Types

    public enum State: Equatable, Sendable {
        case disconnected
        case connected
        case failed(String)
    }

    public enum Error: Swift.Error, Sendable {
        case alreadyStarted
        case notConnected
        case noSocketPath
        case socketFailed(String)
        case connectFailed(String)
        case writeFailed(code: Int32, message: String)
    }

    public struct Configuration: Sendable {
        public var socketPath: String?
        public var readBufferBytes: Int

        public init(
            socketPath: String? = nil, readBufferBytes: Int = 4096
        ) {
            self.socketPath = socketPath
            self.readBufferBytes = readBufferBytes
        }
    }

    // MARK: - State

    public let configuration: Configuration
    private let ioQueue: DispatchQueue
    private let callbackQueue: DispatchQueue

    private var fd: Int32 = -1
    private var readSource: DispatchSourceRead?
    private var framing = EnvelopeFraming()
    private var _state: State = .disconnected

    private var envelopeHandlers: [UUID: (BridgeEnvelope) -> Void] = [:]
    private var stateHandler: ((State) -> Void)?
    private var decodeErrorHandler: ((EnvelopeFraming.Error) -> Void)?

    public var state: State {
        ioQueue.sync { _state }
    }

    // MARK: - Init

    public init(
        configuration: Configuration = .init(),
        callbackQueue: DispatchQueue = .main
    ) {
        self.configuration = configuration
        self.callbackQueue = callbackQueue
        self.ioQueue = DispatchQueue(
            label: "deskmate.bridge.client",
            qos: .userInitiated
        )
    }

    // MARK: - Lifecycle

    /// Open the socket and install the read source.
    ///
    /// - Parameter preConnectedFd: An already-open fd (e.g. one end of
    ///   ``socketpair(2)``). When supplied, the configured
    ///   ``socketPath`` is ignored. Ownership transfers to the client.
    public func start(preConnectedFd: Int32? = nil) throws {
        try ioQueue.sync {
            guard fd == -1 else { throw Error.alreadyStarted }
            let openedFd: Int32
            if let pre = preConnectedFd {
                openedFd = pre
            } else {
                guard let path = configuration.socketPath else {
                    throw Error.noSocketPath
                }
                openedFd = try Self.connect(to: path)
            }
            self.fd = openedFd
            _ = Self.setNonBlocking(fd: openedFd)
            setStateLocked(.connected)
            installReadSourceLocked()
        }
    }

    /// Tear down the connection. Safe to call more than once.
    public func stop() {
        ioQueue.sync { teardownLocked(finalState: .disconnected) }
    }

    // MARK: - Send

    /// Encode + write the envelope. Errors include any framing encode
    /// failure or a POSIX ``write(2)`` failure.
    public func send(_ envelope: BridgeEnvelope) throws {
        let data = try EnvelopeFraming.encode(envelope)
        try ioQueue.sync {
            guard fd >= 0 else { throw Error.notConnected }
            try Self.writeAll(fd: fd, data: data)
        }
    }

    // MARK: - Handlers

    /// Register a handler for every envelope the bridge decodes. Multiple
    /// subscribers are supported — each call appends; the returned closure
    /// removes *that* registration. The result is ``@discardableResult``
    /// so existing "fire-and-forget" call sites compile unchanged.
    @discardableResult
    public func onEnvelope(
        _ cb: @escaping (BridgeEnvelope) -> Void
    ) -> () -> Void {
        let id = UUID()
        ioQueue.sync { envelopeHandlers[id] = cb }
        return { [weak self] in
            self?.ioQueue.async {
                self?.envelopeHandlers.removeValue(forKey: id)
            }
        }
    }

    public func onStateChange(_ cb: @escaping (State) -> Void) {
        ioQueue.sync { stateHandler = cb }
    }

    public func onDecodeError(
        _ cb: @escaping (EnvelopeFraming.Error) -> Void
    ) {
        ioQueue.sync { decodeErrorHandler = cb }
    }

    // MARK: - Internals (must be called on ioQueue)

    private func installReadSourceLocked() {
        let src = DispatchSource.makeReadSource(
            fileDescriptor: fd, queue: ioQueue
        )
        src.setEventHandler { [weak self] in self?.readAvailable() }
        readSource = src
        src.resume()
    }

    private func readAvailable() {
        // Runs on ioQueue.
        guard fd >= 0 else { return }
        let bufSize = configuration.readBufferBytes
        var buffer = [UInt8](repeating: 0, count: bufSize)
        let n: Int = buffer.withUnsafeMutableBufferPointer { ptr in
            #if canImport(Darwin)
            return Darwin.read(fd, ptr.baseAddress, ptr.count)
            #else
            return Glibc.read(fd, ptr.baseAddress, ptr.count)
            #endif
        }
        if n > 0 {
            let chunk = Data(bytes: buffer, count: n)
            let handlers = self.envelopeHandlers
            let decodeErrorHandler = self.decodeErrorHandler
            let callbackQueue = self.callbackQueue
            let envelopes = framing.feedEnvelopes(chunk) { err in
                callbackQueue.async { decodeErrorHandler?(err) }
            }
            for env in envelopes {
                callbackQueue.async {
                    for cb in handlers.values { cb(env) }
                }
            }
            return
        }
        if n == 0 {
            // EOF — the peer closed.
            teardownLocked(finalState: .disconnected)
            return
        }
        // n < 0 → consult errno. EAGAIN / EWOULDBLOCK is OK on
        // non-blocking sockets; otherwise it's a real failure.
        let code = errno
        if code == EAGAIN || code == EWOULDBLOCK || code == EINTR {
            return
        }
        let message = String(cString: strerror(code))
        teardownLocked(finalState: .failed("read: \(message)"))
    }

    private func teardownLocked(finalState: State) {
        readSource?.cancel()
        readSource = nil
        if fd >= 0 {
            #if canImport(Darwin)
            _ = Darwin.close(fd)
            #else
            _ = Glibc.close(fd)
            #endif
            fd = -1
        }
        framing = EnvelopeFraming()
        setStateLocked(finalState)
    }

    private func setStateLocked(_ s: State) {
        guard _state != s else { return }
        _state = s
        let handler = stateHandler
        callbackQueue.async { handler?(s) }
    }

    // MARK: - POSIX helpers

    static func setNonBlocking(fd: Int32) -> Int32 {
        #if canImport(Darwin)
        let current = fcntl(fd, F_GETFL, 0)
        return fcntl(fd, F_SETFL, current | O_NONBLOCK)
        #else
        let current = fcntl(fd, F_GETFL, 0)
        return fcntl(fd, F_SETFL, current | O_NONBLOCK)
        #endif
    }

    static func connect(to path: String) throws -> Int32 {
        #if canImport(Darwin)
        let rawFd = socket(AF_UNIX, SOCK_STREAM, 0)
        #else
        let rawFd = socket(AF_UNIX, Int32(SOCK_STREAM.rawValue), 0)
        #endif
        guard rawFd >= 0 else {
            throw Error.socketFailed(
                String(cString: strerror(errno))
            )
        }

        var addr = sockaddr_un()
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
        let pathBytes = Array(path.utf8)
        guard pathBytes.count < maxLen else {
            #if canImport(Darwin)
            _ = Darwin.close(rawFd)
            #else
            _ = Glibc.close(rawFd)
            #endif
            throw Error.socketFailed("socket path too long (\(pathBytes.count) bytes)")
        }
        #if canImport(Darwin)
        addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        #endif
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &addr.sun_path) { rawPtr in
            rawPtr.withMemoryRebound(to: CChar.self, capacity: maxLen) { buf in
                for (i, byte) in pathBytes.enumerated() {
                    buf[i] = CChar(bitPattern: byte)
                }
                buf[pathBytes.count] = 0
            }
        }

        let connectResult = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                #if canImport(Darwin)
                return Darwin.connect(
                    rawFd, sa, socklen_t(MemoryLayout<sockaddr_un>.size)
                )
                #else
                return Glibc.connect(
                    rawFd, sa, socklen_t(MemoryLayout<sockaddr_un>.size)
                )
                #endif
            }
        }
        if connectResult < 0 {
            let code = errno
            #if canImport(Darwin)
            _ = Darwin.close(rawFd)
            #else
            _ = Glibc.close(rawFd)
            #endif
            throw Error.connectFailed(
                String(cString: strerror(code))
            )
        }
        return rawFd
    }

    static func writeAll(fd: Int32, data: Data) throws {
        try data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var offset = 0
            while offset < data.count {
                let remaining = data.count - offset
                #if canImport(Darwin)
                let n = Darwin.write(fd, base.advanced(by: offset), remaining)
                #else
                let n = Glibc.write(fd, base.advanced(by: offset), remaining)
                #endif
                if n < 0 {
                    let code = errno
                    if code == EINTR { continue }
                    throw Error.writeFailed(
                        code: code,
                        message: String(cString: strerror(code))
                    )
                }
                offset += n
            }
        }
    }
}

// MARK: - Test utilities

extension BridgeClient {
    /// Create a connected ``socketpair(2)`` and return both ends. Use
    /// one for the test harness (simulating the agent) and pass the
    /// other to :meth:`start(preConnectedFd:)`.
    ///
    /// Returns ``(agentFd, clientFd)``; close the agent fd yourself,
    /// the client fd becomes owned by the ``BridgeClient``.
    public static func makeTestSocketPair() -> (agentFd: Int32, clientFd: Int32) {
        var fds: [Int32] = [0, 0]
        let status = fds.withUnsafeMutableBufferPointer { buf -> Int32 in
            #if canImport(Darwin)
            return socketpair(AF_UNIX, SOCK_STREAM, 0, buf.baseAddress)
            #else
            return socketpair(
                AF_UNIX, Int32(SOCK_STREAM.rawValue), 0, buf.baseAddress
            )
            #endif
        }
        precondition(status == 0, "socketpair(2) failed: \(errno)")
        return (fds[0], fds[1])
    }
}
