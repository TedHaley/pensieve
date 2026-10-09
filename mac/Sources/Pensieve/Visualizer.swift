import AppKit
import WebKit

/// The web visualizer (maps, code, insights) in a native window. While it is open the app shows in the Dock
/// and the app switcher; closing it returns Pensieve to a menu-bar-only app.
@MainActor
final class VisualizerController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow?
    private var web: WKWebView?
    private var pendingFragment: String?
    private var loaded = false
    var webView: WKWebView? { web }

    /// `fragment` is already encoded (see `fragment(_:)`), e.g. "settings" or "open=code%3A12&q=umap".
    func show(fragment: String? = nil) {
        if window == nil { create() }
        NSApp.setActivationPolicy(.regular)
        if loaded, let fragment {
            route(fragment)
        } else if !loaded {
            pendingFragment = fragment ?? pendingFragment
            if web?.isLoading != true { load() }
        }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    func reload() {
        loaded = false
        load()
    }

    private func create() {
        let config = WKWebViewConfiguration()
        config.applicationNameForUserAgent = "PensieveMac/2"  // 2: handles pensieve://check-updates
        config.userContentController.addUserScript(WKUserScript(
            source: "window.pensieveNative = true;", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let web = WKWebView(frame: .zero, configuration: config)
        web.underPageBackgroundColor = .clear
        web.navigationDelegate = self
        web.uiDelegate = self
        web.isInspectable = true
        web.allowsBackForwardNavigationGestures = false

        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 900),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = "Pensieve"
        w.titlebarAppearsTransparent = true
        w.titleVisibility = .hidden  // the page's own glass top bar sits under the traffic lights
        w.isReleasedWhenClosed = false
        w.minSize = NSSize(width: 720, height: 480)
        w.contentView = web
        w.delegate = self
        if !w.setFrameUsingName("PensieveVisualizer") { w.center() }
        w.setFrameAutosaveName("PensieveVisualizer")
        window = w
        self.web = web
    }

    private func load() {
        let frag = pendingFragment  // kept until the real page loads, so a failed attempt doesn't lose it
        web?.load(URLRequest(url: Backend.shared.url("", fragment: frag)))
    }

    /// Re-route an already-loaded page: set the hash, or re-fire hashchange if it is already the same.
    private func route(_ fragment: String) {
        let enc = fragment
        let js = """
        (function(h){ if (location.hash === '#' + h) window.dispatchEvent(new HashChangeEvent('hashchange'));
                      else location.hash = h; })(\(Self.jsString(enc)));
        """
        web?.evaluateJavaScript(js)
    }

    /// [("open", "file:/a b/c.md"), ("q", "umap")] -> "open=file%3A%2Fa%20b%2Fc.md&q=umap". Each value is
    /// URI-encoded on its own; nil or empty values are left out. Returns nil when nothing is left.
    static func fragment(_ pairs: [(String, String?)]) -> String? {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        let parts = pairs.compactMap { key, value -> String? in
            guard let v = value?.trimmingCharacters(in: .whitespacesAndNewlines), !v.isEmpty else { return nil }
            return "\(key)=\(v.addingPercentEncoding(withAllowedCharacters: allowed) ?? "")"
        }
        return parts.isEmpty ? nil : parts.joined(separator: "&")
    }

    static func jsString(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())
    }

    // MARK: window

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    // MARK: navigation

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let u = webView.url, Backend.shared.isLocal(u) else { return }  // not the "waiting" placeholder
        loaded = true
        pendingFragment = nil  // it was part of the URL we just loaded
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        loaded = false
        let html = """
        <html><body style="font: 14px -apple-system; color: #888; background: transparent; display: flex;
        align-items: center; justify-content: center; height: 90vh">Waiting for the Pensieve backend…</body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(2))
            guard let self, self.window?.isVisible == true, !self.loaded else { return }
            self.load()
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        guard let u = action.request.url else { return decisionHandler(.allow) }
        let scheme = u.scheme?.lowercased() ?? ""
        if scheme == "about" || scheme == "data" || (["http", "https"].contains(scheme) && Backend.shared.isLocal(u)) {
            return decisionHandler(.allow)
        }
        if scheme == "pensieve" {
            (NSApp.delegate as? AppDelegate)?.handle(u)
        } else {
            NSWorkspace.shared.open(u)  // other sites, vscode://, file:// ... go to their own apps
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url { NSWorkspace.shared.open(u) }
        return nil
    }
}
