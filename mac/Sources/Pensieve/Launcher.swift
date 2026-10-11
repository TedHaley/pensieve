import AppKit
import CoreAudio
import CoreWLAN
import SwiftUI

/// Apps, System Settings and quick toggles, found by name in the search panel like Spotlight. This index lives only
/// in the app: the backend never sees it, so none of it shows on the map.
@MainActor
final class Launcher {
    static let shared = Launcher()

    struct Item: Sendable {
        enum Kind: Sendable { case app, pane, setting, toggle }
        let id: String
        let kind: Kind
        let title: String
        let subtitle: String
        let path: String?  // the .app bundle
        let url: String?  // x-apple.systempreferences:…
        let symbol: String?  // SF Symbol drawn on a colored tile, like System Settings
        let color: String?
        let name: Match
        let keywords: [String]  // normalized words that also find the item

        init(id: String, kind: Kind, title: String, subtitle: String, path: String? = nil, url: String? = nil,
             symbol: String? = nil, color: String? = nil, keywords: [String] = []) {
            self.id = id
            self.kind = kind
            self.title = title
            self.subtitle = subtitle
            self.path = path
            self.url = url
            self.symbol = symbol
            self.color = color
            name = Match(title)
            self.keywords = Array(Set(keywords.flatMap { Match.words($0) }))
        }
    }

    /// A normalized name: lowercase, no accents, punctuation as spaces ("Wi‑Fi" -> "wi fi", compact "wifi").
    struct Match: Sendable {
        let norm: String
        let compact: String
        let words: [String]
        let initials: String

        init(_ s: String) {
            words = Match.words(s)
            norm = words.joined(separator: " ")
            compact = words.joined()
            initials = String(words.compactMap(\.first))
        }

        static func words(_ s: String) -> [String] {
            s.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
                .split { !$0.isLetter && !$0.isNumber }.map(String.init)
        }
    }

    struct Results {
        var hits: [Hit] = []
        var top = false  // the first hit is a strong name match: show it as the Top Hit
    }

    static let maxApps = 5
    static let maxSettings = 4
    static let settingsApp = "/System/Applications/System Settings.app"

    private var items: [Item] = []
    private var byID: [String: Item] = [:]
    private var scannedAt: Date?
    private var scanning = false
    private var uses: [String: Int] = Prefs.store.dictionary(forKey: "launcherUses") as? [String: Int] ?? [:]
    var onScan: (() -> Void)?  // a query typed before the first scan finished needs matching again

    /// Rescan when the panel opens and the last scan is a few minutes old (apps get installed and removed).
    func refreshIfStale() {
        guard !scanning, scannedAt.map({ -$0.timeIntervalSinceNow > 300 }) ?? true else { return }
        scanning = true
        Task.detached(priority: .userInitiated) {
            let found = Self.scanApps() + Self.scanSettings() + Self.toggles
            await MainActor.run {
                let launcher = Launcher.shared
                launcher.items = found
                launcher.byID = Dictionary(found.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
                launcher.scannedAt = Date()
                launcher.scanning = false
                launcher.onScan?()
            }
        }
    }

    func search(_ q: String) -> Results {
        let query = Match(q)
        guard !query.compact.isEmpty else { return Results() }
        var scored: [(Item, Int)] = []
        for it in items {
            guard var s = Self.score(it, query) else { continue }
            s += min(120, Int(35 * log2(1 + Double(uses[it.id] ?? 0))))  // what you open often comes first
            switch it.kind {
            case .app: s += 30
            case .toggle: s += 20
            case .pane: s += 10
            case .setting: break
            }
            scored.append((it, s))
        }
        scored.sort { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0.title.count < $1.0.title.count }
        var apps = 0, settings = 0
        var out = Results()
        for (it, s) in scored {
            if it.kind == .app {
                guard apps < Self.maxApps else { continue }
                apps += 1
            } else {
                guard settings < Self.maxSettings else { continue }
                settings += 1
            }
            if out.hits.isEmpty { out.top = s >= 750 }
            out.hits.append(hit(it))
        }
        return out
    }

    /// Spotlight-like ranking: whole name, then name prefix, then word prefixes, initials, inside the name, and
    /// finally the item's keywords. nil = no match.
    private static func score(_ it: Item, _ q: Match) -> Int? {
        let n = it.name
        let extra = min(60, max(0, n.compact.count - q.compact.count))
        if n.norm == q.norm || n.compact == q.compact { return 1000 }
        if n.norm.hasPrefix(q.norm) || n.compact.hasPrefix(q.compact) { return 900 - extra }
        if q.words.allSatisfy({ w in n.words.contains { $0.hasPrefix(w) } }) { return 780 - extra }
        // initials find "vsc" -> Visual Studio Code; on long setting sentences they mostly find noise
        if q.compact.count >= 2, q.words.count == 1, it.kind == .app || it.kind == .pane, n.initials.hasPrefix(q.compact) {
            return 680 - extra
        }
        if q.compact.count >= 3, n.compact.contains(q.compact) { return 500 - extra }
        // short fragments match too many keywords ("saf" -> "safety"), so those must be whole words
        if q.compact.count >= 3, !it.keywords.isEmpty, q.words.allSatisfy({ w in
            it.keywords.contains { $0 == w || (w.count >= 4 && $0.hasPrefix(w)) } || n.words.contains { $0.hasPrefix(w) }
        }) {
            let whole = q.words.allSatisfy { it.keywords.contains($0) }  // "dark" names Appearance; "darker" doesn't
            return (whole ? 370 : 350) - extra
        }
        return nil
    }

    private func hit(_ it: Item) -> Hit {
        var title = it.title, sub = it.subtitle
        if it.kind == .toggle, let t = Self.toggleText(it.id) { (title, sub) = t }
        return Hit(id: it.id, kind: it.kind == .app ? "app" : "setting", title: title, subtitle: sub, path: it.path,
                   line: nil, snippet: nil, highlights: nil, score: nil, match: nil, ext: nil, isDir: nil)
    }

    func handles(_ hit: Hit) -> Bool { hit.kind == "app" || hit.kind == "setting" }

    /// The SF Symbol and tile color for a settings result, or nil to use System Settings' own icon.
    func glyph(_ hit: Hit) -> (symbol: String, color: Color)? {
        guard let it = byID[hit.id], let sym = it.symbol, NSImage(systemSymbolName: sym, accessibilityDescription: nil) != nil
        else { return nil }
        return (sym, Self.color(it.color))
    }

    func run(_ hit: Hit) {
        guard let it = byID[hit.id] else { return }
        uses[it.id, default: 0] += 1
        Prefs.store.set(uses, forKey: "launcherUses")
        switch it.kind {
        case .app:
            guard let p = it.path else { return }
            NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: p), configuration: NSWorkspace.OpenConfiguration())
        case .pane, .setting:
            if let s = it.url, let u = URL(string: s) { NSWorkspace.shared.open(u) }
        case .toggle:
            Self.runToggle(it.id)
        }
    }

    func isToggle(_ hit: Hit) -> Bool { byID[hit.id]?.kind == .toggle }

    /// Shows a toggle's pane instead of switching it.
    func openSettings(_ hit: Hit) {
        guard let it = byID[hit.id], let s = it.url, let u = URL(string: s) else { return }
        NSWorkspace.shared.open(u)
    }

    nonisolated private static func scanApps() -> [Item] {
        let fm = FileManager.default
        let home = NSHomeDirectory()
        let roots = ["/Applications", "/System/Applications", "\(home)/Applications", "/System/Library/CoreServices/Applications"]
        var seen = Set<String>()
        var out: [Item] = []
        func add(_ path: String) {
            guard seen.insert(path).inserted else { return }
            var name = fm.displayName(atPath: path)
            if name.hasSuffix(".app") { name.removeLast(4) }
            let info = NSDictionary(contentsOfFile: path + "/Contents/Info.plist")
            if (info?["LSBackgroundOnly"] as? Bool) == true { return }
            let alt = [info?["CFBundleName"] as? String, info?["CFBundleDisplayName"] as? String,
                       (path as NSString).lastPathComponent.replacingOccurrences(of: ".app", with: "")].compactMap { $0 }
            let dir = (path as NSString).deletingLastPathComponent
            let real = (path as NSString).resolvingSymlinksInPath  // Safari is a link into a cryptex; its icon got an alias badge
            out.append(Item(id: "app:\(path)", kind: .app, title: name,
                            subtitle: dir.hasPrefix(home) ? "~" + dir.dropFirst(home.count) : dir, path: real, keywords: alt))
        }
        func walk(_ dir: String, depth: Int) {
            guard let names = try? fm.contentsOfDirectory(atPath: dir) else { return }
            for n in names where !n.hasPrefix(".") {
                let p = dir + "/" + n
                if n.hasSuffix(".app") {
                    add(p)
                } else if depth > 0, (try? fm.attributesOfItem(atPath: p)[.type] as? FileAttributeType) == .typeDirectory {
                    walk(p, depth: depth - 1)  // Utilities, vendor folders like /Applications/Setapp
                }
            }
        }
        for r in roots { walk(r, depth: 2) }
        add("/System/Library/CoreServices/Finder.app")
        return out
    }

    /// Each System Settings pane is an app extension whose Info.plist names it, its icon, and a searchTerms file
    /// listing the individual settings inside it (the same index System Settings' own search uses).
    nonisolated private static func scanSettings() -> [Item] {
        let dir = URL(fileURLWithPath: "/System/Library/ExtensionKit/Extensions")
        guard let exts = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return [] }
        let renames = ["com.apple.Battery-Settings.extension": "Battery", "com.apple.HeadphoneSettings": "Headphones",
                       "com.apple.systempreferences.AppleIDSettings": "Apple Account"]
        var out: [Item] = []
        for u in exts where u.pathExtension == "appex" {
            guard let b = Bundle(url: u), let info = b.infoDictionary, let id = info["CFBundleIdentifier"] as? String,
                  let ex = info["EXAppExtensionAttributes"] as? [String: Any],
                  ex["EXExtensionPointIdentifier"] as? String == "com.apple.Settings.extension.ui",
                  let attrs = ex["SettingsExtensionAttributes"] as? [String: Any],
                  attrs["allowsXAppleSystemPreferencesURLScheme"] as? Bool == true else { continue }
            let name = renames[id] ?? (b.localizedInfoDictionary?["CFBundleDisplayName"] as? String)
                ?? (info["CFBundleDisplayName"] as? String) ?? (info["CFBundleName"] as? String) ?? ""
            // untranslated names like "SecurityImprovementsExtension" are internal panes
            guard !name.isEmpty, !(name.hasSuffix("Extension") && !name.contains(" ")) else { continue }
            let icon = (info["CFBundleIcons"] as? [String: Any])?["ISGraphicIconConfiguration"] as? [String: Any]
            let symbol = icon?["ISSymbolName"] as? String, color = icon?["ISEnclosureColor"] as? String
            let url = "x-apple.systempreferences:\(id)"
            var paneWords: [String] = []
            if let file = attrs["searchTermsFileName"] as? String,
               let su = b.url(forResource: file, withExtension: "searchTerms"), let d = try? Data(contentsOf: su),
               let terms = try? PropertyListSerialization.propertyList(from: d, format: nil) as? [String: Any] {
                var titles = Set<String>()
                for (anchor, v) in terms {
                    for s in (v as? [String: Any])?["localizableStrings"] as? [[String: Any]] ?? [] {
                        guard let title = s["title"] as? String, !title.isEmpty else { continue }
                        let words = (s["index"] as? String ?? "").split(separator: ",").map(String.init)
                        paneWords += words
                        // the same title can appear under several anchors; keep the first
                        guard titles.insert(title).inserted, title != name else { continue }
                        let a = anchor.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? anchor
                        out.append(Item(id: "setting:\(id)/\(anchor)/\(title)", kind: .setting, title: title,
                                        subtitle: "System Settings › \(name)", url: "\(url)?\(a)",
                                        symbol: symbol, color: color, keywords: words))
                    }
                }
            }
            out.append(Item(id: "setting:\(id)", kind: .pane, title: name, subtitle: "System Settings", url: url,
                            symbol: symbol, color: color, keywords: paneWords))
        }
        return out
    }

    nonisolated private static func color(_ name: String?) -> Color {
        switch name?.lowercased() {
        case "blue": return .blue
        case "cyan": return .cyan
        case "green": return .green
        case "yellow": return .yellow
        case "orange": return .orange
        case "red": return .red
        case "pink": return .pink
        case "purple", "indigo": return .indigo
        case "black": return Color(white: 0.15)
        case "white": return Color(white: 0.85)
        default: return .gray
        }
    }

    /// Toggles act right away; their url is the pane that holds the same setting, for "Open in System Settings".
    nonisolated private static let toggles: [Item] = [
        Item(id: "toggle:dark", kind: .toggle, title: "Dark Mode", subtitle: "",
             url: "x-apple.systempreferences:com.apple.Appearance-Settings.extension", symbol: "circle.lefthalf.filled",
             color: "black", keywords: ["appearance", "light mode", "theme", "night"]),
        Item(id: "toggle:wifi", kind: .toggle, title: "Wi‑Fi", subtitle: "",
             url: "x-apple.systempreferences:com.apple.wifi-settings-extension", symbol: "wifi", color: "blue",
             keywords: ["wireless", "internet", "network", "airport"]),
        Item(id: "toggle:mute", kind: .toggle, title: "Mute", subtitle: "",
             url: "x-apple.systempreferences:com.apple.Sound-Settings.extension", symbol: "speaker.slash.fill", color: "red",
             keywords: ["sound", "volume", "audio", "silence", "unmute"]),
        Item(id: "toggle:lock", kind: .toggle, title: "Lock Screen", subtitle: "Lock this Mac now",
             url: "x-apple.systempreferences:com.apple.Lock-Screen-Settings.extension", symbol: "lock.fill", color: "black",
             keywords: ["log out", "away"]),
        Item(id: "toggle:sleep", kind: .toggle, title: "Sleep", subtitle: "Put this Mac to sleep now",
             url: "x-apple.systempreferences:com.apple.Battery-Settings.extension", symbol: "moon.fill", color: "indigo",
             keywords: ["suspend"]),
    ]

    /// Toggles say what pressing Return will do, from the current state.
    private static func toggleText(_ id: String) -> (String, String)? {
        switch id {
        case "toggle:dark":
            let dark = UserDefaults.standard.string(forKey: "AppleInterfaceStyle") == "Dark"
            return (dark ? "Turn Dark Mode Off" : "Turn Dark Mode On", "Appearance is \(dark ? "dark" : "light")")
        case "toggle:wifi":
            guard let wifi = CWWiFiClient.shared().interface() else { return nil }
            let on = wifi.powerOn()
            return (on ? "Turn Wi‑Fi Off" : "Turn Wi‑Fi On", "Wi‑Fi is \(on ? "on" : "off")")
        case "toggle:mute":
            guard let m = muted() else { return ("Mute", "The output device can't be muted") }
            return (m ? "Unmute Sound" : "Mute Sound", "Sound is \(m ? "muted" : "on")")
        default:
            return nil
        }
    }

    private static func runToggle(_ id: String) {
        switch id {
        case "toggle:dark":  // asks once for permission to control System Events
            run("/usr/bin/osascript", ["-e", "tell application \"System Events\" to tell appearance preferences to set dark mode to not dark mode"])
        case "toggle:wifi":
            if let wifi = CWWiFiClient.shared().interface() { try? wifi.setPower(!wifi.powerOn()) }
        case "toggle:mute":
            if let m = muted() { setMuted(!m) }
        case "toggle:lock":
            lockScreen()
        case "toggle:sleep":
            run("/usr/bin/pmset", ["sleepnow"])
        default:
            break
        }
    }

    private static func run(_ tool: String, _ args: [String]) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        try? p.run()
    }

    /// The login framework's lock call (what the menu bar's Lock Screen uses); turning the display off is the fallback.
    private static func lockScreen() {
        if let h = dlopen("/System/Library/PrivateFrameworks/login.framework/Versions/Current/login", RTLD_LAZY),
           let sym = dlsym(h, "SACLockScreenImmediate") {
            typealias Lock = @convention(c) () -> Int32
            _ = unsafeBitCast(sym, to: Lock.self)()
        } else {
            run("/usr/bin/pmset", ["displaysleepnow"])
        }
    }

    private static func outputDevice() -> AudioObjectID? {
        var dev = AudioObjectID(0)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &dev) == noErr else { return nil }
        return dev
    }

    private static var muteAddress = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute,
                                                                mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)

    private static func muted() -> Bool? {
        guard let dev = outputDevice(), AudioObjectHasProperty(dev, &muteAddress) else { return nil }
        var v = UInt32(0)
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(dev, &muteAddress, 0, nil, &size, &v) == noErr else { return nil }
        return v != 0
    }

    private static func setMuted(_ on: Bool) {
        guard let dev = outputDevice() else { return }
        var v = UInt32(on ? 1 : 0)
        AudioObjectSetPropertyData(dev, &muteAddress, 0, nil, UInt32(MemoryLayout<UInt32>.size), &v)
    }
}
