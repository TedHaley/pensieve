// swift-tools-version:5.10
// The native macOS shell for Pensieve: a menu-bar item and a window hosting the web app. The Python backend does
// all indexing and search.
import PackageDescription

let package = Package(
    name: "Pensieve",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Pensieve", path: "Sources/Pensieve"),
        // decodes video frames and sound for the backend (Contents/MacOS/pensieve-media)
        .executableTarget(name: "pensieve-media", path: "Sources/PensieveMedia"),
    ]
)
