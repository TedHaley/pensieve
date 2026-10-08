// swift-tools-version:5.10
// The native macOS shell for Pensieve: a Spotlight-style search panel, a global hotkey, a menu-bar item,
// and a window hosting the web visualizer. The Python backend does all indexing and search.
import PackageDescription

let package = Package(
    name: "Pensieve",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Pensieve", path: "Sources/Pensieve"),
    ]
)
