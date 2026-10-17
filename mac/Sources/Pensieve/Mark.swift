import AppKit

/// The Pensieve mark from pensieve.tools (SVG viewBox 0 0 24 24): a P of three circles, a small one at each end
/// of the stem and a large one for the bowl, filled even-odd so where two overlap is cut out.
/// scripts/make_icon.swift draws the same shape for the app icon.
enum Mark {
    static let circles: [(x: Double, y: Double, r: Double)] = [(7.97, 5.06, 3.06), (7.97, 18.94, 3.06), (12.97, 8.11, 6.11)]

    /// Draws into `rect` in a flipped (y-down) context, matching the SVG's coordinates.
    /// `spin` turns the whole mark around the box's center, in radians.
    static func draw(in rect: NSRect, color: NSColor, spin: Double = 0) {
        let s = rect.width / 24
        func p(_ x: Double, _ y: Double) -> NSPoint { NSPoint(x: rect.minX + x * s, y: rect.minY + y * s) }
        color.set()
        let path = NSBezierPath()
        path.windingRule = .evenOdd
        for d in circles {
            let dx = d.x - 12, dy = d.y - 12
            let dc = p(12 + dx * cos(spin) - dy * sin(spin), 12 + dx * sin(spin) + dy * cos(spin)), dr = d.r * s
            path.appendOval(in: NSRect(x: dc.x - dr, y: dc.y - dr, width: 2 * dr, height: 2 * dr))
        }
        path.fill()
    }

    /// Monochrome template for the menu bar (tinted by the system for light/dark menu bars). While indexing, the
    /// caller redraws it with a growing `spin` so the mark turns.
    static func menuBarImage(size: CGFloat = 18, spin: Double = 0) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size), flipped: true) { r in
            draw(in: r, color: .black, spin: spin)
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = spin == 0 ? "Pensieve" : "Pensieve, indexing"
        return img
    }
}
