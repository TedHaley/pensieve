import AppKit
import Carbon.HIToolbox

/// Global hotkeys. Two kinds:
/// - a modifier-only tap like "ctrl+shift" (press together, release, no other key), which needs Accessibility
///   because it watches every key event;
/// - a regular combo like "cmd+shift+space", registered with Carbon, which needs no permission.
/// Control+Shift+Space is always registered as well, so the panel is reachable before Accessibility is granted.
@MainActor
final class HotKeys {
    static let shared = HotKeys()
    var action: (() -> Void)?

    private(set) var spec = ""
    private var refs: [EventHotKeyRef] = []
    private var handlerInstalled = false
    private var monitors: [Any] = []
    private var tapMods: NSEvent.ModifierFlags = []
    private var armedAt: TimeInterval?

    static let fallback = "ctrl+shift+space"

    struct Parsed {
        var flags: NSEvent.ModifierFlags = []
        var carbon: UInt32 = 0
        var key: UInt32?
        var keyName = ""
    }

    func configure(_ newSpec: String) {
        let s = newSpec.lowercased().replacingOccurrences(of: " ", with: "")
        guard s != spec else { return }
        spec = s
        for r in refs { UnregisterEventHotKey(r) }
        refs.removeAll()
        installHandler()
        let fb = Self.parse(Self.fallback)
        register(fb.carbon, fb.key!)
        let p = Self.parse(s)
        tapMods = []
        if let key = p.key {
            if !(key == fb.key && p.carbon == fb.carbon) { register(p.carbon, key) }
        } else if !p.flags.isEmpty {
            tapMods = p.flags
            startTapMonitor()
        }
    }

    var needsAccessibility: Bool { !tapMods.isEmpty && !AXIsProcessTrusted() }

    @discardableResult
    static func requestAccessibility(prompt: Bool) -> Bool {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        return AXIsProcessTrustedWithOptions([key: prompt] as CFDictionary)
    }

    /// "ctrl+shift" -> "⌃⇧", "cmd+shift+space" -> "⇧⌘Space"
    static func display(_ spec: String) -> String {
        let p = parse(spec)
        var s = ""
        if p.flags.contains(.control) { s += "⌃" }
        if p.flags.contains(.option) { s += "⌥" }
        if p.flags.contains(.shift) { s += "⇧" }
        if p.flags.contains(.command) { s += "⌘" }
        return s + p.keyName
    }

    static func parse(_ spec: String) -> Parsed {
        var p = Parsed()
        for raw in spec.lowercased().split(separator: "+").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            switch raw {
            case "cmd", "command", "⌘": p.flags.insert(.command); p.carbon |= UInt32(cmdKey)
            case "ctrl", "control", "⌃": p.flags.insert(.control); p.carbon |= UInt32(controlKey)
            case "opt", "option", "alt", "⌥": p.flags.insert(.option); p.carbon |= UInt32(optionKey)
            case "shift", "⇧": p.flags.insert(.shift); p.carbon |= UInt32(shiftKey)
            case "space": p.key = UInt32(kVK_Space); p.keyName = "Space"
            default:
                if let code = keyCodes[raw] { p.key = UInt32(code); p.keyName = raw.uppercased() }
            }
        }
        return p
    }

    private static let keyCodes: [String: Int] = [
        "a": kVK_ANSI_A, "b": kVK_ANSI_B, "c": kVK_ANSI_C, "d": kVK_ANSI_D, "e": kVK_ANSI_E, "f": kVK_ANSI_F,
        "g": kVK_ANSI_G, "h": kVK_ANSI_H, "i": kVK_ANSI_I, "j": kVK_ANSI_J, "k": kVK_ANSI_K, "l": kVK_ANSI_L,
        "m": kVK_ANSI_M, "n": kVK_ANSI_N, "o": kVK_ANSI_O, "p": kVK_ANSI_P, "q": kVK_ANSI_Q, "r": kVK_ANSI_R,
        "s": kVK_ANSI_S, "t": kVK_ANSI_T, "u": kVK_ANSI_U, "v": kVK_ANSI_V, "w": kVK_ANSI_W, "x": kVK_ANSI_X,
        "y": kVK_ANSI_Y, "z": kVK_ANSI_Z, "0": kVK_ANSI_0, "1": kVK_ANSI_1, "2": kVK_ANSI_2, "3": kVK_ANSI_3,
        "4": kVK_ANSI_4, "5": kVK_ANSI_5, "6": kVK_ANSI_6, "7": kVK_ANSI_7, "8": kVK_ANSI_8, "9": kVK_ANSI_9,
    ]

    // MARK: Carbon combos

    private func installHandler() {
        guard !handlerInstalled else { return }
        var type = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            Task { @MainActor in HotKeys.shared.action?() }
            return noErr
        }, 1, &type, nil, nil)
        handlerInstalled = true
    }

    private func register(_ mods: UInt32, _ key: UInt32) {
        var ref: EventHotKeyRef?
        let id = EventHotKeyID(signature: OSType(0x5053_4E56), id: UInt32(refs.count + 1))  // 'PSNV'
        if RegisterEventHotKey(key, mods, id, GetApplicationEventTarget(), 0, &ref) == noErr, let ref {
            refs.append(ref)
        }
    }

    // MARK: modifier-only tap

    private func startTapMonitor() {
        guard monitors.isEmpty else { return }
        let mask: NSEvent.EventTypeMask = [.flagsChanged, .keyDown]
        if let g = NSEvent.addGlobalMonitorForEvents(matching: mask, handler: { e in
            MainActor.assumeIsolated { HotKeys.shared.handle(e) }
        }) { monitors.append(g) }
        if let l = NSEvent.addLocalMonitorForEvents(matching: mask, handler: { e in
            MainActor.assumeIsolated { HotKeys.shared.handle(e) }
            return e
        }) { monitors.append(l) }
    }

    private func handle(_ e: NSEvent) {
        guard !tapMods.isEmpty else { return }
        if e.type == .keyDown {  // ctrl+shift+T etc. is a shortcut, not a tap
            armedAt = nil
            return
        }
        let m = e.modifierFlags.intersection([.command, .control, .option, .shift])
        if m == tapMods {
            armedAt = e.timestamp
        } else if m.isEmpty {
            if let t = armedAt, e.timestamp - t < 0.6 { action?() }
            armedAt = nil
        } else if !tapMods.isSuperset(of: m) {
            armedAt = nil
        }
    }
}
