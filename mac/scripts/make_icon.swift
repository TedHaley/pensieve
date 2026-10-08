// Draws the app icon (a glowing memory orb over a stone basin, with a faint point cloud) into an .iconset.
// Usage: make_icon <out.iconset>
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

func draw(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let ctx = NSGraphicsContext.current!.cgContext
    let s = CGFloat(px) / 1024
    ctx.scaleBy(x: s, y: s)

    // Squircle-ish tile on Apple's icon grid (824pt body, 100pt margin).
    let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
    let body = NSBezierPath(roundedRect: tile, xRadius: 186, yRadius: 186)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
    shadow.shadowBlurRadius = 24
    shadow.shadowOffset = NSSize(width: 0, height: -10)
    shadow.set()
    rgb(0x0E0F1F).setFill()
    body.fill()
    NSGraphicsContext.restoreGraphicsState()

    NSGraphicsContext.saveGraphicsState()
    body.addClip()
    NSGradient(colors: [rgb(0x1B1E3D), rgb(0x0B0C18)])!.draw(in: tile, angle: -90)

    // Faint point cloud.
    var seed: UInt64 = 7
    func rnd() -> CGFloat {
        seed = seed &* 6364136223846793005 &+ 1442695040888963407
        return CGFloat(seed >> 33) / CGFloat(1 << 31)
    }
    let clusters: [(CGPoint, UInt32)] = [(CGPoint(x: 300, y: 690), 0x7AA2FF), (CGPoint(x: 720, y: 720), 0xB58CFF),
                                         (CGPoint(x: 760, y: 470), 0x5FE0D0), (CGPoint(x: 270, y: 450), 0xFFB27A)]
    for (c, col) in clusters {
        for _ in 0..<38 {
            let a = rnd() * .pi * 2, r = pow(rnd(), 0.7) * 95
            let d = 5 + rnd() * 7
            rgb(col, 0.25 + rnd() * 0.45).setFill()
            NSBezierPath(ovalIn: NSRect(x: c.x + cos(a) * r - d / 2, y: c.y + sin(a) * r * 0.8 - d / 2, width: d, height: d)).fill()
        }
    }

    // Basin: a shallow bowl with a lit rim.
    let basin = NSRect(x: 250, y: 215, width: 524, height: 150)
    NSGradient(colors: [rgb(0x2A2E55), rgb(0x14162C)])!.draw(in: NSBezierPath(ovalIn: basin), angle: -90)
    let pool = NSRect(x: 290, y: 262, width: 444, height: 92)
    NSGradient(colors: [rgb(0x9FD8FF, 0.95), rgb(0x5A7CFF, 0.65), rgb(0x2B2F6B, 0.2)])!
        .draw(in: NSBezierPath(ovalIn: pool), relativeCenterPosition: NSPoint(x: 0, y: 0.2))
    rgb(0xBFD9FF, 0.55).setStroke()
    let rim = NSBezierPath(ovalIn: basin.insetBy(dx: 2, dy: 2))
    rim.lineWidth = 5
    rim.stroke()

    // Rising wisp from the pool to the orb.
    let wisp = NSBezierPath()
    wisp.move(to: NSPoint(x: 512, y: 310))
    wisp.curve(to: NSPoint(x: 512, y: 540), controlPoint1: NSPoint(x: 440, y: 390), controlPoint2: NSPoint(x: 590, y: 460))
    wisp.lineWidth = 16
    wisp.lineCapStyle = .round
    rgb(0xA9C8FF, 0.35).setStroke()
    wisp.stroke()

    // Orb glow and core.
    let center = CGPoint(x: 512, y: 600)
    NSGradient(colors: [rgb(0x9CC4FF, 0.55), rgb(0x6C7BFF, 0.18), rgb(0x6C7BFF, 0)])!
        .draw(in: NSBezierPath(ovalIn: NSRect(x: center.x - 250, y: center.y - 250, width: 500, height: 500)),
              relativeCenterPosition: .zero)
    NSGradient(colors: [rgb(0xFFFFFF), rgb(0xCFE3FF), rgb(0x86A8FF)])!
        .draw(in: NSBezierPath(ovalIn: NSRect(x: center.x - 112, y: center.y - 112, width: 224, height: 224)),
              relativeCenterPosition: NSPoint(x: -0.3, y: 0.35))
    NSGraphicsContext.restoreGraphicsState()

    // Hairline edge.
    rgb(0xFFFFFF, 0.10).setStroke()
    body.lineWidth = 3
    body.stroke()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                   ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
    try! draw(px).write(to: out.appendingPathComponent("icon_\(name).png"))
}
