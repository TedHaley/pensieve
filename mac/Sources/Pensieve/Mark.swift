import AppKit

/// The Pensieve mark from pensieve.tools (SVG viewBox 0 0 24 24): a P of three circles, a small one at each end
/// of the stem and a large one for the bowl, filled even-odd so where two overlap is cut out.
/// scripts/make_icon.swift draws the same shape for the app icon.
enum Mark {
    typealias Circle = (x: Double, y: Double, r: Double)
    static let circles: [Circle] = [(7.97, 5.06, 3.06), (7.97, 18.94, 3.06), (12.97, 8.11, 6.11)]

    /// The circles while Pensieve indexes, `t` seconds in: the mark breathes once every 2 s. The bowl swells as
    /// the small circles shrink and move away from it, then the other way round.
    /// `blend` fades the motion in from the still P (0) to full (1). The same motion is Breathe in the site's logo lab.
    static func breathing(_ t: Double, blend: Double) -> [Circle] {
        let k = sin(t / 2 * 2 * .pi) * blend * blend * (3 - 2 * blend)
        let (top, bottom, bowl) = (circles[0], circles[1], circles[2])
        return [(top.x - 0.9 * k, top.y - 0.5 * k, top.r * (1 - 0.12 * k)),
                (bottom.x, bottom.y + 0.6 * k, bottom.r * (1 - 0.12 * k)),
                (bowl.x, bowl.y, bowl.r * (1 + 0.06 * k))]
    }

    /// Draws `shape` into `rect` in a flipped (y-down) context, matching the SVG's coordinates.
    static func draw(in rect: NSRect, color: NSColor, shape: [Circle] = circles) {
        let s = rect.width / 24
        color.set()
        let path = NSBezierPath()
        path.windingRule = .evenOdd
        for d in shape {
            let x = rect.minX + d.x * s, y = rect.minY + d.y * s, r = d.r * s
            path.appendOval(in: NSRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
        }
        path.fill()
    }

    /// Monochrome template for the menu bar (tinted by the system for light/dark menu bars). Draws `shape` when
    /// given, and the logo otherwise.
    static func menuBarImage(size: CGFloat = 18, shape: [Circle]? = nil) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size), flipped: true) { r in
            draw(in: r, color: .black, shape: shape ?? circles)
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = shape == nil ? "Pensieve" : "Pensieve, indexing"
        return img
    }
}
