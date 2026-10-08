import AppKit
import SwiftUI

struct ResultSection: Identifiable {
    let kind: String
    let items: [(index: Int, hit: Hit)]
    var id: String { kind }
}

/// State behind the search panel: the query, debounced backend searches, and the selection.
@MainActor
final class SearchModel: ObservableObject {
    @Published var query = "" {
        didSet { if query != oldValue { schedule() } }
    }
    @Published private(set) var results: [Hit] = []
    @Published private(set) var sections: [ResultSection] = []
    @Published private(set) var parsed: ParsedQuery?
    @Published private(set) var searched = false  // results belong to the current query
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    @Published var selected: Int?

    var onLayout: (() -> Void)?
    var actions: Actions?
    private var task: Task<Void, Never>?

    // Fixed metrics, so the panel can be sized without measuring SwiftUI.
    static let width: CGFloat = 680
    static let barHeight: CGFloat = 60
    static let hintHeight: CGFloat = 26
    static let rowHeight: CGFloat = 56
    static let headerHeight: CGFloat = 26
    static let emptyHeight: CGFloat = 44
    static let maxRows = 8

    var trimmed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }
    var hint: ParsedQuery { (searched ? parsed : nil) ?? QueryParse.parse(trimmed) }
    var selectedHit: Hit? { selected.flatMap { $0 < results.count ? results[$0] : nil } }

    var listHeight: CGFloat {
        if results.isEmpty { return searched && !trimmed.isEmpty && error == nil ? Self.emptyHeight : 0 }
        let full = CGFloat(results.count) * Self.rowHeight + CGFloat(sections.count) * Self.headerHeight + 10
        let cap = CGFloat(Self.maxRows) * Self.rowHeight + 2 * Self.headerHeight + 10
        return min(full, cap)
    }

    var panelHeight: CGFloat {
        let list = listHeight
        return Self.barHeight + Self.hintHeight + (list > 0 ? list + 1 : 0)
    }

    func clear() {
        query = ""
    }

    private func schedule() {
        task?.cancel()
        let q = trimmed
        searched = false
        if q.isEmpty {
            setResults([], parsed: nil)
            loading = false
            error = nil
            onLayout?()
            return
        }
        task = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(120))
            guard !Task.isCancelled, let self else { return }
            self.loading = true
            defer { if !Task.isCancelled { self.loading = false } }
            do {
                let r = try await Backend.shared.find(q)
                guard !Task.isCancelled, self.trimmed == q else { return }
                self.error = nil
                self.setResults(r.results, parsed: r.query)
                self.searched = true
            } catch {
                guard !Task.isCancelled, self.trimmed == q else { return }
                self.setResults([], parsed: nil)
                self.error = Backend.shared.state == .up ? "Search failed: \(error.localizedDescription)" : nil
            }
            self.onLayout?()
        }
    }

    func retry() {
        let q = query
        query = ""
        query = q
    }

    /// Group by kind like Spotlight. Groups appear in the order of their best result; backend order is kept
    /// within a group. `results` is reordered to match the display so indices line up with the selection.
    private func setResults(_ hits: [Hit], parsed: ParsedQuery?) {
        var kinds: [String] = []
        for h in hits where !kinds.contains(h.kind) { kinds.append(h.kind) }
        var ordered: [Hit] = []
        var secs: [ResultSection] = []
        for k in kinds {
            let group = hits.filter { $0.kind == k }
            secs.append(ResultSection(kind: k, items: group.enumerated().map { (ordered.count + $0.offset, $0.element) }))
            ordered += group
        }
        results = ordered
        sections = secs
        self.parsed = parsed
        selected = ordered.isEmpty ? nil : 0
    }

    func move(_ delta: Int) {
        guard !results.isEmpty else { return }
        let cur = selected ?? (delta > 0 ? -1 : results.count)
        selected = max(0, min(results.count - 1, cur + delta))
    }

    func open(_ index: Int? = nil) {
        guard let i = index ?? selected, i < results.count else { return }
        actions?.open(results[i])
    }
}
