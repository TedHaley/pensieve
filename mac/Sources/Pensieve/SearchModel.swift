import AppKit
import SwiftUI

enum ResultRowItem: Identifiable {
    case header(kind: String, id: String)
    case hit(index: Int, hit: Hit, id: String)

    var id: String {
        switch self {
        case .header(_, let id), .hit(_, _, let id): return id
        }
    }
}

struct ResultSection: Identifiable {
    let kind: String
    let items: [(index: Int, hit: Hit)]
    var id: String { kind }
}

/// State behind the search panel: the query, debounced backend searches, the selection, and the side action list.
@MainActor
final class SearchModel: ObservableObject {
    @Published var query = "" {
        didSet { if query != oldValue { schedule() } }
    }
    @Published private(set) var results: [Hit] = []
    @Published private(set) var sections: [ResultSection] = []
    @Published private(set) var rows: [ResultRowItem] = []
    private var generation = 0
    @Published private(set) var parsed: ParsedQuery?
    @Published private(set) var searched = false  // results belong to the current query
    @Published private(set) var loading = false
    @Published private(set) var error: String?
    @Published var selected: Int? {
        didSet { if selected != oldValue { actionIndex = 0 } }
    }
    @Published private(set) var actionsOpen = false
    /// The search options under an empty field, folded away behind a chevron (remembered).
    @Published var optionsOpen = Prefs.store.bool(forKey: "searchOptionsOpen") {
        didSet { Prefs.store.set(optionsOpen, forKey: "searchOptionsOpen"); onLayout?() }
    }
    @Published var actionIndex = 0

    /// Drives the panel's pop-in when it opens.
    @Published var presented = false

    var onLayout: (() -> Void)?
    var actions: Actions?
    private var task: Task<Void, Never>?
    private var local = Launcher.Results()  // apps and settings for the current query
    private var remote: [Hit] = []  // the backend's results, possibly for an earlier query until the next ones land

    // Fixed metrics, so the panel can be sized without measuring SwiftUI.
    static let width: CGFloat = 680
    static let cornerRadius: CGFloat = 26  // the glass shape, and the window clip that keeps its shadow rounded
    static let barHeight: CGFloat = 60
    static let rowHeight: CGFloat = 56
    static let headerHeight: CGFloat = 26
    static let emptyHeight: CGFloat = 44
    static let maxRows = 8
    static let hintFontSize: CGFloat = 11.5
    static let hintInsets: (leading: CGFloat, trailing: CGFloat) = (53, 20)
    static let actionRowHeight: CGFloat = 30
    static let actionWidth: CGFloat = 218

    var trimmed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }
    var hint: ParsedQuery { (searched ? parsed : nil) ?? QueryParse.parse(trimmed) }
    var selectedHit: Hit? { selected.flatMap { $0 < results.count ? results[$0] : nil } }
    var actionList: [ResultAction] { selectedHit.map(ResultAction.list(for:)) ?? [] }

    // MARK: hint line

    /// The status or guidance under the field, as plain text (nil = show the parsed query instead).
    var statusText: String? {
        switch Backend.shared.state {
        case .checking, .starting: return "Starting Pensieve…"
        case .installing(let msg): return msg
        case .missing: return "Pensieve isn't installed. In Terminal: curl -fsSL https://tedhaley.ca/pensieve/install.sh | sh"
        case .failed(let msg): return msg
        case .up:
            if let error { return error }
            if showsUpdate, let u = Updater.shared.panelText {  // measured with its buttons
                let buttons = "Update ⌘U    Later    Skip this version"
                return !updateButtons ? u : Self.updateButtonsBelow(u) ? u + "\n" + buttons : u + "     " + buttons
            }
            if trimmed.isEmpty { return Self.guidance }
            return nil
        }
    }

    static let guidance = "Open apps and settings, or search files, code, folders, people and agent sessions"
    static let options: [(String, String)] = [
        ("plain words", "by meaning"),
        ("\"quotes\"", "exact words"),
        ("-word", "leave out"),
        ("kind:file  code  doc  session", "only that kind (file = documents and code)"),
        ("kind:folder  repo  person", "folders and repos by name, people who know it"),
        ("by:name", "what someone wrote"),
        ("in:folder  ext:pdf", "inside a path · a file type"),
        ("app or setting name", "open it (Tab fills in the Top Hit)"),
        ("⌃  or  →", "actions on the selected result"),
    ]
    static let optionRowHeight: CGFloat = 18

    /// The plain guidance line (an empty field with nothing else to report), which has the options chevron.
    var showsGuidance: Bool { statusText == Self.guidance }

    /// The parsed query as one line of text: `Exact "x"  Meaning y  Without z  kind:code`.
    var parsedText: AttributedString {
        let p = hint
        var out = AttributedString()
        func add(_ name: String, _ value: String) {
            if !out.characters.isEmpty { out += AttributedString("   ") }
            var n = AttributedString(name + " ")
            n.font = .system(size: Self.hintFontSize, weight: .semibold)
            var v = AttributedString(value)
            v.foregroundColor = Color.primary.opacity(0.75)
            out += n + v
        }
        if let e = p.exact, !e.isEmpty { add("Exact", e.map { "\"\($0)\"" }.joined(separator: " ")) }
        if let s = p.semantic, !s.isEmpty { add("Meaning", s) }
        if let ex = p.exclude, !ex.isEmpty { add("Without", ex.joined(separator: ", ")) }
        for (k, v) in p.filterPairs { add("\(k):", v) }
        return out
    }

    /// The hint wraps instead of clipping, so its height depends on the text.
    var hintHeight: CGFloat {
        let text = statusText ?? String(parsedText.characters)
        let width = Self.width - Self.hintInsets.leading - Self.hintInsets.trailing - (statusShowsSpinner ? 18 : 0)
        let font = NSFont.systemFont(ofSize: Self.hintFontSize, weight: .semibold)  // bold is the wider case
        let h = (text as NSString).boundingRect(with: NSSize(width: width, height: 200),
                                                options: [.usesLineFragmentOrigin], attributes: [.font: font]).height
        let options = showsGuidance && optionsOpen ? CGFloat(Self.options.count) * Self.optionRowHeight + 6 : 0
        return max(26, ceil(h) + 12) + options
    }

    /// An available update (or its download) takes the empty panel's hint line.
    var showsUpdate: Bool {
        Backend.shared.state == .up && error == nil && trimmed.isEmpty && Updater.shared.panelText != nil
    }

    /// Long update messages (errors) put the buttons on their own line instead of squeezing the text.
    static func updateButtonsBelow(_ text: String) -> Bool { text.count > 60 }

    var updateButtons: Bool { Updater.shared.available != nil && !Updater.shared.busy }

    var statusShowsSpinner: Bool {
        if showsUpdate && Updater.shared.busy { return true }
        switch Backend.shared.state {
        case .checking, .starting, .installing: return true
        default: return false
        }
    }

    // MARK: sizing

    var listHeight: CGFloat {
        if results.isEmpty { return searched && !trimmed.isEmpty && error == nil ? Self.emptyHeight : 0 }
        let full = CGFloat(results.count) * Self.rowHeight + CGFloat(sections.count) * Self.headerHeight + 10
        let cap = CGFloat(Self.maxRows) * Self.rowHeight + 2 * Self.headerHeight + 10
        return min(full, cap)
    }

    var actionMenuHeight: CGFloat { CGFloat(actionList.count) * Self.actionRowHeight + 12 }

    var panelHeight: CGFloat {
        let list = listHeight
        var h = Self.barHeight + hintHeight + (list > 0 ? list + 1 : 0)
        if actionsOpen { h = max(h, Self.barHeight + hintHeight + actionMenuHeight + 16) }
        return h
    }

    // MARK: search

    private func schedule() {
        task?.cancel()
        closeActions()
        let q = trimmed
        searched = false
        if q.isEmpty {
            local = Launcher.Results()
            setResults([], parsed: nil)
            loading = false
            error = nil
            onLayout?()
            return
        }
        // Apps and settings match on every keystroke, before the backend answers; the previous backend results
        // stay under them until the new ones arrive.
        matchLocal()
        onLayout?()  // the hint line may change height as the query is typed
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

    /// Queries with operators (quotes, -word, kind:) are for the backend only.
    func matchLocal() {
        let q = trimmed
        let p = QueryParse.parse(q)
        let plain = (p.exact ?? []).isEmpty && (p.exclude ?? []).isEmpty && (p.filters ?? [:]).isEmpty
        local = plain && !q.isEmpty ? Launcher.shared.search(q) : Launcher.Results()
        setResults(remote, parsed: parsed)
    }

    func retry() {
        let q = query
        query = ""
        query = q
    }

    /// Group by kind like Spotlight: a strong app or setting match first as the Top Hit, then apps and settings,
    /// then the backend's groups in the order of their best result (backend order is kept within a group).
    /// `results` is reordered to match the display so indices line up with the selection.
    private func setResults(_ hits: [Hit], parsed: ParsedQuery?) {
        remote = hits
        var groups: [(String, [Hit])] = []
        var mine = local.hits
        if local.top, let first = mine.first {
            groups.append(("top", [first]))
            mine.removeFirst()
        }
        for k in ["app", "setting"] where mine.contains(where: { $0.kind == k }) { groups.append((k, mine.filter { $0.kind == k })) }
        var kinds: [String] = []
        for h in hits where !kinds.contains(h.kind) { kinds.append(h.kind) }
        for k in kinds { groups.append((k, hits.filter { $0.kind == k })) }
        var ordered: [Hit] = []
        var secs: [ResultSection] = []
        for (k, group) in groups {
            secs.append(ResultSection(kind: k, items: group.enumerated().map { (ordered.count + $0.offset, $0.element) }))
            ordered += group
        }
        // keep a selection the user moved to when backend results land under the apps and settings
        let keep = selected.flatMap { i in i > 0 && i < results.count && i < ordered.count && results[i].id == ordered[i].id ? i : nil }
        generation += 1
        var flat: [ResultRowItem] = []
        for sec in secs {
            flat.append(.header(kind: sec.kind, id: "\(generation)/h/\(sec.kind)"))
            for it in sec.items { flat.append(.hit(index: it.index, hit: it.hit, id: "\(generation)/\(it.index)")) }
        }
        results = ordered
        sections = secs
        rows = flat
        self.parsed = parsed
        selected = ordered.isEmpty ? nil : keep ?? 0
    }

    /// The rest of the Top Hit's name, shown greyed after the typed text like Spotlight (Tab fills it in).
    var completion: (rest: String, kind: String)? {
        guard local.top, selected == 0, let top = local.hits.first, !query.hasSuffix(" "), !query.isEmpty,
              top.title.lowercased().hasPrefix(query.lowercased()) else { return nil }
        return (String(top.title.dropFirst(query.count)), top.kind == "app" ? "Application" : "System Settings")
    }

    func complete() -> Bool {
        guard let top = local.hits.first, let c = completion, !c.rest.isEmpty else { return false }
        query = top.title
        return true
    }

    func rowID(for index: Int) -> String? { "\(generation)/\(index)" }

    func move(_ delta: Int) {
        guard !results.isEmpty else { return }
        let cur = selected ?? (delta > 0 ? -1 : results.count)
        withAnimation(.snappy(duration: 0.16)) { selected = max(0, min(results.count - 1, cur + delta)) }
    }

    func open(_ index: Int? = nil) {
        guard let i = index ?? selected, i < results.count else { return }
        closeActions()
        actions?.open(results[i])
    }

    // MARK: side action list

    func openActions() {
        guard selectedHit != nil, !actionsOpen else { return }
        actionIndex = 0
        withAnimation(.snappy(duration: 0.2)) { actionsOpen = true }
        onLayout?()
    }

    func closeActions() {
        guard actionsOpen else { return }
        withAnimation(.snappy(duration: 0.16)) { actionsOpen = false }
        onLayout?()
    }

    func toggleActions() {
        actionsOpen ? closeActions() : openActions()
    }

    func moveAction(_ delta: Int) {
        let n = actionList.count
        guard n > 0 else { return }
        actionIndex = max(0, min(n - 1, actionIndex + delta))
    }

    func runAction(_ index: Int? = nil) {
        let list = actionList
        guard let hit = selectedHit, let a = list[safe: index ?? actionIndex] else { return }
        closeActions()
        actions?.perform(a, hit)
    }
}

extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}
