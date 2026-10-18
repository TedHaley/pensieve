// Draws the app icon into an .iconset: the Pensieve mark from pensieve.tools (a P of three circles filled
// even-odd, SVG viewBox 0 0 24 24) in the site's light ink on its dark background, on Apple's icon grid.
// Usage: make_icon <out.iconset>
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

let ink = rgb(0xF4F3EE)
let bgTop = rgb(0x1E1E1C), bgBottom = rgb(0x121211)

func draw(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let ctx = NSGraphicsContext.current!.cgContext
    ctx.scaleBy(x: CGFloat(px) / 1024, y: CGFloat(px) / 1024)

    // Rounded-square body on the macOS icon grid (824pt, 100pt margin), with the system-style drop shadow.
    let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
    let body = NSBezierPath(roundedRect: tile, xRadius: 186, yRadius: 186)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.3)
    shadow.shadowBlurRadius = 20
    shadow.shadowOffset = NSSize(width: 0, height: -8)
    shadow.set()
    bgBottom.setFill()
    body.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(colors: [bgTop, bgBottom])!.draw(in: body, angle: -90)

    // The mark, y-down like the SVG. Its box is centered, but the small stem circles make it look left of center,
    // so it sits a little to the right.
    let side: CGFloat = 600, nudge: CGFloat = 20
    let m = NSRect(x: 512 - side / 2 + nudge, y: 512 - side / 2, width: side, height: side)
    ctx.saveGState()
    ctx.translateBy(x: 0, y: 1024)
    ctx.scaleBy(x: 1, y: -1)
    let s = side / 24
    ink.set()
    let mark = NSBezierPath()
    mark.windingRule = .evenOdd
    for (x, y, r) in [(7.97, 5.06, 3.06), (7.97, 18.94, 3.06), (12.97, 8.11, 6.11)] {
        mark.appendOval(in: NSRect(x: m.minX + CGFloat(x - r) * s, y: m.minY + CGFloat(y - r) * s,
                                   width: CGFloat(2 * r) * s, height: CGFloat(2 * r) * s))
    }
    mark.fill()
    ctx.restoreGState()

    // Hairline edge so the dark tile reads on a dark Dock.
    rgb(0xFFFFFF, 0.09).setStroke()
    let edge = NSBezierPath(roundedRect: tile.insetBy(dx: 1.5, dy: 1.5), xRadius: 185, yRadius: 185)
    edge.lineWidth = 3
    edge.stroke()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                   ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
    try! draw(px).write(to: out.appendingPathComponent("icon_\(name).png"))
}
