import AppKit

/// Updates the user chooses to install. Checks GitHub's latest release ~30 s after launch and then daily (unless
/// the `auto_update_check` setting is off); a newer version shows up in the menu and the search panel. Nothing is
/// downloaded or installed until the user asks. Installing downloads Pensieve.dmg, checks the app inside (version
/// and signature), swaps it in place of this app's bundle, and relaunches.
@MainActor
final class Updater: ObservableObject {
    static let shared = Updater()

    struct Release: Equatable {
        let version: String
        let dmg: URL
        let page: URL?
    }

    enum Phase: Equatable {
        case idle, checking
        case downloading(Double)  // 0...1, or -1 when the size is unknown
        case installing
        case failed(String)
    }

    @Published private(set) var available: Release?
    @Published private(set) var phase: Phase = .idle
    /// "Later": hidden from the panel until the next launch (still in the menu).
    @Published private(set) var dismissed = false

    private static let skipKey = "skippedUpdateVersion"
    private var timer: Task<Void, Never>?

    var currentVersion: String {
        ProcessInfo.processInfo.environment["PENSIEVE_FAKE_VERSION"]
            ?? Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }

    private var feedURL: URL {
        ProcessInfo.processInfo.environment["PENSIEVE_UPDATE_URL"].flatMap(URL.init(string:))
            ?? URL(string: "https://api.github.com/repos/TedHaley/pensieve/releases/latest")!
    }

    /// What the panel's hint line should say about updates, if anything.
    var panelText: String? {
        switch phase {
        case .downloading(let p): return p >= 0 ? "Downloading update… \(Int(p * 100))%" : "Downloading update…"
        case .installing: return "Installing update…"
        case .failed(let msg): return msg
        case .idle, .checking:
            guard let r = available, !dismissed else { return nil }
            return "Pensieve \(r.version) is available"
        }
    }

    var busy: Bool {
        switch phase {
        case .downloading, .installing: return true
        default: return false
        }
    }

    // MARK: checking

    func start() {
        timer?.cancel()
        let delay = Double(ProcessInfo.processInfo.environment["PENSIEVE_UPDATE_DELAY"] ?? "") ?? 30
        timer = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            while !Task.isCancelled {
                guard let self else { return }
                if Backend.shared.autoUpdateCheck { _ = await self.check() }
                try? await Task.sleep(for: .seconds(24 * 3600))
            }
        }
    }

    /// Checks now. Returns the newer release (ignoring a skipped version unless `manual`), or nil if up to date.
    @discardableResult
    func check(manual: Bool = false) async -> Result<Release?, Error> {
        if busy { return .success(available) }
        phase = .checking
        defer { if phase == .checking { phase = .idle } }
        do {
            var req = URLRequest(url: feedURL)
            req.timeoutInterval = 20
            req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
            req.setValue("Pensieve/\(currentVersion)", forHTTPHeaderField: "User-Agent")  // GitHub requires one
            let (d, r) = try await URLSession.shared.data(for: req)
            if let code = (r as? HTTPURLResponse)?.statusCode, code != 200 {
                throw UpdateError("The update server answered \(code).")
            }
            guard let rel = Self.parse(d) else {
                log("check: no usable release (draft, prerelease, or no Pensieve.dmg asset)")
                available = nil
                return .success(nil)
            }
            let newer = Self.compare(rel.version, currentVersion) > 0
            let skipped = Prefs.store.string(forKey: Self.skipKey) == rel.version
            log("check: latest \(rel.version), running \(currentVersion)\(newer ? " (newer)" : "")\(skipped ? " (skipped)" : "")")
            available = newer && (manual || !skipped) ? rel : nil
            if manual && available != nil { dismissed = false }
            return .success(available)
        } catch {
            log("check failed: \(error.localizedDescription)")
            return .failure(error)
        }
    }

    static func parse(_ data: Data) -> Release? {
        guard let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              o["draft"] as? Bool != true, o["prerelease"] as? Bool != true,
              let tag = o["tag_name"] as? String,
              let assets = o["assets"] as? [[String: Any]],
              let asset = assets.first(where: { $0["name"] as? String == "Pensieve.dmg" }),
              let url = (asset["browser_download_url"] as? String).flatMap(URL.init(string:)) else { return nil }
        let version = tag.hasPrefix("v") || tag.hasPrefix("V") ? String(tag.dropFirst()) : tag
        return Release(version: version, dmg: url, page: (o["html_url"] as? String).flatMap(URL.init(string:)))
    }

    /// Numeric semver compare ("0.10.0" > "0.9.2"); anything after '-' or '+' is ignored.
    static func compare(_ a: String, _ b: String) -> Int {
        func parts(_ s: String) -> [Int] {
            let core = s.split(whereSeparator: { $0 == "-" || $0 == "+" }).first.map(String.init) ?? s
            return core.split(separator: ".").map { Int($0.filter(\.isNumber)) ?? 0 }
        }
        let x = parts(a), y = parts(b)
        for i in 0..<max(x.count, y.count) {
            let l = i < x.count ? x[i] : 0, r = i < y.count ? y[i] : 0
            if l != r { return l < r ? -1 : 1 }
        }
        return 0
    }

    func later() {
        dismissed = true
        if case .failed = phase { phase = .idle }
    }

    func skip() {
        guard let r = available else { return }
        Prefs.store.set(r.version, forKey: Self.skipKey)
        log("skip \(r.version)")
        available = nil
        if case .failed = phase { phase = .idle }
    }

    // MARK: installing

    func install() {
        guard let rel = available, !busy else { return }
        dismissed = false
        phase = .downloading(0)
        log("update to \(rel.version): downloading \(rel.dmg.absoluteString)")
        Task {
            do {
                let work = FileManager.default.temporaryDirectory.appendingPathComponent("pensieve-update-\(UUID().uuidString)")
                try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
                let dmg = work.appendingPathComponent("Pensieve.dmg")
                try await Downloader.fetch(rel.dmg, to: dmg) { [weak self] p in
                    Task { @MainActor in
                        if case .downloading = self?.phase { self?.phase = .downloading(p) }
                    }
                }
                phase = .installing
                let bundle = Bundle.main.bundlePath
                try await Task.detached { try Self.swap(dmg: dmg, work: work, version: rel.version, into: bundle) }.value
                log("installed \(rel.version) at \(bundle); relaunching")
                relaunch(bundle)
            } catch {
                let msg = (error as? UpdateError)?.message ?? error.localizedDescription
                log("update failed: \(msg)")
                phase = .failed("Update failed: \(msg)")
            }
        }
    }

    /// Mount, verify, and atomically replace the app bundle. Runs off the main thread.
    nonisolated private static func swap(dmg: URL, work: URL, version: String, into bundlePath: String) throws {
        let fm = FileManager.default
        let mount = work.appendingPathComponent("mnt")
        try fm.createDirectory(at: mount, withIntermediateDirectories: true)
        try run("/usr/bin/hdiutil", ["attach", "-nobrowse", "-noverify", "-readonly", "-mountpoint", mount.path, dmg.path],
                "Couldn't open the downloaded disk image.")
        defer {
            _ = try? run("/usr/bin/hdiutil", ["detach", mount.path, "-force"], "")
            try? fm.removeItem(at: work)
        }
        let newApp = mount.appendingPathComponent("Pensieve.app")
        guard let info = NSDictionary(contentsOf: newApp.appendingPathComponent("Contents/Info.plist")),
              let got = info["CFBundleShortVersionString"] as? String else {
            throw UpdateError("The disk image doesn't contain Pensieve.app.")
        }
        guard got == version else {
            throw UpdateError("The downloaded app is version \(got), expected \(version).")
        }
        try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", newApp.path],
                "The downloaded app's signature didn't verify.")

        let dest = URL(fileURLWithPath: bundlePath)
        let parent = dest.deletingLastPathComponent()
        guard fm.isWritableFile(atPath: parent.path), fm.isWritableFile(atPath: bundlePath) else {
            throw UpdateError("Pensieve can't replace itself in \((parent.path as NSString).abbreviatingWithTildeInPath). Download Pensieve.dmg from "
                              + "github.com/TedHaley/pensieve/releases and drag Pensieve into Applications.")
        }
        // Copy next to the app first (same volume), then swap in one step so a failure never leaves half an app.
        let staged = parent.appendingPathComponent(".Pensieve-\(version)-\(UUID().uuidString.prefix(8)).app")
        try run("/usr/bin/ditto", [newApp.path, staged.path], "Couldn't copy the new version next to the app.")
        do {
            _ = try fm.replaceItemAt(dest, withItemAt: staged, backupItemName: nil, options: [])
        } catch {
            try? fm.removeItem(at: staged)
            throw UpdateError("Couldn't replace the app: \(error.localizedDescription)")
        }
    }

    @discardableResult
    nonisolated private static func run(_ tool: String, _ args: [String], _ failure: String) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        try p.run()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        p.waitUntilExit()
        Updater.logLine("$ \(tool) \(args.joined(separator: " ")) -> \(p.terminationStatus)\(out.isEmpty ? "" : "\n" + out)")
        if p.terminationStatus != 0 && !failure.isEmpty { throw UpdateError(failure) }
        return out
    }

    /// Start the new copy once this one has exited, then quit (stopping the backend we started, as on Quit).
    private func relaunch(_ bundlePath: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        var env = ProcessInfo.processInfo.environment
        if Debug.enabled {
            // Test instances relaunch the binary directly with their environment (minus the fake version), so
            // LaunchServices never hands the launch to an installed copy with the same bundle id.
            env["PENSIEVE_FAKE_VERSION"] = nil
            p.arguments = ["-c", "sleep 1; exec \"$0/Contents/MacOS/Pensieve\" >>\"$HOME/.pensieve/update-relaunch.log\" 2>&1", bundlePath]
        } else {
            p.arguments = ["-c", "sleep 1; open \"$0\"", bundlePath]
        }
        p.environment = env
        try? p.run()
        Backend.shared.stop()
        NSApp.terminate(nil)
    }

    // MARK: log

    private func log(_ s: String) { Self.logLine(s) }

    nonisolated static func logLine(_ s: String) {
        let dir = "\(NSHomeDirectory())/.pensieve"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let path = "\(dir)/update.log"
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(s)\n"
        if let h = FileHandle(forWritingAtPath: path) {
            h.seekToEndOfFile()
            h.write(line.data(using: .utf8)!)
            try? h.close()
        } else {
            try? line.write(toFile: path, atomically: true, encoding: .utf8)
        }
    }
}

struct UpdateError: LocalizedError {
    let message: String
    init(_ m: String) { message = m }
    var errorDescription: String? { message }
}

/// A download task with progress, moved to `dest` when done.
final class Downloader: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    private let dest: URL
    private let progress: @Sendable (Double) -> Void
    private var cont: CheckedContinuation<Void, Error>?
    private var moveError: Error?

    private init(dest: URL, progress: @escaping @Sendable (Double) -> Void) {
        self.dest = dest
        self.progress = progress
    }

    static func fetch(_ url: URL, to dest: URL, progress: @escaping @Sendable (Double) -> Void) async throws {
        let d = Downloader(dest: dest, progress: progress)
        let session = URLSession(configuration: .ephemeral, delegate: d, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            d.cont = c
            session.downloadTask(with: url).resume()
        }
    }

    func urlSession(_ s: URLSession, downloadTask: URLSessionDownloadTask, didWriteData _: Int64,
                    totalBytesWritten w: Int64, totalBytesExpectedToWrite t: Int64) {
        progress(t > 0 ? Double(w) / Double(t) : -1)
    }

    func urlSession(_ s: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        if let code = (downloadTask.response as? HTTPURLResponse)?.statusCode, code != 200 {
            moveError = UpdateError("The download failed (HTTP \(code)).")
            return
        }
        do {
            try? FileManager.default.removeItem(at: dest)
            try FileManager.default.moveItem(at: location, to: dest)
        } catch {
            moveError = error
        }
    }

    func urlSession(_ s: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let c = cont
        cont = nil
        if let e = error ?? moveError { c?.resume(throwing: e) } else { c?.resume() }
    }
}
