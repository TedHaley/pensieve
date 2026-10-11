import AppKit
import Carbon.HIToolbox
import Combine
import ServiceManagement

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var badge: NSView?
    private var watchers: Set<AnyCancellable> = []
    private let visualizer = VisualizerController()
    private lazy var panel = PanelController(visualizer: visualizer)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.mainMenu = Self.mainMenu()  // key equivalents (copy, paste, ⌘W …) need a main menu even when hidden
        buildStatusItem()

        let backend = Backend.shared
        // A debug instance (tests) leaves global hotkeys to the installed app and takes commands privately.
        let hotkeys = !Debug.enabled
        if Debug.enabled { Debug.listen { [weak self] in self?.handle($0) } }
        HotKeys.shared.action = { [weak self] in self?.panel.toggle() }
        if hotkeys { HotKeys.shared.configure(backend.hotkey) }
        applyAppearance()
        backend.onSettingsChange = { [weak self] in
            if hotkeys { HotKeys.shared.configure(Backend.shared.hotkey) }
            self?.applyAppearance()
            self?.askForSourcesIfNeeded()
        }
        if hotkeys && HotKeys.shared.needsAccessibility && !Prefs.store.bool(forKey: "askedAccessibility") {
            Prefs.store.set(true, forKey: "askedAccessibility")
            HotKeys.requestAccessibility(prompt: true)
        }
        backend.start()
        defaultLaunchAtLogin()
        Launcher.shared.refreshIfStale()
        Updater.shared.start()
        Updater.shared.$available.receive(on: RunLoop.main)
            .sink { [weak self] r in self?.badge?.isHidden = r == nil }.store(in: &watchers)
        backend.$syncing.receive(on: RunLoop.main)
            .sink { [weak self] jobs in self?.showSyncing(jobs) }.store(in: &watchers)
        if ProcessInfo.processInfo.environment["PENSIEVE_SHOW_PANEL"] != nil { panel.show() }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if !Debug.enabled { SpotlightShortcut.restore() }  // ⌘Space would do nothing while Pensieve isn't running
        Backend.shared.stop()
    }

    /// Launch at Login is on by default: turned on once, the first time an installed copy runs. Turning it off
    /// in the menu afterwards sticks.
    private func defaultLaunchAtLogin() {
        let installed = ["/Applications/", NSHomeDirectory() + "/Applications/"].contains { Bundle.main.bundlePath.hasPrefix($0) }
        guard installed, !Debug.enabled, !Prefs.store.bool(forKey: "launchAtLoginDefaulted") else { return }
        Prefs.store.set(true, forKey: "launchAtLoginDefaulted")
        if SMAppService.mainApp.status != .enabled { try? SMAppService.mainApp.register() }
    }

    /// Opening the app again (Finder, Spotlight, `open -a Pensieve`) shows the search panel.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        panel.show()
        return false
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        urls.forEach(handle)
    }

    /// pensieve://search?q=…  pensieve://visualize[?id=…]  pensieve://settings
    func handle(_ url: URL) {
        guard url.scheme?.lowercased() == "pensieve" else { return }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let q = { (name: String) in items.first { $0.name == name }?.value }
        switch url.host?.lowercased() ?? "" {
        case "search":
            panel.show(query: q("q"))
        case "visualize", "visualizer", "open":
            visualizer.show(fragment: VisualizerController.fragment([("open", q("id")), ("q", q("q"))]))
        case "settings":
            visualizer.show(fragment: "settings")
        case "check-updates":  // Settings → General → Check now
            checkForUpdates()
        case "snapshot" where Debug.enabled:
            if q("target") == "statusitem" {
                if let img = statusItem.button?.image, let p = q("path") { Debug.writeEnlarged(img, to: p) }
            } else {
                Debug.snapshot(url, panel: q("target") == "buttons" ? panel.buttons : panel.panel, web: visualizer.webView)
            }
        case "hover" where Debug.enabled:  // the pointer over the search bar (`on=0`: off it), for snapshots
            panel.model.overBar = q("on") != "0"
        case "options" where Debug.enabled:  // the bar's chevron
            panel.model.optionsOpen.toggle()
        case "js" where Debug.enabled:  // e.g. scroll the visualizer before a snapshot
            visualizer.webView?.evaluateJavaScript(q("code") ?? "")
        case "key" where Debug.enabled:
            Debug.key(url, panel: panel.panel)
        case "state" where Debug.enabled:
            if let m = q("installing") { Backend.shared.debugSetState(.installing(m)) }
            if let m = q("failed") { Backend.shared.debugSetState(.failed(m)) }
        case "update" where Debug.enabled:
            switch q("do") ?? "" {
            case "check": Task { await Updater.shared.check() }
            case "install": Updater.shared.install()
            case "later": Updater.shared.later()
            case "skip": Updater.shared.skip()
            default: break
            }
        case "menu" where Debug.enabled:  // write the status menu's items to a file
            let m = NSMenu()
            menuNeedsUpdate(m)
            let lines = m.items.map { $0.isSeparatorItem ? "---" : $0.title + ($0.isEnabled ? "" : " (disabled)") }
            let badgeOn = badge.map { !$0.isHidden } ?? false
            try? (lines + ["badge: \(badgeOn)"]).joined(separator: "\n").write(toFile: q("path") ?? "/dev/null", atomically: true, encoding: .utf8)
        case "appearance" where Debug.enabled:
            if let a = q("set") { setAppearance(a) }
        default:
            panel.show()
        }
    }

    // MARK: menus

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let b = statusItem.button {
            b.image = Mark.menuBarImage()
            // a small dot when an update is available
            let dot = NSView(frame: NSRect(x: b.bounds.width - 9, y: b.bounds.height - 9, width: 6, height: 6))
            dot.wantsLayer = true
            dot.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
            dot.layer?.cornerRadius = 3
            dot.autoresizingMask = [.minXMargin, .minYMargin]
            dot.isHidden = true
            b.addSubview(dot)
            badge = dot
        }
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
    }

    private var syncJobs: [Backend.SyncJob] = []
    private var syncTimer: Timer?
    private var spin = 0.0

    /// While anything is indexing, the dots swirl in the ring; the menu lists what is being indexed.
    private func showSyncing(_ jobs: [Backend.SyncJob]) {
        syncJobs = jobs
        if jobs.isEmpty {
            syncTimer?.invalidate()
            syncTimer = nil
            spin = 0
            statusItem.button?.image = Mark.menuBarImage()
        } else if syncTimer == nil {
            syncTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 20, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.spin += 2 * .pi / 60  // one turn every 3 s
                    self.statusItem.button?.image = Mark.menuBarImage(spin: self.spin)
                }
            }
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let spec = HotKeys.shared.spec
        let tap = HotKeys.parse(spec).key == nil
        let keys = tap ? "tap \(HotKeys.display(spec)) or \(HotKeys.display(HotKeys.fallback))"
                       : HotKeys.display(spec)
        let up = Updater.shared
        switch up.phase {
        case .downloading, .installing:
            let i = NSMenuItem(title: up.panelText ?? "", action: nil, keyEquivalent: "")
            i.isEnabled = false
            menu.addItem(i)
        case .failed(let msg):  // the full reason is in the panel and the tooltip; menus don't wrap
            let i = NSMenuItem(title: "Update failed (details in ~/.pensieve/update.log)", action: nil, keyEquivalent: "")
            i.toolTip = msg
            i.isEnabled = false
            menu.addItem(i)
        default: break
        }
        if let r = up.available, !up.busy {
            menu.addItem(item("Update to Pensieve \(r.version)…", #selector(installUpdate)))
        }
        if up.available != nil || up.phase != .idle { menu.addItem(.separator()) }

        if !syncJobs.isEmpty {
            let head = NSMenuItem(title: "Indexing now", action: nil, keyEquivalent: "")
            head.isEnabled = false
            menu.addItem(head)
            for j in syncJobs {
                let i = NSMenuItem(title: j.label, action: nil, keyEquivalent: "")
                i.isEnabled = false
                i.indentationLevel = 1
                menu.addItem(i)
            }
            menu.addItem(.separator())
        }

        let search = item(tap ? "Search Pensieve    \(keys)" : "Search Pensieve", #selector(showSearch))
        if !tap, let k = HotKeys.parse(spec).key {  // a combo shows right-aligned like any menu shortcut
            search.keyEquivalent = k == UInt32(kVK_Space) ? " " : HotKeys.parse(spec).keyName.lowercased()
            search.keyEquivalentModifierMask = HotKeys.parse(spec).flags
        }
        menu.addItem(search)
        menu.addItem(item("Open Map",#selector(openVisualizer)))
        menu.addItem(.separator())

        menu.addItem(item("Settings…", #selector(openSettings), key: ","))
        let short = NSMenuItem(title: "Search Shortcut", action: nil, keyEquivalent: "")
        let shorts = NSMenu(title: "Search Shortcut")
        var known = false
        for (value, title) in HotKeys.presets {
            let i = item(title, #selector(pickShortcut(_:)))
            i.representedObject = value
            i.state = spec == value ? .on : .off
            known = known || spec == value
            shorts.addItem(i)
        }
        if !known {  // a combo typed into Settings
            let i = NSMenuItem(title: HotKeys.display(spec), action: nil, keyEquivalent: "")
            i.state = .on
            shorts.addItem(i)
        }
        short.submenu = shorts
        menu.addItem(short)
        if HotKeys.shared.needsAccessibility {
            menu.addItem(item("Allow the \(HotKeys.display(spec)) Shortcut…", #selector(openAccessibility)))
        }
        let look = NSMenuItem(title: "Appearance", action: nil, keyEquivalent: "")
        let looks = NSMenu(title: "Appearance")
        for (title, value) in [("System", "system"), ("Light", "light"), ("Dark", "dark")] {
            let i = item(title, #selector(pickAppearance(_:)))
            i.representedObject = value
            i.state = Backend.shared.appearance == value ? .on : .off
            looks.addItem(i)
        }
        look.submenu = looks
        menu.addItem(look)
        let login = item("Launch at Login", #selector(toggleLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())

        let status: String
        switch Backend.shared.state {
        case .up: status = "Backend running"
        case .checking, .starting: status = "Backend starting…"
        case .missing: status = "Backend not installed"
        case .installing: status = "Setting up the backend…"
        case .failed: status = "Backend stopped (see ~/.pensieve/server.log)"
        }
        let s = NSMenuItem(title: status, action: nil, keyEquivalent: "")
        s.isEnabled = false
        menu.addItem(s)
        let version = NSMenuItem(title: "Version \(up.currentVersion)", action: nil, keyEquivalent: "")
        version.isEnabled = false
        menu.addItem(version)
        menu.addItem(item("Check for Updates…", #selector(checkForUpdates)))
        menu.addItem(.separator())
        menu.addItem(item("Quit Pensieve", #selector(NSApplication.terminate(_:)), key: "q", target: NSApp))
    }

    private func item(_ title: String, _ action: Selector, key: String = "", target: AnyObject? = nil) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.target = target ?? self
        return i
    }

    @objc private func showSearch() { panel.show() }
    @objc private func openVisualizer() { visualizer.show() }
    @objc private func openSettings() { visualizer.show(fragment: "settings") }
    @objc private func reloadVisualizer() { visualizer.reload() }

    @objc private func installUpdate() { Updater.shared.install() }

    /// Checks now and answers in a small alert: up to date, or the update with Update / Later / Skip.
    @objc func checkForUpdates() {
        Task {
            let up = Updater.shared
            let result = await up.check(manual: true)
            NSApp.activate()
            let a = NSAlert()
            switch result {
            case .failure(let e):
                a.messageText = "Couldn't check for updates"
                a.informativeText = e.localizedDescription
            case .success(nil):
                a.messageText = "You're up to date"
                a.informativeText = "Pensieve \(up.currentVersion) is the latest version."
            case .success(let r?):
                a.messageText = "Pensieve \(r.version) is available"
                a.informativeText = "You have \(up.currentVersion). Updating downloads the new version, replaces this app, and restarts it."
                a.addButton(withTitle: "Update")
                a.addButton(withTitle: "Later")
                a.addButton(withTitle: "Skip This Version")
                if Debug.enabled { Debug.lastAlert = a.messageText }
                switch a.runModal() {
                case .alertFirstButtonReturn: up.install()
                case .alertThirdButtonReturn: up.skip()
                default: up.later()
                }
                return
            }
            if Debug.enabled { Debug.lastAlert = a.messageText }
            a.runModal()
        }
    }

    /// Applies at once (so ⌘Space works even with the backend down), then saves it like any other setting.
    @objc private func pickShortcut(_ sender: NSMenuItem) {
        guard let v = sender.representedObject as? String else { return }
        if !Debug.enabled { HotKeys.shared.configure(v) }
        Task { await Backend.shared.updateSettings(["hotkey": v]) }
    }

    @objc private func pickAppearance(_ sender: NSMenuItem) {
        if let v = sender.representedObject as? String { setAppearance(v) }
    }

    /// Apply right away, remember locally, and save to the backend (which tells the visualizer and other clients).
    private func setAppearance(_ value: String) {
        Prefs.store.set(value, forKey: "appearance")
        NSApp.appearance = Self.appearance(value)
        Task { await Backend.shared.updateSettings(["appearance": value]) }
    }

    private var askedForSources = false

    /// A fresh install or a reset indexes nothing until the user picks the sources: show that choice once per launch.
    private func askForSourcesIfNeeded() {
        guard !askedForSources, Backend.shared.settings["setup_done"] as? Bool == false, !Debug.enabled else { return }
        askedForSources = true
        visualizer.show(fragment: "setup")
    }

    private func applyAppearance() {
        let value = Backend.shared.appearance
        Prefs.store.set(value, forKey: "appearance")
        NSApp.appearance = Self.appearance(value)
    }

    private static func appearance(_ value: String) -> NSAppearance? {
        switch value {
        case "light": return NSAppearance(named: .aqua)
        case "dark": return NSAppearance(named: .darkAqua)
        default: return nil  // follow the system
        }
    }

    @objc private func openAccessibility() {
        HotKeys.requestAccessibility(prompt: true)
        if let u = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(u)
        }
    }

    @objc private func toggleLogin() {
        let svc = SMAppService.mainApp
        do {
            if svc.status == .enabled { try svc.unregister() } else { try svc.register() }
        } catch {
            let a = NSAlert()
            a.messageText = "Couldn't change Launch at Login"
            a.informativeText = error.localizedDescription
            a.runModal()
        }
    }

    private static func mainMenu() -> NSMenu {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let top = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            top.submenu = m
            main.addItem(top)
        }
        func mi(_ t: String, _ a: String, _ k: String, _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: Selector(a), keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            return i
        }
        let settings = mi("Settings…", "openSettings", ",")
        settings.target = NSApp.delegate
        sub("Pensieve", [settings, .separator(), mi("Hide Pensieve", "hide:", "h"), .separator(),
                         mi("Quit Pensieve", "terminate:", "q")])
        sub("Edit", [mi("Undo", "undo:", "z"), mi("Redo", "redo:", "z", [.command, .shift]), .separator(),
                     mi("Cut", "cut:", "x"), mi("Copy", "copy:", "c"), mi("Paste", "paste:", "v"),
                     mi("Select All", "selectAll:", "a")])
        let reload = mi("Reload", "reloadVisualizer", "r")
        reload.target = NSApp.delegate
        sub("View", [reload, mi("Enter Full Screen", "toggleFullScreen:", "f", [.command, .control])])
        sub("Window", [mi("Close", "performClose:", "w"), mi("Minimize", "performMiniaturize:", "m")])
        return main
    }
}
