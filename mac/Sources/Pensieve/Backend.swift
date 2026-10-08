import Foundation

/// The Python backend (`pensieve serve`): finds or starts it, reads its settings, and runs searches.
@MainActor
final class Backend: ObservableObject {
    static let shared = Backend()

    enum State: Equatable {
        case checking, starting, up, missing
        case failed(String)
    }

    @Published private(set) var state: State = .checking
    @Published private(set) var settings: [String: Any] = [:]
    var onSettingsChange: (() -> Void)?

    let base: URL
    /// Set when PENSIEVE_URL points somewhere else (dev, tests): never spawn a backend then, just wait for it.
    private let external: Bool
    private var child: Process?
    private var events: Task<Void, Never>?

    private init() {
        let env = ProcessInfo.processInfo.environment
        external = env["PENSIEVE_URL"] != nil || env["PENSIEVE_NO_SPAWN"] != nil
        var s = env["PENSIEVE_URL"] ?? "http://127.0.0.1:8765"
        while s.hasSuffix("/") { s.removeLast() }
        base = URL(string: s)!
    }

    var hotkey: String { (settings["hotkey"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "ctrl+shift" }
    var editor: String { (settings["editor"] as? String ?? "").lowercased() }

    func url(_ path: String, _ query: [URLQueryItem] = [], fragment: String? = nil) -> URL {
        var c = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            c.queryItems = query
            // URLComponents leaves '+' alone, but form decoding on the server reads it as a space.
            c.percentEncodedQuery = c.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        }
        if let fragment { c.percentEncodedFragment = fragment }
        return c.url!
    }

    func isLocal(_ u: URL) -> Bool {
        u.host == base.host && (u.port ?? 80) == (base.port ?? 80)
    }

    // MARK: lifecycle

    func start() {
        Task {
            if await isUp() { return await becameUp() }
            if !external, let bin = Self.findBinary() {
                spawn(bin)
            } else {
                state = external ? .starting : .missing
            }
            while true {
                try? await Task.sleep(for: .seconds(1))
                if await isUp() { return await becameUp() }
                if let c = child, !c.isRunning {
                    state = .failed("The backend exited (code \(c.terminationStatus)). See ~/.pensieve/server.log")
                    child = nil
                }
            }
        }
    }

    func stop() {
        guard let c = child, c.isRunning else { return }
        c.terminate()
        let deadline = Date().addingTimeInterval(3)
        while c.isRunning && Date() < deadline { usleep(50_000) }
        if c.isRunning { kill(c.processIdentifier, SIGKILL) }
    }

    private func becameUp() async {
        state = .up
        await loadSettings()
        listen()
    }

    private func isUp() async -> Bool {
        var req = URLRequest(url: url("api/status"))
        req.timeoutInterval = 2
        guard let (_, r) = try? await URLSession.shared.data(for: req) else { return false }
        return (r as? HTTPURLResponse)?.statusCode == 200
    }

    static func findBinary() -> String? {
        let home = NSHomeDirectory()
        let candidates = [ProcessInfo.processInfo.environment["PENSIEVE_BIN"], "\(home)/.local/bin/pensieve",
                          "/opt/homebrew/bin/pensieve", "/usr/local/bin/pensieve"]
        return candidates.compactMap { $0 }.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func spawn(_ bin: String) {
        let home = NSHomeDirectory()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bin)
        p.arguments = ["serve", "--no-open"]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        p.environment = env
        let dir = "\(home)/.pensieve"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let log = "\(dir)/server.log"
        if !FileManager.default.fileExists(atPath: log) { FileManager.default.createFile(atPath: log, contents: nil) }
        if let h = FileHandle(forWritingAtPath: log) {
            h.seekToEndOfFile()
            p.standardOutput = h
            p.standardError = h
        }
        do {
            try p.run()
            child = p
            state = .starting
        } catch {
            state = .failed("Couldn't start \(bin): \(error.localizedDescription)")
        }
    }

    // MARK: settings

    func loadSettings() async {
        guard let (d, r) = try? await URLSession.shared.data(from: url("api/settings")),
              (r as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
        settings = obj["settings"] as? [String: Any] ?? obj  // {"settings": {...}, "defaults": ..., "descriptions": ...}
        onSettingsChange?()
    }

    /// Follow the backend's event stream; a {"type": "settings"} event (e.g. an agent changed settings over MCP)
    /// re-reads them, and every reconnect re-reads them too.
    private func listen() {
        events?.cancel()
        events = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    var req = URLRequest(url: self.url("api/events"))
                    req.timeoutInterval = 120
                    let (bytes, _) = try await URLSession.shared.bytes(for: req)
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: "), let d = line.dropFirst(6).data(using: .utf8),
                              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { continue }
                        if obj["type"] as? String == "settings" { await self.loadSettings() }
                    }
                } catch {}
                try? await Task.sleep(for: .seconds(3))
                if await self.isUp() {
                    if self.state != .up { self.state = .up }
                    await self.loadSettings()
                } else if self.state == .up {
                    self.state = .starting
                }
            }
        }
    }

    // MARK: search

    func find(_ q: String, limit: Int = 20) async throws -> FindResponse {
        let u = url("api/find", [URLQueryItem(name: "q", value: q), URLQueryItem(name: "limit", value: String(limit))])
        let (d, r) = try await URLSession.shared.data(from: u)
        guard let code = (r as? HTTPURLResponse)?.statusCode, code == 200 else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(FindResponse.self, from: d)
    }
}
