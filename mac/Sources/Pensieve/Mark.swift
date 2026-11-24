import AppKit

/// The Pensieve mark from pensieve.tools (SVG viewBox 0 0 24 24): a P of three circles, a small one at each end
/// of the stem and a large one for the bowl, filled even-odd so that the overlap of two circles is cut out.
/// The app icon (mac/build/AppIcon.icns) shows the same shape.
enum Mark {
    typealias Circle = (x: Double, y: Double, r: Double)
    static let circles: [Circle] = [(7.97, 5.06, 3.06), (7.97, 18.94, 3.06), (12.97, 8.11, 6.11)]

    /// Draws the mark into `rect` in a flipped (y-down) context, which matches the SVG's coordinates.
    static func draw(in rect: NSRect, color: NSColor) {
        let s = rect.width / 24
        color.set()
        let path = NSBezierPath()
        path.windingRule = .evenOdd
        for d in circles {
            let x = rect.minX + d.x * s, y = rect.minY + d.y * s, r = d.r * s
            path.appendOval(in: NSRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
        }
        path.fill()
    }

    /// A template image: the system tints it for light and dark menu bars.
    static func menuBarImage(size: CGFloat = 18) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size), flipped: true) { r in
            draw(in: r, color: .black)
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = "Pensieve"
        return img
    }
}
