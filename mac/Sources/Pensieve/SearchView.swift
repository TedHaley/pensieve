import AppKit
import SwiftUI

struct SearchView: View {
    @ObservedObject var model: SearchModel
    @ObservedObject var backend = Backend.shared
    @ObservedObject var updater = Updater.shared

    var body: some View {
        VStack(spacing: 0) {
            bar
            HintLine(model: model, backend: backend)
                .frame(height: model.hintHeight, alignment: .top)  // the options overflow it while closing
            if model.listHeight > 0 {
                Divider().opacity(0.6).padding(.horizontal, 14)
                ResultsList(model: model)
            }
        }
        .frame(width: model.glassSize.width, height: model.glassSize.height, alignment: .top)
        .clipShape(RoundedRectangle(cornerRadius: SearchModel.cornerRadius, style: .continuous))
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
        .scaleEffect(model.presented ? 1 : 0.95, anchor: .top)  // grows into place while the window fades in
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var optionsChevron: some View {
        let shown = model.showsGuidance && (model.buttonsShown || model.optionsOpen)  // open options keep their way back
        return Button { model.optionsOpen.toggle() } label: {
            Image(systemName: "chevron.down")
                .font(.system(size: 14, weight: .semibold))
                .rotationEffect(.degrees(model.optionsOpen ? 180 : 0))
                .animation(.spring(response: 0.3, dampingFraction: 0.8), value: model.optionsOpen)
                .frame(width: 30, height: 30)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
        .help(model.optionsOpen ? "Hide search options" : "Show search options")
        .opacity(shown ? 1 : 0)
        .scaleEffect(shown ? 1 : 0.6)
        .allowsHitTesting(shown)
        .animation(shown ? .spring(response: 0.38, dampingFraction: 0.7) : .easeIn(duration: 0.14), value: shown)
    }

    @ViewBuilder private var scopeMenu: some View {
        let names = ((backend.settings["scopes"] as? [String: Any]) ?? [:]).keys.sorted()
        let current = backend.settings["default_scope"] as? String ?? ""
        if !names.isEmpty {
            Menu {
                Button { pickScope("") } label: { Label("Everything", systemImage: current.isEmpty ? "checkmark" : "") }
                Divider()
                ForEach(names, id: \.self) { n in
                    Button { pickScope(n) } label: { Label(n, systemImage: current == n ? "checkmark" : "") }
                }
            } label: {
                Text(current.isEmpty ? "Everything" : current)
                    .font(.system(size: 13, weight: .medium))
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.visible)
            .fixedSize()
            .foregroundStyle(.secondary)
            .help("Search in this scope")
        }
    }

    private func pickScope(_ name: String) {
        Task {
            await backend.updateSettings(["default_scope": name])
            model.retry()
        }
    }

    private var bar: some View {
        HStack(spacing: 12) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 21, weight: .medium))
                .foregroundStyle(.secondary)
            TextField("Search", text: $model.query)
                .textFieldStyle(.plain)
                .font(.system(size: 24, weight: .regular))
                .overlay(alignment: .leading) {
                    if let c = model.completion {
                        // the typed text, invisible, positions the rest of the Top Hit's name right after the caret
                        (Text(model.query).foregroundColor(.clear) + Text(c.rest).foregroundColor(.secondary)
                            + Text(" — \(c.kind)").foregroundColor(Color.secondary.opacity(0.6)))
                            .font(.system(size: 24, weight: .regular))
                            .lineLimit(1)
                            .allowsHitTesting(false)
                    }
                }
            if model.loading {
                ProgressView().controlSize(.small)
            }
            scopeMenu
            if let r = updater.available, !updater.busy {
                Text("⌘U to update")
                    .font(.system(size: 13))
                    .foregroundStyle(.tertiary)
                    .fixedSize()
                    .help("Install Pensieve \(r.version)")
            }
            optionsChevron
        }
        .padding(.leading, 20)
        .padding(.trailing, 14)
        .frame(height: SearchModel.barHeight)
        .contentShape(Rectangle())
        .onHover { model.overBar = $0 }
    }
}

/// The map and Settings buttons: glass circles in their own window to the right of the bar, as in Spotlight.
struct BarButtons: View {
    @ObservedObject var model: SearchModel

    var body: some View {
        HStack(spacing: SearchModel.buttonGap) {
            GlassCircle(symbol: "circle.hexagongrid", help: "Open the Map ⌘O", index: 0, shown: model.buttonsShown) {
                model.actions?.visualize(nil)
            }
            GlassCircle(symbol: "gearshape", help: "Settings ⌘,", index: 1, shown: model.buttonsShown) {
                model.actions?.settings()
            }
        }
        .padding(.leading, SearchModel.buttonGap)
        .frame(height: SearchModel.barHeight)
        .contentShape(Rectangle())
        .onHover { model.overButtons = $0 }
        .padding([.top, .bottom, .trailing], SearchModel.buttonPad)
        .padding(.leading, SearchModel.buttonPad - SearchModel.buttonGap)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

/// Drops into place with a spring when shown, each circle a little after the one before it.
struct GlassCircle: View {
    let symbol: String
    let help: String
    let index: Int
    let shown: Bool
    let action: () -> Void
    @State private var over = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 19, weight: .medium))
                .foregroundStyle(over ? .primary : .secondary)
                .frame(width: SearchModel.buttonSize, height: SearchModel.buttonSize)
                .background(Circle().fill(Color.primary.opacity(over ? 0.08 : 0)))
                .circleGlass()
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help(help)
        .onHover { over = $0 }
        .scaleEffect(shown ? 1 : 0.55)
        .offset(y: shown ? 0 : -SearchModel.buttonPad * 0.75)
        .blur(radius: shown ? 0 : 4)
        .opacity(shown ? 1 : 0)
        .allowsHitTesting(shown)
        .animation(shown ? .spring(response: 0.42, dampingFraction: 0.62).delay(Double(index) * 0.06)
                         : .easeIn(duration: 0.14), value: shown)
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
            } else if model.showsGuidance {
                if model.optionsOpen {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(SearchModel.options, id: \.0) { key, what in
                            HStack(spacing: 0) {
                                Text(key).font(.system(size: SearchModel.hintFontSize, design: .monospaced))
                                    .foregroundStyle(.primary.opacity(0.8))
                                    .frame(width: 215, alignment: .leading)
                                Text(what)
                            }
                            .frame(height: SearchModel.optionRowHeight, alignment: .leading)
                        }
                    }
                    .transition(.opacity.combined(with: .offset(y: -8)))
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
        .lineLimit(model.showsGuidance ? nil : 6)
        .padding(.top, 2)
        .padding(.leading, SearchModel.hintInsets.leading)
        .padding(.trailing, SearchModel.hintInsets.trailing)
        .animation(SearchModel.resize, value: model.optionsOpen)  // the options list slides in with the glass
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
    @Namespace private var selection  // the highlight slides between rows instead of jumping

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
                                ResultRow(hit: hit, selected: on, actionsOpen: on && model.actionsOpen, highlight: selection)
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
    var highlight: Namespace.ID?

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
        .background {
            if selected {
                let shape = RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.accentColor)
                if let highlight { shape.matchedGeometryEffect(id: "selection", in: highlight) } else { shape }
            }
        }
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
            if hit.kind == "file" || hit.kind == "app" || hit.kind == "document", let p = hit.path, FileManager.default.fileExists(atPath: p) {
                Image(nsImage: IconCache.icon(for: p))
                    .resizable()
                    .interpolation(.high)
                    .frame(width: 32, height: 32)
            } else if hit.kind == "setting", let g = Launcher.shared.glyph(hit) {
                Image(systemName: g.symbol)  // a colored tile, as in System Settings' sidebar
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 28, height: 28)
                    .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(g.color.gradient))
            } else if hit.kind == "setting" {
                Image(nsImage: IconCache.icon(for: Launcher.settingsApp))
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
        case "person": return "person.crop.circle"
        case "repo": return "shippingbox"
        case "calc": return hit.ext == "conversion" ? "arrow.left.arrow.right" : "equal"
        default: return hit.isFolder ? "folder" : "doc.text"
        }
    }

    private var tint: Color {
        switch hit.kind {
        case "code": return .teal
        case "session": return .purple
        case "person": return .pink
        case "repo", "calc": return .orange
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

    /// The bar's glass in a circle. These sit outside the panel's window, so they draw their own shadow, cut away
    /// inside the circle: under translucent glass it would show through and make the circle darker than the bar.
    /// The bar's window shadow also draws a hairline edge; the circles' window has no shadow, so they stroke one.
    @ViewBuilder func circleGlass() -> some View {
        let shadow = Circle().fill(Color.black)
            .shadow(color: .black.opacity(0.25), radius: 12, y: 5)
            .mask(Rectangle().padding(-40).overlay(Circle().blendMode(.destinationOut)).compositingGroup())
        if #available(macOS 26.0, *) {
            self.glassEffect(.regular, in: Circle())
                .overlay(Circle().strokeBorder(Color.primary.opacity(0.28), lineWidth: 1))
                .background(shadow)
        } else {
            self.background(VisualEffect().clipShape(Circle()))
                .overlay(Circle().strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5))
                .background(shadow)
        }
    }

    /// Spotlight's look: Liquid Glass on macOS 26, a popover-style blur before that.
    @ViewBuilder func panelGlass() -> some View {
        let shape = RoundedRectangle(cornerRadius: SearchModel.cornerRadius, style: .continuous)
        if #available(macOS 26.0, *) {
            self.glassEffect(.regular, in: shape)
        } else {
            self.background(VisualEffect().clipShape(shape))
                .overlay(shape.strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5))
        }
    }
}
