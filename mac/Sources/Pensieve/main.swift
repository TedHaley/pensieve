import AppKit

/// The daemon's HTTP routes on this Mac. A call waits for the answer.
enum Daemon {
    static let base: URL = {
        let env = ProcessInfo.processInfo.environment
        if let u = env["PENSIEVE_URL"].flatMap(URL.init(string:)) { return u }
        return URL(string: "http://127.0.0.1:\(env["PENSIEVE_PORT"] ?? "8790")")!
    }()

    static func call(_ path: String, post body: [String: Any]? = nil) -> [String: Any]? {
        var r = URLRequest(url: base.appendingPathComponent(path))
        // A daemon that does not run refuses the connection at once. A daemon that runs answers /status from
        // memory, so 3 s is a hung daemon, not a busy one.
        r.timeoutInterval = 3
        if let body {
            r.httpMethod = "POST"
            r.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        var out: [String: Any]?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: r) { data, _, _ in
            out = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            done.signal()
        }.resume()
        done.wait()
        return out
    }

    /// The answer of /status in one line. Before the daemon's first count, the counts are absent.
    static func statusLine(_ s: [String: Any]?) -> String {
        guard let s else { return "Pensieve is not running" }
        if s["opening"] as? Bool == true { return "Pensieve opens its index…" }
        if s["sessions"] == nil { return "Running · counting the index…" }
        let n = { (k: String) in (s[k] as? Int) ?? 0 }
        return "Running · \(n("sessions")) sessions · \(n("thoughts")) thoughts · \(n("repos")) repos"
    }
}

/// The `pensieve` program: in the app when build.sh put it there, else the copy that install made.
@MainActor
enum Program {
    static var path: String? {
        let inApp = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/pensieve").path
        let installed = ("~/.pensieve/bin/pensieve" as NSString).expandingTildeInPath
        return [inApp, installed].first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    static func install() {
        confirmAndRun(["install"], "Install Pensieve?",
                      "This starts the daemon at login, adds the MCP server to Claude Code and Codex, and adds the Pensieve skills.")
    }

    static func uninstall() {
        confirmAndRun(["uninstall"], "Uninstall Pensieve?",
                      "This stops the daemon and removes the MCP server and the skills. The index stays in ~/.pensieve.")
    }

    static func openData() {
        NSWorkspace.shared.open(URL(fileURLWithPath: ("~/.pensieve" as NSString).expandingTildeInPath))
    }

    private static func confirmAndRun(_ args: [String], _ title: String, _ text: String) {
        guard let path else { return }
        let ask = NSAlert()
        ask.messageText = title
        ask.informativeText = text
        ask.addButton(withTitle: "Continue")
        ask.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard ask.runModal() == .alertFirstButtonReturn else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do {
            try p.run()
            let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            p.waitUntilExit()
            tell(p.terminationStatus == 0 ? "Done" : "The command failed", out)
        } catch {
            tell("The command did not start", error.localizedDescription)
        }
    }

    static func tell(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        NSApp.activate(ignoringOtherApps: true)
        a.runModal()
    }
}

/// Pensieve has no Dock icon. The menu bar item shows what the daemon does and opens the settings window. All other
/// use is through the `pensieve` command and the agents' MCP tools.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item.button?.image = Mark.menuBarImage()
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let status = Daemon.call("status")
        menu.addItem(note(Daemon.statusLine(status)))
        if let busy = status?["busy"] as? [String], !busy.isEmpty {
            menu.addItem(note("Indexing: \(busy.joined(separator: ", "))"))
        }
        menu.addItem(.separator())
        menu.addItem(action("Settings…", #selector(openSettings), key: ","))
        menu.addItem(.separator())
        menu.addItem(action("Quit Pensieve Menu", #selector(quit), key: "q"))
    }

    @objc private func openSettings() {
        SettingsWindow.show()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func note(_ title: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    private func action(_ title: String, _ selector: Selector, key: String = "") -> NSMenuItem {
        let i = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        i.target = self
        return i
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
