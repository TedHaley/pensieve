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

/// Holds the glass circles beside the search bar. It never takes key, so a click on a circle doesn't close the panel.
final class ButtonsPanel: NSPanel {
    init() {
        super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        isReleasedWhenClosed = false
        ignoresMouseEvents = true
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// The circles' window is never key, so a click must work on the first try.
final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
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
        case .settings: openSettings(hit)
        }
    }

    func openSettings(_ hit: Hit) {
        dismiss()
        Launcher.shared.openSettings(hit)
    }

    func settings() {
        dismiss()
        visualizer.show(fragment: "settings")
    }

    func open(_ hit: Hit) {
        if hit.kind == "calc" {
            copyPath(hit)
            NSSound(named: "Tink")?.play()
            return dismiss()
        }
        dismiss()
        if Launcher.shared.handles(hit) { return Launcher.shared.run(hit) }
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
        if Launcher.shared.isToggle(hit) { return openSettings(hit) }  // ⌘↩ on a toggle shows its pane
        guard let path = hit.path, FileManager.default.fileExists(atPath: path) else { return }
        dismiss()
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func visualize(_ hit: Hit?) {
        dismiss()
        let onMap = hit.flatMap { $0.onMap ? $0.id : nil }
        visualizer.show(fragment: VisualizerController.fragment([("open", onMap), ("q", query())]))
    }

    @discardableResult
    func copyPath(_ hit: Hit) -> Bool {
        if hit.kind == "calc" {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(String(hit.id.dropFirst("calc:".count)), forType: .string)
            return true
        }
        guard let path = hit.path else { return false }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(hit.line.map { "\(path):\($0)" } ?? path, forType: .string)
        return true
    }
}

@MainActor
final class PanelController: NSObject, NSWindowDelegate {
    let panel = SpotlightPanel()
    let buttons = ButtonsPanel()
    let model = SearchModel()
    let actions: Actions
    private var keyMonitor: Any?
    private var flagsMonitor: Any?
    private var top: CGFloat = 0
    private var height: CGFloat = 0  // where the frame is headed, which can differ from the frame mid-animation
    private var width: CGFloat = 0
    private var hiding = false
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
        Launcher.shared.onScan = { [weak self] in
            guard let self, !self.model.trimmed.isEmpty else { return }
            self.model.matchLocal()
            self.relayout()
        }
        let host = NSHostingView(rootView: SearchView(model: model))
        host.sizingOptions = []
        // clip to the glass's rounded shape: the window shadow is drawn from what's opaque, and the square
        // corners otherwise showed as a rectangular outline around the panel
        host.wantsLayer = true
        host.layer?.cornerRadius = SearchModel.cornerRadius
        host.layer?.cornerCurve = .continuous
        host.layer?.masksToBounds = true
        panel.contentView = host
        panel.delegate = self
        buttons.contentView = FirstMouseHostingView(rootView: BarButtons(model: model))
        // hidden circles must not block clicks on whatever is behind them
        model.$buttonsShown.receive(on: RunLoop.main)
            .sink { [weak self] on in self?.buttons.ignoresMouseEvents = !on }.store(in: &watchers)
        // the hint line wraps, so backend state changes (setup, errors) can change the panel's height
        Backend.shared.$state.dropFirst().receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.relayout() }.store(in: &watchers)
        Updater.shared.objectWillChange.receive(on: RunLoop.main)  // the update line can appear, grow, or go
            .sink { [weak self] _ in Task { @MainActor in self?.relayout() } }.store(in: &watchers)
    }

    var isShown: Bool { panel.isVisible && !hiding }

    func toggle() {
        isShown ? hide() : show()
    }

    func show(query: String? = nil) {
        Launcher.shared.refreshIfStale()
        if let query { model.query = query }
        let appearing = !isShown, fading = hiding
        hiding = false
        place()
        if appearing && !fading {  // a fade-out in progress reverses from where it is
            panel.alphaValue = 0
            model.presented = false
        }
        panel.makeKeyAndOrderFront(nil)
        if buttons.parent == nil { panel.addChildWindow(buttons, ordered: .above) }
        installKeyMonitor()
        focusField(selectAll: query == nil)
        guard appearing else { return }
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.16
            ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
            panel.animator().alphaValue = 1
        }
        DispatchQueue.main.async { [weak self] in  // after the shrunken frame has been drawn once
            withAnimation(.spring(response: 0.28, dampingFraction: 0.78)) { self?.model.presented = true }
        }
    }

    func hide() {
        guard isShown else { return }
        model.closeActions()
        model.hideButtons()
        for m in [keyMonitor, flagsMonitor].compactMap({ $0 }) { NSEvent.removeMonitor(m) }
        keyMonitor = nil
        flagsMonitor = nil
        hiding = true
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.12
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            MainActor.assumeIsolated {
                guard let self, self.hiding else { return }  // shown again while fading out
                self.hiding = false
                self.panel.removeChildWindow(self.buttons)
                self.buttons.orderOut(nil)
                self.panel.orderOut(nil)
                self.panel.alphaValue = 1
            }
        })
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
        height = model.panelHeight
        width = model.panelWidth
        let left = vf.midX - SearchModel.width / 2
        model.glassSize = CGSize(width: width, height: height)
        panel.setFrame(NSRect(x: left, y: top - height, width: width, height: height), display: true)
        // the circles' hover area starts at the narrowed panel's edge, so the pointer never falls between the two
        let s = SearchModel.self
        let w = s.buttonsReserve + 2 * s.buttonPad - s.buttonGap, h = s.barHeight + 2 * s.buttonPad
        buttons.setFrame(NSRect(x: left + s.width - s.buttonsReserve - (s.buttonPad - s.buttonGap),
                                y: top - s.barHeight / 2 - h / 2, width: w, height: h), display: false)
        refreshShadow()
    }

    /// The shadow follows the content's shape, so recompute it once the new size has been drawn.
    private func refreshShadow() {
        panel.invalidateShadow()
        DispatchQueue.main.async { [weak self] in self?.panel.invalidateShadow() }
    }

    /// SearchView animates the glass to the new size, so the window only has to hold it: it grows at once and
    /// shrinks when the glass has finished shrinking. Its top edge stays at `top`, so the bar doesn't move.
    private func relayout() {
        guard panel.isVisible else { return }
        let h = model.panelHeight, w = model.panelWidth
        guard abs(height - h) > 0.5 || abs(width - w) > 0.5 else { return }
        height = h
        width = w
        let f = panel.frame
        let grown = NSSize(width: max(f.width, w), height: max(f.height, h))
        if grown != f.size {
            panel.setFrame(NSRect(x: f.minX, y: top - grown.height, width: grown.width, height: grown.height), display: true)
        }
        DispatchQueue.main.async { [weak self] in  // after the window's resize has been laid out
            withAnimation(SearchModel.resize) { self?.model.glassSize = CGSize(width: w, height: h) }
            self?.refreshShadow()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + SearchModel.resizeSeconds) { [weak self] in
            guard let self, self.panel.isVisible, self.height == h, self.width == w else { return }
            let f = self.panel.frame
            if f.size != NSSize(width: w, height: h) {
                self.panel.setFrame(NSRect(x: f.minX, y: self.top - h, width: w, height: h), display: true)
            }
            self.refreshShadow()
        }
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
        case kVK_Tab where mods.isEmpty:
            return model.complete()
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
