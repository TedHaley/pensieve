import AppKit
import ServiceManagement

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private let visualizer = VisualizerController()
    private lazy var panel = PanelController(visualizer: visualizer)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.mainMenu = Self.mainMenu()  // key equivalents (copy, paste, ⌘W …) need a main menu even when hidden
        buildStatusItem()

        let backend = Backend.shared
        HotKeys.shared.action = { [weak self] in self?.panel.toggle() }
        HotKeys.shared.configure(backend.hotkey)
        backend.onSettingsChange = {
            HotKeys.shared.configure(Backend.shared.hotkey)
        }
        if HotKeys.shared.needsAccessibility && !UserDefaults.standard.bool(forKey: "askedAccessibility") {
            UserDefaults.standard.set(true, forKey: "askedAccessibility")
            HotKeys.requestAccessibility(prompt: true)
        }
        backend.start()
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
            visualizer.show(fragment: q("id").map { "open=\($0)" })
        case "settings":
            visualizer.show(fragment: "settings")
        case "snapshot" where Debug.enabled:
            Debug.snapshot(url, panel: panel.panel, web: visualizer.webView)
        case "key" where Debug.enabled:
            Debug.key(url, panel: panel.panel)
        default:
            panel.show()
        }
    }

    // MARK: menus

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let b = statusItem.button {
            b.image = NSImage(systemSymbolName: "sparkle.magnifyingglass", accessibilityDescription: "Pensieve")
            b.image?.isTemplate = true
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
        menu.addItem(item("Search Pensieve    \(keys)", #selector(showSearch)))
        menu.addItem(item("Open Visualizer", #selector(openVisualizer)))
        menu.addItem(item("Settings…", #selector(openSettings)))
        menu.addItem(.separator())

        let status: String
        switch Backend.shared.state {
        case .up: status = "Backend running"
        case .checking, .starting: status = "Backend starting…"
        case .missing: status = "Backend not installed"
        case .failed: status = "Backend stopped (see ~/.pensieve/server.log)"
        }
        let s = NSMenuItem(title: status, action: nil, keyEquivalent: "")
        s.isEnabled = false
        menu.addItem(s)
        if HotKeys.shared.needsAccessibility {
            menu.addItem(item("Allow the \(HotKeys.display(spec)) Hotkey…", #selector(openAccessibility)))
        }
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
        sub("Pensieve", [mi("Hide Pensieve", "hide:", "h"), .separator(), mi("Quit Pensieve", "terminate:", "q")])
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
