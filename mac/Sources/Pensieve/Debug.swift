import AppKit
import WebKit

/// Development aid, active only when PENSIEVE_DEBUG is set: `pensieve://snapshot?target=panel|visualizer&path=/x.png`
/// renders that window to a PNG.
@MainActor
enum Debug {
    static var enabled: Bool { ProcessInfo.processInfo.environment["PENSIEVE_DEBUG"] != nil }
    static var lastAlert = ""

    /// Debug builds take pensieve:// commands over a distributed notification instead of LaunchServices, so tests
    /// never reach an installed copy of the app (same bundle id and URL scheme).
    static let notification = Notification.Name("ca.tedhaley.pensieve.debug")

    static func listen(_ handle: @escaping @MainActor (URL) -> Void) {
        DistributedNotificationCenter.default().addObserver(forName: notification, object: nil, queue: .main) { n in
            let s = n.object as? String
            MainActor.assumeIsolated {
                if let s, let u = URL(string: s) { handle(u) }
            }
        }
    }

    static func snapshot(_ url: URL, panel: NSPanel, web: WKWebView?) {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        guard let path = items.first(where: { $0.name == "path" })?.value else { return }
        let target = items.first(where: { $0.name == "target" })?.value ?? "panel"
        if target == "url" {  // the visualizer's current URL, as text
            try? (web?.url?.absoluteString ?? "none").write(toFile: path, atomically: true, encoding: .utf8)
            return
        }
        if target == "visualizer", let web {
            web.takeSnapshot(with: nil) { img, _ in
                if let img { write(img, to: path) }
            }
            return
        }
        guard let cg = windowImage(panel.windowNumber) else { return }
        write(NSImage(cgImage: cg, size: .zero), to: path)
    }

    /// `pensieve://key?code=125&mods=cmd,opt` posts a key-down into our own event queue, so it goes through the
    /// same local monitors as a real key press.
    static func key(_ url: URL, panel: NSPanel) {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        if let tap = items.first(where: { $0.name == "tap" })?.value {  // `tap=ctrl` or `tap=ctrl,shift`: modifiers down, then up
            postFlags(tap.split(separator: ",").map(String.init), panel: panel)
            return
        }
        guard let code = items.first(where: { $0.name == "code" })?.value.flatMap(UInt16.init) else { return }
        var flags: NSEvent.ModifierFlags = []
        for m in (items.first(where: { $0.name == "mods" })?.value ?? "").split(separator: ",") {
            switch m {
            case "cmd": flags.insert(.command)
            case "opt": flags.insert(.option)
            case "shift": flags.insert(.shift)
            case "ctrl": flags.insert(.control)
            default: break
            }
        }
        if let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: ProcessInfo.processInfo.systemUptime,
                                    windowNumber: panel.windowNumber, context: nil, characters: "", charactersIgnoringModifiers: "",
                                    isARepeat: false, keyCode: code) {
            NSApp.postEvent(e, atStart: false)
        }
    }

    /// Posts flagsChanged events pressing the modifiers one at a time, then releasing them in reverse, 50 ms apart.
    private static func postFlags(_ mods: [String], panel: NSPanel) {
        let table: [String: (CGEventFlags, CGKeyCode)] = ["ctrl": (.maskControl, 59), "shift": (.maskShift, 56),
                                                          "cmd": (.maskCommand, 55), "opt": (.maskAlternate, 58)]
        var flags: CGEventFlags = []
        var steps: [(CGEventFlags, CGKeyCode)] = []
        for m in mods { if let (f, k) = table[m] { flags.insert(f); steps.append((flags, k)) } }
        for m in mods.reversed() { if let (f, k) = table[m] { flags.remove(f); steps.append((flags, k)) } }
        let t0 = ProcessInfo.processInfo.systemUptime
        for (i, (f, k)) in steps.enumerated() {
            guard let cg = CGEvent(keyboardEventSource: nil, virtualKey: k, keyDown: true) else { continue }
            cg.type = .flagsChanged
            cg.flags = f
            cg.timestamp = CGEventTimestamp((t0 + Double(i) * 0.05) * 1_000_000_000)
            if let e = NSEvent(cgEvent: cg) { NSApp.postEvent(e, atStart: false) }
        }
    }

    /// The window as composited on screen (glass included). Capturing our own windows needs no permission.
    @available(macOS, deprecated: 14.0)
    private static func windowImage(_ number: Int) -> CGImage? {
        CGWindowListCreateImage(.null, .optionIncludingWindow, CGWindowID(number), [.boundsIgnoreFraming, .bestResolution])
    }

    private static func write(_ img: NSImage, to path: String) {
        guard let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: URL(fileURLWithPath: path))
    }
}
