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
    let kind: String  // file | code | session
    let title: String
    let subtitle: String?
    let path: String?
    let line: Int?
    let snippet: String?
    let highlights: [[Int]]?
    let score: Double?
    let match: String?  // exact | semantic | both
    let ext: String?

    var isExact: Bool { match == "exact" || match == "both" }
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
        default: return kind.capitalized
        }
    }
}

/// Instant, local reading of the query so the hint line updates while typing; the backend's parse wins once
/// results arrive. Mirrors Google: "quotes" = exact, -word = exclude, key:value = filter, the rest = meaning.
enum QueryParse {
    private static let quoted = try! NSRegularExpression(pattern: "(-?)\"([^\"]*)\"?")
    private static let filterKeys: Set<String> = ["kind", "ext", "type", "in", "repo"]

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
