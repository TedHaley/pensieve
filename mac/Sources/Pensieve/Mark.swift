import AppKit

/// The Pensieve mark from pensieve.tools (SVG viewBox 0 0 24 24): a ring with three dots of memory.
/// scripts/make_icon.swift draws the same shape for the app icon.
enum Mark {
    static let ring = (cx: 12.0, cy: 12.0, r: 10.0, stroke: 1.6)
    static let dots: [(x: Double, y: Double, r: Double)] = [(9, 10, 1.6), (15, 9, 1.2), (13, 15, 1.9)]

    /// Draws into `rect` in a flipped (y-down) context, matching the SVG's coordinates.
    /// `spin` turns the dots around the ring's center, in radians.
    static func draw(in rect: NSRect, color: NSColor, strokeScale: Double = 1, spin: Double = 0) {
        let s = rect.width / 24
        func p(_ x: Double, _ y: Double) -> NSPoint { NSPoint(x: rect.minX + x * s, y: rect.minY + y * s) }
        color.set()
        let r = ring.r * s
        let c = p(ring.cx, ring.cy)
        let circle = NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
        circle.lineWidth = ring.stroke * s * strokeScale
        circle.stroke()
        for d in dots {
            let dx = d.x - ring.cx, dy = d.y - ring.cy
            let dc = p(ring.cx + dx * cos(spin) - dy * sin(spin), ring.cy + dx * sin(spin) + dy * cos(spin)), dr = d.r * s
            NSBezierPath(ovalIn: NSRect(x: dc.x - dr, y: dc.y - dr, width: 2 * dr, height: 2 * dr)).fill()
        }
    }

    /// Monochrome template for the menu bar (tinted by the system for light/dark menu bars). While indexing, the
    /// caller redraws it with a growing `spin` so the dots swirl in the ring.
    static func menuBarImage(size: CGFloat = 18, spin: Double = 0) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size), flipped: true) { r in
            draw(in: r, color: .black, strokeScale: 1.1, spin: spin)
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = spin == 0 ? "Pensieve" : "Pensieve, indexing"
        return img
    }
}
