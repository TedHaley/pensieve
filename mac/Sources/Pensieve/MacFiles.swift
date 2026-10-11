import CoreServices
import Foundation

/// Documents and folders anywhere on the Mac, found by name in Spotlight's own index (nothing is indexed here, and
/// none of it reaches the backend or the map). Code is left out: files in a git repo and source files belong to the
/// backend's code index, which reads their contents, not only their names.
@MainActor
final class MacFiles {
    static let shared = MacFiles()
    nonisolated static let limit = 4

    private var generation = 0

    /// Calls `done` with the best name matches for `q`, unless a newer query started first.
    func search(_ q: String, done: @escaping ([Hit]) -> Void) {
        generation += 1
        let gen = generation
        let words = Launcher.Match.words(q)
        guard !words.isEmpty, words.joined().count >= 2 else { return done([]) }
        Task.detached(priority: .userInitiated) {
            let hits = Self.find(words)
            await MainActor.run {
                guard MacFiles.shared.generation == gen else { return }
                done(hits)
            }
        }
    }

    nonisolated private static func find(_ words: [String]) -> [Hit] {
        // "w*"cdw: any word of the name starts with w, ignoring case and accents (Spotlight's own name matching)
        let text = words.map { "kMDItemFSName == \"\($0)*\"cdw" }.joined(separator: " && ")
        guard let query = MDQueryCreate(kCFAllocatorDefault, text as CFString, nil, nil) else { return [] }
        MDQuerySetMaxCount(query, 400)
        MDQuerySetSearchScope(query, [kMDQueryScopeComputerIndexed] as CFArray, 0)
        guard MDQueryExecute(query, CFOptionFlags(kMDQuerySynchronous.rawValue)) else { return [] }
        let q = Launcher.Match(words.joined(separator: " "))
        var repoCache: [String: Bool] = [:]
        var scored: [(Hit, Double)] = []
        for i in 0..<MDQueryGetResultCount(query) {
            guard let raw = MDQueryGetResultAtIndex(query, i) else { continue }
            let item = Unmanaged<MDItem>.fromOpaque(raw).takeUnretainedValue()
            guard let path = MDItemCopyAttribute(item, kMDItemPath) as? String, wanted(path) else { continue }
            let types = MDItemCopyAttribute(item, kMDItemContentTypeTree) as? [String] ?? []
            if types.contains(where: skippedTypes.contains) { continue }
            let url = URL(fileURLWithPath: path)
            if inRepo(url.deletingLastPathComponent().path, &repoCache) { continue }
            let name = url.lastPathComponent
            guard let s = score(name, q) else { continue }
            let used = (MDItemCopyAttribute(item, kMDItemLastUsedDate) as? Date)
                ?? (MDItemCopyAttribute(item, kMDItemContentModificationDate) as? Date)
            let days = used.map { max(0, -$0.timeIntervalSinceNow / 86400) } ?? 3650
            let folder = types.contains("public.folder")
            let home = NSHomeDirectory()
            let dir = url.deletingLastPathComponent().path
            let shown = dir.hasPrefix(home) ? "~" + dir.dropFirst(home.count) : dir
            let hit = Hit(id: "mac:" + path, kind: "document", title: name, subtitle: shown, path: path, line: nil,
                          snippet: nil, highlights: nil, score: nil, match: nil, ext: url.pathExtension, isDir: folder)
            scored.append((hit, Double(s) - 40 * log2(1 + days)))  // recently used comes first among equal names
        }
        scored.sort { $0.1 > $1.1 }
        return scored.prefix(limit).map(\.0)
    }

    /// Name quality: whole name (without extension), name prefix, then every word starts a word of the name.
    nonisolated private static func score(_ name: String, _ q: Launcher.Match) -> Int? {
        let n = Launcher.Match(name), base = Launcher.Match((name as NSString).deletingPathExtension)
        if base.compact == q.compact || n.compact == q.compact { return 1000 }
        if n.compact.hasPrefix(q.compact) { return 900 - min(60, n.compact.count - q.compact.count) }
        if q.words.allSatisfy({ w in n.words.contains { $0.hasPrefix(w) } }) { return 700 }
        return nil
    }

    /// Code, apps (the Applications group lists those), and files that are rarely what anyone looks for by name.
    nonisolated private static let skippedTypes: Set<String> = [
        "public.source-code", "public.script", "public.shell-script", "com.apple.application-bundle",
        "com.apple.application-file", "public.unix-executable", "com.apple.systempreference.prefpane",
        "public.log",
    ]

    nonisolated private static let skippedDirs: Set<String> = [
        "node_modules", "site-packages", "__pycache__", "DerivedData", "Pods", "venv", "build", "dist", "target",
    ]

    /// Only the user's own places: no system folders, no Library, nothing hidden, nothing inside a bundle.
    nonisolated private static func wanted(_ path: String) -> Bool {
        let home = NSHomeDirectory()
        if path.hasPrefix(home + "/Library/") || path.hasPrefix(home + "/go/pkg/") || path.hasPrefix("/Applications/") || path.hasPrefix("/System/")
            || path.hasPrefix("/Library/") || path.hasPrefix("/private/") || path.hasPrefix("/usr/")
            || path.hasPrefix("/opt/") || path.hasPrefix("/cores/") { return false }
        let parts = path.split(separator: "/")
        for (i, p) in parts.enumerated() {
            if p.hasPrefix(".") || skippedDirs.contains(String(p)) { return false }
            if i < parts.count - 1, p.hasSuffix(".app") || p.hasSuffix(".bundle") || p.hasSuffix(".framework")
                || p.hasSuffix(".photoslibrary") || p.hasSuffix(".xcodeproj") { return false }
        }
        return true
    }

    /// True when `dir` or a folder above it has a .git: the file belongs to a repo, which the backend indexes.
    nonisolated private static func inRepo(_ dir: String, _ cache: inout [String: Bool]) -> Bool {
        var walked: [String] = []
        var d = dir
        var found = false
        while d.count > 1 {
            if let known = cache[d] { found = known; break }
            walked.append(d)
            if FileManager.default.fileExists(atPath: d + "/.git") { found = true; break }
            d = (d as NSString).deletingLastPathComponent
        }
        for w in walked { cache[w] = found }
        return found
    }
}
