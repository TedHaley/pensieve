import AppKit
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
        }
        if hotkeys && HotKeys.shared.needsAccessibility && !Prefs.store.bool(forKey: "askedAccessibility") {
            Prefs.store.set(true, forKey: "askedAccessibility")
            HotKeys.requestAccessibility(prompt: true)
        }
        backend.start()
        Updater.shared.start()
        Updater.shared.$available.receive(on: RunLoop.main)
            .sink { [weak self] r in self?.badge?.isHidden = r == nil }.store(in: &watchers)
        if ProcessInfo.processInfo.environment["PENSIEVE_SHOW_PANEL"] != nil { panel.show() }
    }

    func applicationWillTerminate(_ notification: Notification) {
        Backend.shared.stop()
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
        case "snapshot" where Debug.enabled:
            Debug.snapshot(url, panel: panel.panel, web: visualizer.webView)
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
        menu.addItem(item("Search Pensieve    \(keys)", #selector(showSearch)))
        menu.addItem(item("Open Visualizer", #selector(openVisualizer)))
        menu.addItem(item("Settings…", #selector(openSettings)))
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
        if HotKeys.shared.needsAccessibility {
            menu.addItem(item("Allow the \(HotKeys.display(spec)) Hotkey…", #selector(openAccessibility)))
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
        menu.addItem(item("Check for Updates…", #selector(checkForUpdates)))
        let login = item("Launch at Login", #selector(toggleLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
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
    @objc private func checkForUpdates() {
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

    @objc private func pickAppearance(_ sender: NSMenuItem) {
        if let v = sender.representedObject as? String { setAppearance(v) }
    }

    /// Apply right away, remember locally, and save to the backend (which tells the visualizer and other clients).
    private func setAppearance(_ value: String) {
        Prefs.store.set(value, forKey: "appearance")
        NSApp.appearance = Self.appearance(value)
        Task { await Backend.shared.updateSettings(["appearance": value]) }
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
