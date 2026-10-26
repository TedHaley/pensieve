import Foundation

/// Spotlight's ⌘Space is system shortcut 64 in com.apple.symbolichotkeys. Versions of Pensieve with a search panel
/// turned it off while their own shortcut was ⌘Space. `restore` turns it back on, but only if Pensieve was the one
/// that turned it off.
@MainActor
enum SpotlightShortcut {
    private static let domain = "com.apple.symbolichotkeys" as CFString
    private static let key = "AppleSymbolicHotKeys" as CFString
    private static let spotlight = "64"
    private static let tookKey = "disabledSpotlightShortcut"

    static func restore() {
        guard Prefs.store.bool(forKey: tookKey) else { return }
        var all = CFPreferencesCopyAppValue(key, domain) as? [String: Any] ?? [:]
        var entry = all[spotlight] as? [String: Any]
            ?? ["value": ["parameters": [32, 49, 1_048_576], "type": "standard"]]  // ⌘Space
        entry["enabled"] = true
        all[spotlight] = entry
        CFPreferencesSetAppValue(key, all as CFDictionary, domain)
        CFPreferencesAppSynchronize(domain)
        // makes the change take effect now instead of at the next login
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/System/Library/PrivateFrameworks/SystemAdministration.framework/Resources/activateSettings")
        p.arguments = ["-u"]
        try? p.run()
        p.waitUntilExit()
        Prefs.store.set(false, forKey: tookKey)
    }
}
