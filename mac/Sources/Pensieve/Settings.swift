import AppKit
import ServiceManagement
import SwiftUI

/// The daemon's settings (GET and POST /settings). Each change is saved at once, and the daemon indexes with it
/// from then on.
@MainActor
final class SettingsModel: ObservableObject {
    @Published private(set) var values: [String: Any] = [:]
    @Published private(set) var status: [String: Any]?
    @Published var error: String?
    @Published var openAtLogin = SMAppService.mainApp.status == .enabled

    func load() {
        values = Daemon.call("settings") ?? [:]
        error = values.isEmpty ? "Pensieve is not running, so the settings cannot load. Start it with `pensieve install`." : nil
    }

    func refreshStatus() async {
        let s = await Task.detached { Daemon.call("status") }.value
        status = s
    }

    func set(_ key: String, _ value: Any) {
        guard let out = Daemon.call("settings", post: [key: value]) else {
            error = "Pensieve is not running, so the change was not saved."
            return
        }
        if let detail = out["detail"] as? String {
            error = detail
            return
        }
        values = out
        error = nil
    }

    func strings(_ key: String) -> [String] { values[key] as? [String] ?? [] }
    func int(_ key: String) -> Int { values[key] as? Int ?? 0 }
    func bool(_ key: String) -> Bool { values[key] as? Bool ?? false }

    func intBinding(_ key: String) -> Binding<Int> {
        Binding(get: { self.int(key) }, set: { self.set(key, $0) })
    }

    func boolBinding(_ key: String) -> Binding<Bool> {
        Binding(get: { self.bool(key) }, set: { self.set(key, $0) })
    }

    /// content, name or off.
    func level(_ type: String) -> String {
        ((values["file_types"] as? [String: Any])?[type] as? [String: Any])?["index"] as? String ?? "name"
    }

    func levelBinding(_ type: String) -> Binding<String> {
        Binding(get: { self.level(type) }, set: { new in
            var types = self.values["file_types"] as? [String: Any] ?? [:]
            var kind = types[type] as? [String: Any] ?? ["map": false]
            kind["index"] = new
            types[type] = kind
            self.set("file_types", types)
        })
    }

    func setOpenAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            self.error = error.localizedDescription
        }
        openAtLogin = SMAppService.mainApp.status == .enabled
    }
}

/// Folders chosen in an open panel, with the home folder as `~`, as the settings keep them.
@MainActor
func pickFolders() -> [String] {
    let open = NSOpenPanel()
    open.canChooseDirectories = true
    open.canChooseFiles = false
    open.allowsMultipleSelection = true
    open.prompt = "Add"
    NSApp.activate(ignoringOtherApps: true)
    guard open.runModal() == .OK else { return [] }
    let home = NSHomeDirectory()
    return open.urls.map { $0.path.hasPrefix(home) ? "~" + $0.path.dropFirst(home.count) : $0.path }
}

/// The rows of a list setting, each with a remove button, and a way to add entries: folders from an open panel
/// (`add`) or text from a field (`placeholder`).
struct ListEditor: View {
    let items: [String]
    let save: ([String]) -> Void
    var placeholder: String?
    var add: (() -> [String])?
    @State private var draft = ""

    var body: some View {
        ForEach(items, id: \.self) { item in
            HStack {
                Text(item).lineLimit(1).truncationMode(.middle)
                Spacer()
                Button { save(items.filter { $0 != item }) } label: { Image(systemName: "minus.circle") }
                    .buttonStyle(.borderless)
                    .help("Remove")
            }
        }
        if let placeholder {
            HStack {
                TextField("", text: $draft, prompt: Text(placeholder)).labelsHidden().onSubmit(commit)
                Button("Add", action: commit).disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        } else if let add {
            Button("Add Folder…") { append(add()) }
        }
    }

    private func commit() {
        append([draft.trimmingCharacters(in: .whitespaces)])
        draft = ""
    }

    private func append(_ new: [String]) {
        let fresh = new.filter { !$0.isEmpty && !items.contains($0) }
        if !fresh.isEmpty { save(items + fresh) }
    }
}

struct GeneralTab: View {
    @ObservedObject var m: SettingsModel

    var body: some View {
        Form {
            Section("Daemon") {
                Text(Daemon.statusLine(m.status))
                if let busy = m.status?["busy"] as? [String], !busy.isEmpty {
                    Text("Indexing: \(busy.joined(separator: ", "))").foregroundStyle(.secondary)
                }
                Button("Open Data Folder") { Program.openData() }
            }
            Section {
                Button("Install for Claude Code and Codex…") { Program.install() }.disabled(Program.path == nil)
                Button("Uninstall…") { Program.uninstall() }.disabled(Program.path == nil)
            } header: {
                Text("Agents")
            } footer: {
                Text("Install starts the daemon at login, and adds the MCP server and the skills to Claude Code and Codex.")
            }
            Section("Menu bar") {
                Toggle("Open the menu bar item at login", isOn: Binding(get: { m.openAtLogin }, set: { m.setOpenAtLogin($0) }))
                if SMAppService.mainApp.status == .requiresApproval {
                    Text("Allow Pensieve in System Settings › General › Login Items.").foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .task {
            while !Task.isCancelled {
                await m.refreshStatus()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }
}

struct SourcesTab: View {
    @ObservedObject var m: SettingsModel
    private static let agents = [("claude", "Claude Code"), ("codex", "Codex"), ("qwen", "Qwen Code")]

    var body: some View {
        Form {
            Section {
                ListEditor(items: m.strings("folders"), save: { m.set("folders", $0) }, add: pickFolders)
            } header: {
                Text("Document folders")
            } footer: {
                Text("Pensieve indexes the documents in these folders. File Types says which ones.")
            }
            Section {
                ListEditor(items: m.strings("repos"), save: { m.set("repos", $0) }, add: pickFolders)
            } header: {
                Text("Repos")
            } footer: {
                Text("Repos, or folders of repos, that are always indexed, at any size.")
            }
            Section {
                ListEditor(items: m.strings("sweep_roots"), save: { m.set("sweep_roots", $0) }, add: pickFolders)
                Stepper("Folder levels to look in: \(m.int("sweep_depth"))", value: m.intBinding("sweep_depth"), in: 1...10)
                Stepper("Largest repo found this way: \(m.int("max_repo_files")) files", value: m.intBinding("max_repo_files"),
                        in: 1000...100_000, step: 1000)
            } header: {
                Text("Find git repos in")
            } footer: {
                Text("A larger repo is indexed only when an agent works in it, or when you add it above.")
            }
            Section {
                Toggle("Skip sessions that ran in a temporary folder", isOn: m.boolBinding("skip_temp_sessions"))
                ListEditor(items: m.strings("session_folders"), save: { m.set("session_folders", $0) })
                Menu("Add Folder…") {
                    ForEach(Self.agents, id: \.0) { id, name in
                        Button(name) {
                            let new = pickFolders().map { "\(id):\($0)" }.filter { !m.strings("session_folders").contains($0) }
                            if !new.isEmpty { m.set("session_folders", m.strings("session_folders") + new) }
                        }
                    }
                }
                .fixedSize()
            } header: {
                Text("Agent sessions")
            } footer: {
                Text("Pensieve reads ~/.claude and ~/.codex. Add other folders of agent sessions here.")
            }
        }
        .formStyle(.grouped)
    }
}

struct FileTypesTab: View {
    @ObservedObject var m: SettingsModel
    private static let types = [
        ("text", "Text and notes", "Plain text, Markdown, RTF, mail, subtitles"),
        ("pdf", "PDFs", "Text only, no scanned images"),
        ("office", "Office and iWork", "Word, PowerPoint, Excel, Pages, Keynote, Numbers"),
        ("data", "Data", "CSV, TSV, JSON, notebooks"),
        ("code", "Code and config", "Outside git repos; repos are indexed as repos"),
        ("images", "Images", "By what they show"),
        ("video", "Video", "By what they show"),
        ("audio", "Audio", "By what is said or heard"),
        ("other", "Other files", "Archives, apps and everything else"),
    ]

    var body: some View {
        Form {
            Section {
                ForEach(Self.types, id: \.0) { id, name, detail in
                    Picker(selection: m.levelBinding(id)) {
                        if id != "other" { Text("Content").tag("content") }
                        Text("Name").tag("name")
                        Text("Off").tag("off")
                    } label: {
                        VStack(alignment: .leading) {
                            Text(name)
                            Text(detail).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            } header: {
                Text("Read each type by")
            } footer: {
                Text("Content: search finds what the file says. Name: only its name. Images, video and audio by content need a model part, which Pensieve downloads when such a file is in use.")
            }
            Section("Limits") {
                Stepper("Skip files larger than \(m.int("max_file_mb")) MB", value: m.intBinding("max_file_mb"), in: 1...1000)
                Stepper(m.int("max_chunks") == 0 ? "Read all of each document" : "Read the first \(m.int("max_chunks")) pieces of each document",
                        value: m.intBinding("max_chunks"), in: 0...20_000, step: 500)
            }
        }
        .formStyle(.grouped)
    }
}

struct PrivacyTab: View {
    @ObservedObject var m: SettingsModel

    var body: some View {
        Form {
            Section {
                ListEditor(items: m.strings("exclude"), save: { m.set("exclude", $0) }, placeholder: "Folder name, for example node_modules")
            } header: {
                Text("Never open folders named")
            }
            Section {
                ListEditor(items: m.strings("exclude_files"), save: { m.set("exclude_files", $0) }, placeholder: "Pattern, for example *.pem")
            } header: {
                Text("Never index files that match")
            } footer: {
                Text("Patterns ignore case. * matches any text. The defaults keep keys, password exports and recovery codes out of the index.")
            }
        }
        .formStyle(.grouped)
    }
}

struct SettingsView: View {
    @ObservedObject var m: SettingsModel

    var body: some View {
        VStack(spacing: 0) {
            TabView {
                GeneralTab(m: m).tabItem { Label("General", systemImage: "gearshape") }
                SourcesTab(m: m).tabItem { Label("Sources", systemImage: "folder") }
                FileTypesTab(m: m).tabItem { Label("File Types", systemImage: "doc") }
                PrivacyTab(m: m).tabItem { Label("Privacy", systemImage: "hand.raised") }
            }
            if let e = m.error {
                Text(e).foregroundStyle(.red).padding(8)
            }
        }
        .frame(width: 600, height: 560)
    }
}

@MainActor
enum SettingsWindow {
    private static var window: NSWindow?
    private static let model = SettingsModel()

    static func show() {
        model.load()
        if window == nil {
            let w = NSWindow(contentViewController: NSHostingController(rootView: SettingsView(m: model)))
            w.title = "Pensieve Settings"
            w.styleMask = [.titled, .closable, .miniaturizable]
            w.isReleasedWhenClosed = false
            w.center()
            window = w
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
