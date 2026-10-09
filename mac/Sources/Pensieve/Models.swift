import Foundation

/// How the backend read the query: free text is matched by meaning, "quoted phrases" exactly.
struct ParsedQuery: Decodable, Equatable {
    var semantic: String?
    var exact: [String]?
    var exclude: [String]?
    var filters: [String: String?]?

    var filterPairs: [(String, String)] {
        (filters ?? [:]).compactMap { k, v in v.map { (k, $0) } }.sorted { $0.0 < $1.0 }
    }
}

struct Hit: Decodable, Identifiable, Equatable {
    let id: String
    let kind: String  // file | code | session | folder | repo | person
    let title: String
    let subtitle: String?
    let path: String?
    let line: Int?
    let snippet: String?
    let highlights: [[Int]]?
    let score: Double?
    let match: String?  // exact | semantic | both
    let ext: String?
    let isDir: Bool?

    enum CodingKeys: String, CodingKey {
        case id, kind, title, subtitle, path, line, snippet, highlights, score, match, ext
        case isDir = "is_dir"
    }

    var isExact: Bool { match == "exact" || match == "both" }
    var isFolder: Bool { isDir == true }
    var pathExists: Bool { path.map { FileManager.default.fileExists(atPath: $0) } ?? false }
}

/// What can be done with a result, shown in the side action list (⌃ or →).
enum ResultAction: String, Identifiable {
    case open, reveal, map, copy

    var id: String { rawValue }

    static func list(for hit: Hit) -> [ResultAction] {
        if hit.kind == "session" || hit.kind == "person" { return [.open] }  // both open on the map
        if hit.path == nil { return [.open, .map] }
        var out: [ResultAction] = [.open]
        if hit.pathExists { out.append(.reveal) }
        if !hit.isFolder || hit.id.hasPrefix("folder:") { out.append(.map) }  // a folder result filters the map
        out.append(.copy)
        return out
    }

    func title(for hit: Hit) -> String {
        switch self {
        case .open: return hit.kind == "session" || hit.kind == "person" ? "Open on map" : hit.isFolder ? "Open in Finder" : "Open"
        case .reveal: return "Show in Finder"
        case .map: return "Show on map"
        case .copy: return "Copy path"
        }
    }

    var symbol: String {
        switch self {
        case .open: return "arrow.up.forward.app"
        case .reveal: return "folder"
        case .map: return "circle.hexagongrid"
        case .copy: return "doc.on.doc"
        }
    }

    var shortcut: String {
        switch self {
        case .open: return "↩"
        case .reveal: return "⌘↩"
        case .map: return "⌥↩"
        case .copy: return "⌘C"
        }
    }
}

struct FindResponse: Decodable {
    let query: ParsedQuery?
    let results: [Hit]
}

enum Kind {
    static let order = ["file", "code", "session"]

    static func title(_ kind: String) -> String {
        switch kind {
        case "file": return "Files"
        case "code": return "Code"
        case "session": return "Agent sessions"
        case "folder": return "Folders"
        case "repo": return "Repositories"
        case "person": return "People"
        default: return kind.capitalized
        }
    }
}

/// Instant, local reading of the query so the hint line updates while typing; the backend's parse wins once
/// results arrive. Mirrors Google: "quotes" = exact, -word = exclude, key:value = filter, the rest = meaning.
enum QueryParse {
    private static let quoted = try! NSRegularExpression(pattern: "(-?)\"([^\"]*)\"?")
    private static let filterKeys: Set<String> = ["kind", "ext", "type", "in", "repo", "by"]

    static func parse(_ q: String) -> ParsedQuery {
        let ns = q as NSString
        var exact: [String] = [], exclude: [String] = [], filters: [String: String?] = [:]
        var rest = ""
        var last = 0
        for m in quoted.matches(in: q, range: NSRange(location: 0, length: ns.length)) {
            rest += ns.substring(with: NSRange(location: last, length: m.range.location - last)) + " "
            let phrase = ns.substring(with: m.range(at: 2)).trimmingCharacters(in: .whitespaces)
            if !phrase.isEmpty {
                if m.range(at: 1).length > 0 { exclude.append(phrase) } else { exact.append(phrase) }
            }
            last = m.range.location + m.range.length
        }
        rest += ns.substring(from: last)
        var words: [String] = []
        for w in rest.split(whereSeparator: \.isWhitespace).map(String.init) {
            if w.count > 1, w.hasPrefix("-") {
                exclude.append(String(w.dropFirst()))
            } else if let i = w.firstIndex(of: ":"), filterKeys.contains(w[..<i].lowercased()), w.index(after: i) < w.endIndex {
                filters[w[..<i].lowercased()] = String(w[w.index(after: i)...])
            } else {
                words.append(w)
            }
        }
        return ParsedQuery(semantic: words.joined(separator: " "), exact: exact, exclude: exclude, filters: filters)
    }
}
