import CryptoKit
import Foundation

/// The Python backend (`pensieve serve`): finds or starts it, reads its settings, and runs searches.
@MainActor
final class Backend: ObservableObject {
    static let shared = Backend()

    enum State: Equatable {
        case checking, starting, up, missing
        case installing(String)
        case failed(String)
    }

    @Published private(set) var state: State = .checking
    @Published private(set) var settings: [String: Any] = [:]
    /// What is being indexed now (GET /api/syncing): empty when idle.
    @Published private(set) var syncing: [SyncJob] = []
    /// The backend does background work (GET /api/status, see statusBusy).
    @Published private(set) var busy = false
    var onSettingsChange: (() -> Void)?

    /// One running indexing job: `total` and `seconds_left` are known for repos and a search-model switch, not for
    /// documents or sessions (they are found by walking folders).
    struct SyncJob: Decodable, Equatable {
        let kind: String  // "repo" | "files" | "sessions" | "model"
        let name: String
        let done: Int
        let total: Int?
        let seconds_left: Int?

        var label: String {
            let left = seconds_left.map { " · \(Self.timeLeft($0))" } ?? ""
            if kind == "model" {
                let pct = total.map { $0 > 0 ? " · \(100 * done / $0)%" : "" } ?? ""
                return "Switching to \(name)\(pct)\(left)"
            }
            let what = switch kind {
            case "repo": "Code in \(name)"
            case "files": "Documents in \(name)"
            default: "\(["claude": "Claude Code", "qwen": "Qwen Code", "codex": "Codex"][name] ?? name) sessions"
            }
            let n = done.formatted()
            if let total { return "\(what) · \(n) of \(total.formatted()) files\(left)" }
            return "\(what) · \(n) \(kind == "sessions" ? "sessions" : "files") so far"
        }

        static func timeLeft(_ s: Int) -> String {
            if s < 60 { return "less than a minute left" }
            let m = (s + 30) / 60
            return m < 60 ? "about \(m) min left" : "about \(m / 60) h \(m % 60) min left"
        }
    }

    let base: URL
    /// Set when PENSIEVE_URL points somewhere else (dev, tests): never spawn a backend then, just wait for it.
    private let external: Bool
    private let port: Int
    private var child: Process?
    private var events: Task<Void, Never>?
    private var syncPoll: Task<Void, Never>?

    private init() {
        let env = ProcessInfo.processInfo.environment
        external = env["PENSIEVE_URL"] != nil || env["PENSIEVE_NO_SPAWN"] != nil
        port = Int(env["PENSIEVE_PORT"] ?? "") ?? 8765
        var s = env["PENSIEVE_URL"] ?? "http://127.0.0.1:\(port)"
        while s.hasSuffix("/") { s.removeLast() }
        base = URL(string: s)!
    }

    /// Check GitHub for a newer release once a day (the user still chooses whether to install).
    var autoUpdateCheck: Bool { settings["auto_update_check"] as? Bool ?? true }
    /// Threads mode is the backend's "session" mode, its default: only agent sessions, as threads.
    var threadsMode: Bool { settings["mode"] as? String ?? "session" != "developer" }

    /// "system" | "light" | "dark". The backend setting wins; the last value is remembered locally so the app
    /// starts in the right appearance before the backend is up (and works with a backend that lacks the key).
    var appearance: String {
        if let a = settings["appearance"] as? String, ["system", "light", "dark"].contains(a) { return a }
        return Prefs.store.string(forKey: "appearance") ?? "system"
    }

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
            var replaced = false
            if await isUp() {
                // a backend left running from an older install would keep serving its old UI and API after an update
                guard !external, let wheel = Self.bundledWheel(), let running = await runningVersion(),
                      Self.isOlder(running, than: Self.version(of: wheel)), await stopRunningBackend() else {
                    return await becameUp()
                }
                replaced = true  // the stamp file can't be trusted: something else installed that old version
            }
            if !external, let wheel = Self.bundledWheel(), replaced || Self.installedVersion() != Self.stamp(wheel) {
                // first launch (or the app was updated): install the matching backend from inside the app bundle
                state = .installing(Self.findBinary() == nil
                    ? "Setting up Pensieve. The first launch downloads about 1 GB; this takes a few minutes."
                    : "Updating Pensieve…")
                if let err = await Self.install(wheel) {
                    state = .failed(err)
                    return
                }
            }
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
        pollSyncing()
    }

    /// A long index sends no events while it runs, so the menu bar polls: every second while something is
    /// indexing, every 3 s otherwise. A backend without /api/syncing (older) reads as idle.
    private func pollSyncing() {
        syncPoll?.cancel()
        syncPoll = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                var jobs: [SyncJob] = []
                if self.state == .up, let (d, r) = try? await URLSession.shared.data(from: self.url("api/syncing")),
                   (r as? HTTPURLResponse)?.statusCode == 200,
                   let res = try? JSONDecoder().decode([String: [SyncJob]].self, from: d) {
                    jobs = res["syncing"] ?? []
                }
                if jobs != self.syncing { self.syncing = jobs }
                let busy = self.state == .up ? await self.statusBusy() : false
                if busy != self.busy { self.busy = busy }
                try? await Task.sleep(for: .seconds(jobs.isEmpty && !busy ? 3 : 1))
            }
        }
    }

    /// Busy: layout, insights or indexing are in progress. An error stops this work, so it is not busy. Session
    /// summaries and topic names wait for the user's agent, so they do not count.
    private func statusBusy() async -> Bool {
        guard let (d, r) = try? await URLSession.shared.data(from: url("api/status")),
              (r as? HTTPURLResponse)?.statusCode == 200,
              let st = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return false }
        let on = { (k: String) in st[k] as? Bool ?? false }
        if (st["message"] as? String ?? "").hasPrefix("error") { return false }
        return on("layout") || on("insights_running") || on("indexing")
    }

    private func isUp() async -> Bool {
        var req = URLRequest(url: url("api/status"))
        req.timeoutInterval = 2
        guard let (_, r) = try? await URLSession.shared.data(for: req) else { return false }
        return (r as? HTTPURLResponse)?.statusCode == 200
    }

    private func runningVersion() async -> String? {
        guard let (d, r) = try? await URLSession.shared.data(from: url("api/about")),
              (r as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return nil }
        return obj["version"] as? String
    }

    /// "pensieve-0.5.6-py3-none-any.whl" -> "0.5.6"
    static func version(of wheel: URL) -> String {
        let parts = wheel.lastPathComponent.split(separator: "-")
        return parts.count > 1 ? String(parts[1]) : ""
    }

    /// Numeric dotted versions only: "dev" or anything unparsable is never treated as older.
    static func isOlder(_ a: String, than b: String) -> Bool {
        let pa = a.split(separator: ".").map { Int($0) }, pb = b.split(separator: ".").map { Int($0) }
        guard !pa.isEmpty, !pb.isEmpty, !pa.contains(nil), !pb.contains(nil) else { return false }
        for i in 0..<max(pa.count, pb.count) {
            let x = i < pa.count ? pa[i]! : 0, y = i < pb.count ? pb[i]! : 0
            if x != y { return x < y }
        }
        return false
    }

    /// Stops the `pensieve serve` that listens on our port (not one this app started). True once the port is free.
    private func stopRunningBackend() async -> Bool {
        let port = self.port
        let pids: [Int32] = await Task.detached {
            func run(_ tool: String, _ args: [String]) -> String {
                let p = Process(), out = Pipe()
                p.executableURL = URL(fileURLWithPath: tool)
                p.arguments = args
                p.standardOutput = out
                p.standardError = FileHandle.nullDevice
                guard (try? p.run()) != nil else { return "" }
                p.waitUntilExit()
                return String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            }
            return run("/usr/sbin/lsof", ["-nP", "-iTCP:\(port)", "-sTCP:LISTEN", "-t"])
                .split(separator: "\n").compactMap { Int32($0) }
                .filter { pid in
                    let cmd = run("/bin/ps", ["-o", "command=", "-p", String(pid)])
                    return cmd.contains("pensieve") && cmd.contains("serve")
                }
        }.value
        guard !pids.isEmpty else { return false }
        state = .installing("Updating Pensieve…")
        pids.forEach { kill($0, SIGTERM) }
        for _ in 0..<50 {
            try? await Task.sleep(for: .milliseconds(200))
            if !(await isUp()) { return true }
        }
        pids.forEach { kill($0, SIGKILL) }
        try? await Task.sleep(for: .milliseconds(500))
        return !(await isUp())
    }

    // MARK: bundled installer (the DMG build puts uv and the backend wheel in Contents/Resources/backend)

    static var bundleBackend: URL? { Bundle.main.resourceURL?.appendingPathComponent("backend") }

    static func bundledWheel() -> URL? {
        guard let dir = bundleBackend,
              let items = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return nil }
        return items.first { $0.pathExtension == "whl" }
    }

    static var versionFile: String { "\(NSHomeDirectory())/.pensieve/backend-version" }

    /// Identifies the bundled backend build: file name plus content hash, so a rebuilt app reinstalls.
    static func stamp(_ wheel: URL) -> String {
        let digest = (try? Data(contentsOf: wheel)).map { SHA256.hash(data: $0).prefix(8).map { String(format: "%02x", $0) }.joined() } ?? ""
        return "\(wheel.lastPathComponent) \(digest)"
    }

    static func installedVersion() -> String? {
        guard findBinary() != nil else { return nil }
        return (try? String(contentsOfFile: versionFile, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// `uv tool install` the bundled wheel (Python 3.12 and dependencies come from the internet). nil on success.
    static func install(_ wheel: URL) async -> String? {
        guard let uv = bundleBackend?.appendingPathComponent("uv"), FileManager.default.isExecutableFile(atPath: uv.path) else {
            return "The app is missing its installer. Reinstall Pensieve from the DMG."
        }
        let home = NSHomeDirectory()
        try? FileManager.default.createDirectory(atPath: "\(home)/.pensieve", withIntermediateDirectories: true)
        let log = "\(home)/.pensieve/install.log"
        FileManager.default.createFile(atPath: log, contents: nil)
        let status: Int32 = await Task.detached {
            let p = Process()
            p.executableURL = uv
            p.arguments = ["tool", "install", "--force", "--python", "3.12", wheel.path]
            var env = ProcessInfo.processInfo.environment
            env["UV_TOOL_BIN_DIR"] = "\(home)/.local/bin"
            p.environment = env
            if let h = FileHandle(forWritingAtPath: log) { p.standardOutput = h; p.standardError = h }
            do { try p.run() } catch { return -1 }
            p.waitUntilExit()
            return p.terminationStatus
        }.value
        guard status == 0, FileManager.default.isExecutableFile(atPath: "\(home)/.local/bin/pensieve") else {
            return "Setup failed (see ~/.pensieve/install.log). Check your internet connection and reopen Pensieve."
        }
        try? stamp(wheel).write(toFile: versionFile, atomically: true, encoding: .utf8)
        return nil
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
        p.arguments = ["serve", "--no-open", "--port", String(port)]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        // the video and sound decoder that this app carries (pensieve/media.py)
        env["PENSIEVE_MEDIA_TOOL"] = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/pensieve-media").path
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

    /// Tests only (PENSIEVE_DEBUG): show a backend state, e.g. the first-launch setup message.
    func debugSetState(_ s: State) {
        if Debug.enabled { state = s }
    }

    // MARK: settings

    func loadSettings() async {
        guard let (d, r) = try? await URLSession.shared.data(from: url("api/settings")),
              (r as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
        settings = obj["settings"] as? [String: Any] ?? obj  // {"settings": {...}, "defaults": ..., "descriptions": ...}
        onSettingsChange?()
    }

    /// PUT /api/settings. Returns false if the backend rejected or couldn't take the change.
    @discardableResult
    func updateSettings(_ changes: [String: Any]) async -> Bool {
        var req = URLRequest(url: url("api/settings"))
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: changes)
        guard let (d, r) = try? await URLSession.shared.data(for: req), (r as? HTTPURLResponse)?.statusCode == 200 else {
            return false
        }
        if let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let s = obj["settings"] as? [String: Any] {
            settings = s
            onSettingsChange?()
        }
        return true
    }

    /// Follow the backend's event stream; a {"type": "settings"} event (e.g. an agent changed settings over MCP)
    /// re-reads them, and every reconnect re-reads them too.
    private func listen() {
        events?.cancel()
        events = Task { [weak self] in
            var down = 0
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    var req = URLRequest(url: self.url("api/events"))
                    req.timeoutInterval = 120
                    let (bytes, r) = try await URLSession.shared.bytes(for: req)
                    // the stream stays open while the backend runs, so a reconnect is the moment it is back
                    if (r as? HTTPURLResponse)?.statusCode == 200, self.state != .up {
                        self.state = .up
                        await self.loadSettings()
                    }
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: "), let d = line.dropFirst(6).data(using: .utf8),
                              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { continue }
                        if obj["type"] as? String == "settings" { await self.loadSettings() }
                    }
                } catch {}
                try? await Task.sleep(for: .seconds(3))
                if await self.isUp() {
                    down = 0
                    if self.state != .up { self.state = .up }
                    await self.loadSettings()
                } else {
                    if self.state == .up { self.state = .starting }
                    down += 1
                    // gone for good (stopped, crashed, or replaced by an installer): start it again
                    if down == 3, !self.external, self.child?.isRunning != true, let bin = Self.findBinary() {
                        self.spawn(bin)
                    }
                }
            }
        }
    }
}
