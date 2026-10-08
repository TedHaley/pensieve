import AppKit
import Carbon.HIToolbox
import Combine
import SwiftUI

/// Borderless floating panel that can take keyboard focus without activating the app, like Spotlight.
final class SpotlightPanel: NSPanel {
    init() {
        super.init(contentRect: NSRect(x: 0, y: 0, width: SearchModel.width, height: 100),
                   styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
        hidesOnDeactivate = false
        isMovable = false
        isReleasedWhenClosed = false
        animationBehavior = .utilityWindow
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// What happens to a result: open it, reveal it, show it in the visualizer, copy its path.
@MainActor
final class Actions {
    let visualizer: VisualizerController
    var dismiss: () -> Void = {}
    var query: () -> String = { "" }  // the panel's search, carried into the visualizer

    init(visualizer: VisualizerController) {
        self.visualizer = visualizer
    }

    func perform(_ action: ResultAction, _ hit: Hit) {
        switch action {
        case .open: open(hit)
        case .reveal: reveal(hit)
        case .map: visualize(hit)
        case .copy: copyPath(hit)
        }
    }

    func settings() {
        dismiss()
        visualizer.show(fragment: "settings")
    }

    func open(_ hit: Hit) {
        dismiss()
        guard let path = hit.path, hit.kind != "session" else { return visualize(hit) }
        if hit.isFolder {  // a Finder window on the folder (never launches a bundle like Foo.app)
            NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
            return
        }
        if hit.kind == "code", let line = hit.line,
           let scheme = ["vscode": "vscode", "cursor": "cursor", "zed": "zed", "windsurf": "windsurf"][Backend.shared.editor],
           let u = URL(string: "\(scheme)://file\(path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? path):\(line)") {
            NSWorkspace.shared.open(u)
            return
        }
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        } else {
            visualize(hit)
        }
    }

    func reveal(_ hit: Hit) {
        guard let path = hit.path, FileManager.default.fileExists(atPath: path) else { return }
        dismiss()
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func visualize(_ hit: Hit?) {
        dismiss()
        visualizer.show(fragment: VisualizerController.fragment([("open", hit?.id), ("q", query())]))
    }

    @discardableResult
    func copyPath(_ hit: Hit) -> Bool {
        guard let path = hit.path else { return false }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(hit.line.map { "\(path):\($0)" } ?? path, forType: .string)
        return true
    }
}

@MainActor
final class PanelController: NSObject, NSWindowDelegate {
    let panel = SpotlightPanel()
    let model = SearchModel()
    let actions: Actions
    private var keyMonitor: Any?
    private var flagsMonitor: Any?
    private var top: CGFloat = 0
    private var lastFlags: NSEvent.ModifierFlags = []
    private var controlDownAt: TimeInterval?
    private var watchers: Set<AnyCancellable> = []

    init(visualizer: VisualizerController) {
        actions = Actions(visualizer: visualizer)
        super.init()
        actions.dismiss = { [weak self] in self?.hide() }
        actions.query = { [weak self] in self?.model.trimmed ?? "" }
        model.actions = actions
        model.onLayout = { [weak self] in self?.relayout() }
        let host = NSHostingView(rootView: SearchView(model: model))
        host.sizingOptions = []
        panel.contentView = host
        panel.delegate = self
        // the hint line wraps, so backend state changes (setup, errors) can change the panel's height
        Backend.shared.$state.dropFirst().receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.relayout() }.store(in: &watchers)
        Updater.shared.objectWillChange.receive(on: RunLoop.main)  // the update line can appear, grow, or go
            .sink { [weak self] _ in Task { @MainActor in self?.relayout() } }.store(in: &watchers)
    }

    var isShown: Bool { panel.isVisible }

    func toggle() {
        isShown ? hide() : show()
    }

    func show(query: String? = nil) {
        if let query { model.query = query }
        place()
        panel.makeKeyAndOrderFront(nil)
        installKeyMonitor()
        focusField(selectAll: query == nil)
    }

    func hide() {
        guard panel.isVisible else { return }
        model.closeActions()
        panel.orderOut(nil)
        for m in [keyMonitor, flagsMonitor].compactMap({ $0 }) { NSEvent.removeMonitor(m) }
        keyMonitor = nil
        flagsMonitor = nil
    }

    func windowDidResignKey(_ notification: Notification) {
        hide()
    }

    /// Centered on the screen with the mouse, its top edge a fifth of the way down, like Spotlight.
    private func place() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        guard let vf = screen?.visibleFrame else { return }
        top = vf.maxY - vf.height * 0.2
        let h = model.panelHeight
        panel.setFrame(NSRect(x: vf.midX - SearchModel.width / 2, y: top - h, width: SearchModel.width, height: h),
                       display: true)
    }

    /// Grow or shrink downward as results change; the search bar never moves.
    private func relayout() {
        guard panel.isVisible else { return }
        let h = model.panelHeight
        let f = panel.frame
        guard abs(f.height - h) > 0.5 else { return }
        panel.setFrame(NSRect(x: f.minX, y: top - h, width: f.width, height: h), display: true)
        panel.invalidateShadow()
    }

    /// Control tapped alone (pressed and released with nothing else) toggles the action list. Only a clean
    /// none -> ⌃ -> none sequence counts, so ⌃⇧ (the global hotkey) and ⌃-shortcuts never trigger it.
    /// Seeing our own panel's modifier events needs no Accessibility permission.
    private func handleFlags(_ e: NSEvent) {
        let m = e.modifierFlags.intersection([.command, .control, .option, .shift])
        defer { lastFlags = m }
        if m == .control && lastFlags.isEmpty {
            controlDownAt = e.timestamp
        } else if m.isEmpty && lastFlags == .control, let t = controlDownAt, e.timestamp - t < 0.5 {
            controlDownAt = nil
            if HotKeys.shared.spec != "ctrl" && HotKeys.shared.spec != "control" { model.toggleActions() }
        } else {
            controlDownAt = nil
        }
    }

    private func focusField(selectAll: Bool) {
        // SwiftUI's TextField is an NSTextField underneath; focusing it directly is the most reliable way.
        Task { @MainActor in
            guard let field = Self.findTextField(in: panel.contentView) else { return }
            panel.makeFirstResponder(field)
            if let editor = field.currentEditor() {
                if selectAll { editor.selectAll(nil) } else { editor.selectedRange = NSRange(location: (field.stringValue as NSString).length, length: 0) }
            }
        }
    }

    private static func findTextField(in view: NSView?) -> NSTextField? {
        guard let view else { return nil }
        if let f = view as? NSTextField, f.isEditable { return f }
        for sub in view.subviews {
            if let f = findTextField(in: sub) { return f }
        }
        return nil
    }

    /// → only opens the action list when it wouldn't otherwise move the caret.
    private var caretAtEnd: Bool {
        guard let tv = panel.firstResponder as? NSTextView else { return true }
        let r = tv.selectedRange()
        return r.length == 0 && r.location >= (tv.string as NSString).length
    }

    private var fieldHasSelection: Bool {
        (panel.firstResponder as? NSTextView).map { $0.selectedRange().length > 0 } ?? false
    }

    private func installKeyMonitor() {
        if flagsMonitor == nil {
            flagsMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] e in
                nonisolated(unsafe) let e = e
                MainActor.assumeIsolated {
                    guard let self, self.panel.isKeyWindow else { return }
                    self.handleFlags(e)
                }
                return e
            }
        }
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            nonisolated(unsafe) let e = e  // local monitors run on the main thread
            let handled = MainActor.assumeIsolated {
                guard let self, e.window === self.panel else { return false }
                return self.handleKey(e)
            }
            return handled ? nil : e
        }
    }

    /// Returns true when the key was handled here (and should not reach the text field).
    private func handleKey(_ e: NSEvent) -> Bool {
        controlDownAt = nil  // ⌃ followed by a key is a shortcut, not a tap
        let mods = e.modifierFlags.intersection([.command, .option, .control, .shift])
        if model.actionsOpen {
            switch Int(e.keyCode) {
            case kVK_Escape, kVK_LeftArrow: model.closeActions()
            case kVK_DownArrow: model.moveAction(1)
            case kVK_UpArrow: model.moveAction(-1)
            case kVK_Return, kVK_ANSI_KeypadEnter: model.runAction()
            case kVK_RightArrow: break
            default:
                model.closeActions()  // typing goes back to the search field
                return handleKey(e)
            }
            return true
        }
        switch Int(e.keyCode) {
        case kVK_Escape:
            hide()
        case kVK_RightArrow where model.selectedHit != nil && caretAtEnd:
            model.openActions()
        case kVK_ANSI_U where mods == .command && Updater.shared.available != nil:
            Updater.shared.install()
        case kVK_ANSI_Comma where mods == .command:
            actions.settings()
        case kVK_DownArrow:
            model.move(1)
        case kVK_UpArrow:
            model.move(-1)
        case kVK_Return, kVK_ANSI_KeypadEnter:
            guard let hit = model.selectedHit else {
                if !model.trimmed.isEmpty { model.retry() }
                return true
            }
            if mods.contains(.command) {
                actions.reveal(hit)
            } else if mods.contains(.option) {
                actions.visualize(hit)
            } else {
                actions.open(hit)
            }
        case kVK_ANSI_O where mods == .command:
            actions.visualize(model.selectedHit)
        case kVK_ANSI_C where mods == .command:
            guard !fieldHasSelection, let hit = model.selectedHit else { return false }
            if !actions.copyPath(hit) { return false }
            NSSound(named: "Tink")?.play()
        default:
            return false
        }
        return true
    }
}
