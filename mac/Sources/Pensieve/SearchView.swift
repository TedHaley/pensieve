import AppKit
import SwiftUI

struct SearchView: View {
    @ObservedObject var model: SearchModel
    @ObservedObject var backend = Backend.shared

    var body: some View {
        VStack(spacing: 0) {
            bar
            HintLine(model: model, backend: backend)
                .frame(height: model.hintHeight, alignment: .top)
            if model.listHeight > 0 {
                Divider().opacity(0.6).padding(.horizontal, 14)
                ResultsList(model: model)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .overlayPreferenceValue(SelectedRowKey.self) { anchor in
            GeometryReader { geo in
                if model.actionsOpen, let anchor, let hit = model.selectedHit {
                    let row = geo[anchor]
                    let h = model.actionMenuHeight
                    let minY = SearchModel.barHeight + 4
                    let y = min(max(row.midY - h / 2, minY), geo.size.height - h - 10)
                    ActionMenu(model: model, hit: hit)
                        .frame(width: SearchModel.actionWidth, height: h)
                        .offset(x: geo.size.width - SearchModel.actionWidth - 18, y: max(minY, y))
                        .transition(.asymmetric(
                            insertion: .opacity.combined(with: .offset(x: 16)).combined(with: .scale(scale: 0.97, anchor: .leading)),
                            removal: .opacity.combined(with: .offset(x: 10))))
                }
            }
        }
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
            Button {
                model.actions?.settings()
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 17, weight: .medium))
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Settings ⌘,")
        }
        .padding(.leading, 20)
        .padding(.trailing, 12)
        .frame(height: SearchModel.barHeight)
    }
}

/// The line under the field: how the query is being read, or the backend's state. Wraps rather than clipping;
/// SearchModel.hintHeight sizes the panel for it.
struct HintLine: View {
    @ObservedObject var model: SearchModel
    @ObservedObject var backend: Backend
    @ObservedObject var updater = Updater.shared

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            if model.statusShowsSpinner {
                ProgressView().controlSize(.mini).padding(.top, 1)
            }
            if model.showsUpdate, let text = updater.panelText {
                let below = SearchModel.updateButtonsBelow(text)
                let layout = below ? AnyLayout(VStackLayout(alignment: .leading, spacing: 4)) : AnyLayout(HStackLayout(alignment: .top, spacing: 6))
                layout {
                    Text(text).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    if model.updateButtons {
                        HStack(spacing: 12) {
                            Button("Update ⌘U") { updater.install() }.fontWeight(.semibold)
                            Button("Later") { updater.later() }
                            Button("Skip this version") { updater.skip() }
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(Color.accentColor)
                        .fixedSize()
                        .padding(.leading, below ? 0 : 6)
                    }
                }
            } else {
                Group {
                    if let status = model.statusText {
                        Text(status)
                    } else {
                        Text(model.parsedText)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            }
            Spacer(minLength: 0)
        }
        .font(.system(size: SearchModel.hintFontSize))
        .foregroundStyle(.secondary)
        .lineLimit(6)
        .padding(.top, 2)
        .padding(.leading, SearchModel.hintInsets.leading)
        .padding(.trailing, SearchModel.hintInsets.trailing)
    }
}

/// Where the selected row is, so the action list can pop out beside it.
struct SelectedRowKey: PreferenceKey {
    static let defaultValue: Anchor<CGRect>? = nil
    static func reduce(value: inout Anchor<CGRect>?, nextValue: () -> Anchor<CGRect>?) {
        value = value ?? nextValue()
    }
}

struct ActionMenu: View {
    @ObservedObject var model: SearchModel
    let hit: Hit

    var body: some View {
        let list = model.actionList
        VStack(spacing: 0) {
            ForEach(Array(list.enumerated()), id: \.element) { i, a in
                let on = i == model.actionIndex
                HStack(spacing: 9) {
                    Image(systemName: a.symbol)
                        .font(.system(size: 12, weight: .semibold))
                        .frame(width: 16)
                    Text(a.title(for: hit))
                        .font(.system(size: 13, weight: on ? .semibold : .regular))
                        .lineLimit(1)
                        .fixedSize()
                    Spacer(minLength: 8)
                    Text(a.shortcut)
                        .font(.system(size: 11.5))
                        .foregroundStyle(on ? Color.white.opacity(0.8) : Color.secondary)
                        .fixedSize()
                }
                .foregroundStyle(on ? Color.white : Color.primary)
                .padding(.horizontal, 10)
                .frame(height: SearchModel.actionRowHeight)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(on ? Color.accentColor : .clear))
                .contentShape(Rectangle())
                .onHover { if $0 { model.actionIndex = i } }
                .onTapGesture { model.runAction(i) }
            }
        }
        .padding(6)
        .menuGlass()
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
                        // One flat list with ids unique to each search's results, so rows never keep a stale header.
                        ForEach(model.rows) { row in
                            switch row {
                            case .header(let kind, _):
                                Text(Kind.title(kind))
                                    .font(.system(size: 11, weight: .semibold))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                                    .padding(.leading, 12)
                                    .frame(height: SearchModel.headerHeight, alignment: .bottomLeading)
                            case .hit(let index, let hit, _):
                                let on = model.selected == index
                                ResultRow(hit: hit, selected: on, actionsOpen: on && model.actionsOpen)
                                    .anchorPreference(key: SelectedRowKey.self, value: .bounds) { on ? $0 : nil }
                                    .onTapGesture(count: 2) { model.open(index) }
                                    .simultaneousGesture(TapGesture().onEnded {
                                        if on && model.actionsOpen { return model.closeActions() }
                                        model.selected = index
                                        model.openActions()
                                    })
                            }
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.bottom, 10)
                }
                .scrollIndicators(.automatic)
                .frame(maxHeight: .infinity)
                .onChange(of: model.selected) {
                    if let s = model.selected, let id = model.rowID(for: s) { proxy.scrollTo(id) }
                }
            }
        }
    }
}

struct ResultRow: View {
    let hit: Hit
    let selected: Bool
    var actionsOpen = false

    /// File names and paths keep both ends (the name and extension matter); prose like session titles keeps its start.
    private var truncation: Text.TruncationMode { hit.kind == "session" ? .tail : .middle }

    var body: some View {
        HStack(spacing: 12) {
            HitIcon(hit: hit, selected: selected)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(hit.title)
                        .font(.system(size: 13.5, weight: .semibold))
                        .lineLimit(1)
                        .truncationMode(truncation)
                    if hit.isExact {
                        Text("exact")
                            .font(.system(size: 9.5, weight: .bold))
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(selected ? Color.white.opacity(0.25) : Color.accentColor.opacity(0.16)))
                            .foregroundStyle(selected ? Color.white : Color.accentColor)
                            .fixedSize()
                    }
                    Spacer(minLength: 0)
                }
                .foregroundStyle(selected ? Color.white : Color.primary)
                if let sub = hit.subtitle, !sub.isEmpty {
                    Text(sub)
                        .font(.system(size: 11))
                        .foregroundStyle(selected ? Color.white.opacity(0.78) : Color.secondary)
                        .lineLimit(1)
                        .truncationMode(truncation)
                }
                if let snip = hit.snippet, !snip.isEmpty {
                    Text(Self.snippet(snip, hit.highlights ?? [], selected: selected))
                        .font(.system(size: 11.5))
                        .lineLimit(1)
                }
            }
            if selected {
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.white.opacity(actionsOpen ? 0.95 : 0.6))
                    .help("Actions: ⌃ or →")
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
        default: return hit.isFolder ? "folder" : "doc.text"
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
    /// The side action list: a smaller glass card floating over the results.
    @ViewBuilder func menuGlass() -> some View {
        let shape = RoundedRectangle(cornerRadius: 14, style: .continuous)
        if #available(macOS 26.0, *) {
            self.background(shape.fill(.regularMaterial))
                .glassEffect(.regular, in: shape)
                .shadow(color: .black.opacity(0.25), radius: 14, y: 6)
        } else {
            self.background(shape.fill(.regularMaterial))
                .overlay(shape.strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5))
                .shadow(color: .black.opacity(0.25), radius: 14, y: 6)
        }
    }

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
