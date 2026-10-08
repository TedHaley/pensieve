import AppKit
import SwiftUI

struct SearchView: View {
    @ObservedObject var model: SearchModel
    @ObservedObject var backend = Backend.shared

    var body: some View {
        VStack(spacing: 0) {
            bar
            HintLine(model: model, backend: backend)
                .frame(height: SearchModel.hintHeight, alignment: .top)
            if model.listHeight > 0 {
                Divider().opacity(0.6).padding(.horizontal, 14)
                ResultsList(model: model)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .panelGlass()
    }

    private var bar: some View {
        HStack(spacing: 12) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 21, weight: .medium))
                .foregroundStyle(.secondary)
            TextField("Pensieve Search", text: $model.query)
                .textFieldStyle(.plain)
                .font(.system(size: 24, weight: .regular))
            if model.loading {
                ProgressView().controlSize(.small)
            }
            Button {
                model.actions?.visualize(nil)
            } label: {
                Image(systemName: "circle.hexagongrid")
                    .font(.system(size: 17, weight: .medium))
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Open visualizer ⌘O")
        }
        .padding(.leading, 20)
        .padding(.trailing, 14)
        .frame(height: SearchModel.barHeight)
    }
}

/// The small line under the field: how the query is being read, or the backend's state.
struct HintLine: View {
    @ObservedObject var model: SearchModel
    @ObservedObject var backend: Backend

    var body: some View {
        HStack(spacing: 10) {
            content
            Spacer(minLength: 0)
        }
        .font(.system(size: 11.5))
        .foregroundStyle(.secondary)
        .lineLimit(1)
        .padding(.leading, 53)
        .padding(.trailing, 20)
    }

    @ViewBuilder private var content: some View {
        switch backend.state {
        case .checking, .starting:
            HStack(spacing: 6) {
                ProgressView().controlSize(.mini)
                Text("Starting Pensieve…")
            }
        case .installing(let msg):
            HStack(spacing: 6) {
                ProgressView().controlSize(.mini)
                Text(msg)
            }
        case .missing:
            Text("Pensieve isn't installed. In Terminal: curl -fsSL https://tedhaley.ca/pensieve/install.sh | sh")
                .textSelection(.enabled)
        case .failed(let msg):
            Text(msg)
        case .up:
            if let err = model.error {
                Text(err)
            } else if model.trimmed.isEmpty {
                Text("Search by meaning  ·  \"quotes\" for exact words  ·  kind:code  ext:pdf  ·  -word to exclude")
            } else {
                parsedHint(model.hint)
            }
        }
    }

    @ViewBuilder private func parsedHint(_ p: ParsedQuery) -> some View {
        if let exact = p.exact, !exact.isEmpty {
            label("Exact", exact.map { "\"\($0)\"" }.joined(separator: " "))
        }
        if let s = p.semantic, !s.isEmpty {
            label("Meaning", s)
        }
        if let ex = p.exclude, !ex.isEmpty {
            label("Without", ex.joined(separator: ", "))
        }
        ForEach(p.filterPairs, id: \.0) { k, v in
            Text("\(k):\(v)").fontWeight(.semibold)
        }
    }

    private func label(_ name: String, _ value: String) -> some View {
        HStack(spacing: 4) {
            Text(name).fontWeight(.semibold)
            Text(value).foregroundStyle(.primary.opacity(0.75))
        }
    }
}

struct ResultsList: View {
    @ObservedObject var model: SearchModel

    var body: some View {
        if model.results.isEmpty {
            Text("No results")
                .font(.system(size: 13))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, 53)
                .frame(height: SearchModel.emptyHeight)
        } else {
            ScrollViewReader { proxy in
                ScrollView(.vertical) {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(model.sections) { sec in
                            Text(Kind.title(sec.kind))
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(.secondary)
                                .padding(.leading, 12)
                                .frame(height: SearchModel.headerHeight, alignment: .bottomLeading)
                            ForEach(sec.items, id: \.index) { item in
                                ResultRow(hit: item.hit, selected: model.selected == item.index)
                                    .id(item.index)
                                    .onTapGesture(count: 2) { model.open(item.index) }
                                    .simultaneousGesture(TapGesture().onEnded { model.selected = item.index })
                            }
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.bottom, 10)
                }
                .scrollIndicators(.automatic)
                .frame(maxHeight: .infinity)
                .onChange(of: model.selected) {
                    if let s = model.selected { proxy.scrollTo(s) }
                }
            }
        }
    }
}

struct ResultRow: View {
    let hit: Hit
    let selected: Bool

    var body: some View {
        HStack(spacing: 12) {
            HitIcon(hit: hit, selected: selected)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(hit.title)
                        .font(.system(size: 13.5, weight: .semibold))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if hit.isExact {
                        Text("exact")
                            .font(.system(size: 9.5, weight: .bold))
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(selected ? Color.white.opacity(0.25) : Color.accentColor.opacity(0.16)))
                            .foregroundStyle(selected ? Color.white : Color.accentColor)
                    }
                    Spacer(minLength: 0)
                }
                .foregroundStyle(selected ? Color.white : Color.primary)
                if let sub = hit.subtitle, !sub.isEmpty {
                    Text(sub)
                        .font(.system(size: 11))
                        .foregroundStyle(selected ? Color.white.opacity(0.78) : Color.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                if let snip = hit.snippet, !snip.isEmpty {
                    Text(Self.snippet(snip, hit.highlights ?? [], selected: selected))
                        .font(.system(size: 11.5))
                        .lineLimit(1)
                }
            }
        }
        .padding(.horizontal, 10)
        .frame(height: SearchModel.rowHeight)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(selected ? Color.accentColor : Color.clear)
        )
        .contentShape(Rectangle())
    }

    /// Highlight ranges are code-point offsets (Python string indices) into the snippet. Newlines and tabs become
    /// spaces one-for-one, so the offsets stay valid.
    static func snippet(_ s: String, _ ranges: [[Int]], selected: Bool) -> AttributedString {
        let flat = String(String.UnicodeScalarView(s.unicodeScalars.map {
            $0 == "\n" || $0 == "\r" || $0 == "\t" ? " " : $0
        }))
        var a = AttributedString(flat)
        a.foregroundColor = selected ? Color.white.opacity(0.85) : Color.secondary
        let scalars = a.unicodeScalars
        let n = scalars.count
        for r in ranges where r.count == 2 {
            let lo = max(0, min(n, r[0])), hi = max(lo, min(n, r[1]))
            guard hi > lo else { continue }
            let start = scalars.index(scalars.startIndex, offsetBy: lo)
            let end = scalars.index(scalars.startIndex, offsetBy: hi)
            a[start..<end].foregroundColor = selected ? Color.white : Color.primary
            a[start..<end].font = .system(size: 11.5, weight: .bold)
        }
        // Start the line near the first highlight so it is visible in a one-line snippet.
        if let first = ranges.first(where: { $0.count == 2 })?.first, first > 70, first < n {
            let cut = a.unicodeScalars.index(a.unicodeScalars.startIndex, offsetBy: first - 40)
            var tail = AttributedString("…")
            tail.foregroundColor = a.foregroundColor
            return tail + AttributedString(a[cut...])
        }
        return a
    }
}

struct HitIcon: View {
    let hit: Hit
    let selected: Bool

    var body: some View {
        Group {
            if hit.kind == "file", let p = hit.path, FileManager.default.fileExists(atPath: p) {
                Image(nsImage: IconCache.icon(for: p))
                    .resizable()
                    .interpolation(.high)
                    .frame(width: 32, height: 32)
            } else {
                Image(systemName: symbol)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(selected ? Color.white : tint)
                    .frame(width: 30, height: 30)
                    .background(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(selected ? Color.white.opacity(0.22) : tint.opacity(0.15))
                    )
            }
        }
        .frame(width: 32, height: 32)
    }

    private var symbol: String {
        switch hit.kind {
        case "code": return "chevron.left.forwardslash.chevron.right"
        case "session": return "bubble.left.and.text.bubble.right"
        default: return "doc.text"
        }
    }

    private var tint: Color {
        switch hit.kind {
        case "code": return .teal
        case "session": return .purple
        default: return .blue
        }
    }
}

@MainActor
enum IconCache {
    private static var cache: [String: NSImage] = [:]

    static func icon(for path: String) -> NSImage {
        if let i = cache[path] { return i }
        let i = NSWorkspace.shared.icon(forFile: path)
        if cache.count > 500 { cache.removeAll() }
        cache[path] = i
        return i
    }
}

private struct VisualEffect: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = .popover
        v.blendingMode = .behindWindow
        v.state = .active
        return v
    }

    func updateNSView(_ v: NSVisualEffectView, context: Context) {}
}

extension View {
    /// Spotlight's look: Liquid Glass on macOS 26, a popover-style blur before that.
    @ViewBuilder func panelGlass() -> some View {
        let shape = RoundedRectangle(cornerRadius: 26, style: .continuous)
        if #available(macOS 26.0, *) {
            self.glassEffect(.regular, in: shape)
        } else {
            self.background(VisualEffect().clipShape(shape))
                .overlay(shape.strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5))
        }
    }
}
