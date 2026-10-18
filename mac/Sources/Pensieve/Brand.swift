import AppKit
import SwiftUI

/// The pensieve.tools look: warm paper and ink, Inter Tight and JetBrains Mono. The web UI uses the same values
/// (pensieve/static/app.css), so the panel and the main window match.
enum Brand {
    static let ink = NSColor(srgbRed: 0x12 / 255, green: 0x12 / 255, blue: 0x11 / 255, alpha: 1)
    static let paper = NSColor(srgbRed: 0xf4 / 255, green: 0xf3 / 255, blue: 0xee / 255, alpha: 1)

    /// Selection and primary actions: ink in light mode, paper in dark mode.
    static let accent = Color(nsColor: adaptive(light: ink, dark: paper))
    static let onAccent = Color(nsColor: adaptive(light: paper, dark: ink))
    /// A wash of the page color over the panel's glass, so it reads warm rather than grey.
    static let glassTint = Color(nsColor: adaptive(light: paper.withAlphaComponent(0.35), dark: ink.withAlphaComponent(0.35)))

    private static func adaptive(light: NSColor, dark: NSColor) -> NSColor {
        NSColor(name: nil) { $0.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light }
    }

    private static var fontsLoaded = false

    /// Registers the bundled fonts for this process. Without them (a `swift run` build) the system fonts stand in.
    static func registerFonts() {
        for name in ["InterTight", "JetBrainsMono"] {
            guard let url = Bundle.main.url(forResource: name, withExtension: "woff2") else { continue }
            if CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil) { fontsLoaded = true }
        }
    }

    static func font(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        fontsLoaded ? .custom("Inter Tight", size: size).weight(weight) : .system(size: size, weight: weight)
    }

    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        fontsLoaded ? .custom("JetBrains Mono", size: size).weight(weight) : .system(size: size, weight: weight, design: .monospaced)
    }
}
