// Draws the DMG window's background: the Pensieve mark and name, and an arrow from the app to Applications with
// "Drag to install". Light, so Finder's dark icon labels stay readable. Positions match dmg.sh's Finder layout.
// Usage: make_dmg_background <out.png> <scale>   (1 for the 640×400 pt image, 2 for its Retina copy)
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments[1])
let scale = CGFloat(Double(CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "1") ?? 1)
let size = NSSize(width: 640, height: 400)
let appX: CGFloat = 160, appsX: CGFloat = 480, iconY: CGFloat = 200  // icon centers, as in dmg.sh

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}
let ink = rgb(0x1E1E1C)

/// The mark (ring and three dots, SVG viewBox 0 0 24 24) in a y-down context.
func mark(in r: NSRect, color: NSColor, stroke: CGFloat = 1.6) {
    let s = r.width / 24
    color.set()
    let ring = NSBezierPath(ovalIn: NSRect(x: r.minX + 2 * s, y: r.minY + 2 * s, width: 20 * s, height: 20 * s))
    ring.lineWidth = stroke * s
    ring.stroke()
    for (x, y, d) in [(9.0, 10.0, 1.6), (15.0, 9.0, 1.2), (13.0, 15.0, 1.9)] {
        NSBezierPath(ovalIn: NSRect(x: r.minX + CGFloat(x - d) * s, y: r.minY + CGFloat(y - d) * s,
                                    width: CGFloat(2 * d) * s, height: CGFloat(2 * d) * s)).fill()
    }
}

func text(_ s: String, size: CGFloat, weight: NSFont.Weight, color: NSColor, centerX: CGFloat, y: CGFloat) {
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: color]
    let w = (s as NSString).size(withAttributes: attrs).width
    (s as NSString).draw(at: NSPoint(x: centerX - w / 2, y: y), withAttributes: attrs)
}

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
rep.size = size  // so Finder shows the 2x copy at 640×400 pt; the context then draws in points
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext
ctx.translateBy(x: 0, y: size.height)  // y-down, like Finder's icon positions
ctx.scaleBy(x: 1, y: -1)
NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: true)

NSGradient(colors: [rgb(0xF8F7F3), rgb(0xECEAE3)])!.draw(in: NSRect(origin: .zero, size: size), angle: 90)

let titleAttrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 24, weight: .semibold)]
let titleW = ("Pensieve" as NSString).size(withAttributes: titleAttrs).width
let markSide: CGFloat = 28, gap: CGFloat = 10
let left = (size.width - markSide - gap - titleW) / 2  // the mark and the name are centered together
mark(in: NSRect(x: left, y: 36, width: markSide, height: markSide), color: ink)
text("Pensieve", size: 24, weight: .semibold, color: ink, centerX: left + markSide + gap + titleW / 2, y: 34)
text("Search everything on your Mac by meaning.", size: 13, weight: .regular, color: ink.withAlphaComponent(0.55),
     centerX: size.width / 2, y: 74)

let a = NSPoint(x: appX + 78, y: iconY), b = NSPoint(x: appsX - 78, y: iconY)
let arc = NSBezierPath()
arc.move(to: a)
arc.curve(to: b, controlPoint1: NSPoint(x: a.x + 40, y: iconY - 26), controlPoint2: NSPoint(x: b.x - 40, y: iconY - 26))
arc.lineWidth = 2.5
arc.lineCapStyle = .round
arc.setLineDash([7, 6], count: 2, phase: 0)
ink.withAlphaComponent(0.45).setStroke()
arc.stroke()
let head = NSBezierPath()  // along the curve's end direction, which comes in from the upper left
head.move(to: NSPoint(x: b.x - 13, y: b.y - 11))
head.line(to: b)
head.line(to: NSPoint(x: b.x - 15, y: b.y + 5))
head.lineWidth = 2.5
head.lineCapStyle = .round
head.lineJoinStyle = .round
head.stroke()
text("Drag to install", size: 14, weight: .semibold, color: ink.withAlphaComponent(0.75), centerX: size.width / 2, y: iconY - 52)

text("Then open Pensieve from Applications.", size: 12, weight: .regular, color: ink.withAlphaComponent(0.5),
     centerX: size.width / 2, y: 342)

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: out)
