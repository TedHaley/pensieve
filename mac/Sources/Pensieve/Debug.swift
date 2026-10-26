import AppKit
import WebKit

/// Development aid, active only when PENSIEVE_DEBUG is set: `pensieve://snapshot?target=statusitem|visualizer|url&path=/x.png`
/// writes that view to a PNG (the URL as text).
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

    static func snapshot(_ url: URL, web: WKWebView?) {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        guard let path = items.first(where: { $0.name == "path" })?.value else { return }
        let target = items.first(where: { $0.name == "target" })?.value ?? "visualizer"
        if target == "url" {  // the visualizer's current URL, as text
            try? (web?.url?.absoluteString ?? "none").write(toFile: path, atomically: true, encoding: .utf8)
            return
        }
        web?.takeSnapshot(with: nil) { img, _ in
            if let img { write(img, to: path) }
        }
    }

    /// A menu bar image drawn large on white, since its window isn't ours to capture.
    static func writeEnlarged(_ img: NSImage, scale: CGFloat = 6, to path: String) {
        let size = NSSize(width: img.size.width * scale, height: img.size.height * scale)
        write(NSImage(size: size, flipped: false) { r in
            NSColor.white.setFill()
            r.fill()
            img.draw(in: r)
            return true
        }, to: path)
    }

    private static func write(_ img: NSImage, to path: String) {
        guard let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: URL(fileURLWithPath: path))
    }
}
