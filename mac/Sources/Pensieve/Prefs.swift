import Foundation

/// The app's own preferences. Debug instances (PENSIEVE_DEBUG) use a separate store so tests never change the
/// installed app's settings.
enum Prefs {
    nonisolated(unsafe) static let store: UserDefaults =
        ProcessInfo.processInfo.environment["PENSIEVE_DEBUG"] != nil
            ? UserDefaults(suiteName: "ca.tedhaley.pensieve.debug") ?? .standard
            : .standard
}
