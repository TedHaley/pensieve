// swift-tools-version:6.2
// The menu bar item of Pensieve. It shows what the daemon does and changes its settings.
// The daemon (the Rust binary) does all indexing and search. This app only calls its /status and /settings routes.
import PackageDescription

let package = Package(
    name: "Pensieve",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(name: "Pensieve", path: "Sources/Pensieve", swiftSettings: [.swiftLanguageMode(.v5)]),
    ]
)
