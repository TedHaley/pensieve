import AppKit
import WebKit

/// Development aid, active only when PENSIEVE_DEBUG is set: `pensieve://snapshot?target=panel|visualizer&path=/x.png`
/// renders that window to a PNG.
@MainActor
enum Debug {
    static var enabled: Bool { ProcessInfo.processInfo.environment["PENSIEVE_DEBUG"] != nil }

    static func snapshot(_ url: URL, panel: NSPanel, web: WKWebView?) {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        guard let path = items.first(where: { $0.name == "path" })?.value else { return }
        let target = items.first(where: { $0.name == "target" })?.value ?? "panel"
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
