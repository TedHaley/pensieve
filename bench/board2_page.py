"""Write the work board (model v2) as one HTML page, from bench/out/board/v2.json.

  .venv/bin/python bench/board2_page.py    # writes bench/out/board/v2.html

Threads are the work that writes to a shared sink. Each shows its sinks and their completion. Loose work and notes
(questions and comments) are listed below them. Nothing is sent anywhere."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "out" / "board"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="claude:viewport-fit-sides" content="cover">
<title>Work board</title>
<style>
:root {
  --bg: #f6f6f4; --panel: #ffffff; --ink: #1b1b1d; --muted: #6b6b70; --line: #dcdcd8; --chip: #ececE8;
  --open: #1f8a3b; --loose: #b26a00; --closed: #6b6b70; --forgotten: #b3261e; --done: #1f8a3b; --unk: #6b6b70;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161617; --panel: #1f1f21; --ink: #ececec; --muted: #9a9aa0; --line: #34343a; --chip: #2a2a2e;
    --open: #4cd46e; --loose: #f0a33a; --closed: #9a9aa0; --forgotten: #ff7a70; --done: #4cd46e; --unk: #9a9aa0;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #161617; --panel: #1f1f21; --ink: #ececec; --muted: #9a9aa0; --line: #34343a; --chip: #2a2a2e;
  --open: #4cd46e; --loose: #f0a33a; --closed: #9a9aa0; --forgotten: #ff7a70; --done: #4cd46e; --unk: #9a9aa0;
  color-scheme: dark;
}
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 20px 40px; }
main { max-width: 1040px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; min-width: 0; }
h1 { font-size: 22px; margin: 0; font-weight: 650; }
h2 { font-size: 15px; margin: 0 0 8px; font-weight: 650; }
.sub, .note { color: var(--muted); font-size: 12px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
.stat b { display: block; font-size: 20px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 12px; }
.bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
select, input { font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 9px; min-width: 0; }
input[type=search] { flex: 1 1 200px; }
.pill { display: inline-block; font-size: 12px; padding: 2px 8px; border-radius: 99px; background: var(--chip); white-space: nowrap; }
.open { color: var(--open); } .loose_ends { color: var(--loose); } .closed { color: var(--closed); }
.forgotten { color: var(--forgotten); } .done { color: var(--done); } .unknown, .open_sink { color: var(--unk); }
.thread { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.thread summary { cursor: pointer; display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; list-style: none; }
.thread summary::-webkit-details-marker { display: none; }
.label { font-weight: 600; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 12px; grid-column: 1 / -1; }
.item { border-top: 1px solid var(--line); padding-top: 6px; margin-top: 6px; min-width: 0; }
.item .t { font-weight: 550; overflow-wrap: anywhere; }
.item .d { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.grid { display: flex; flex-direction: column; gap: 8px; }
.limits li { margin: 4px 0; color: var(--muted); }
</style>
</head>
<body>
<main>
  <div>
    <h1>Work board</h1>
    <div class="sub" id="built"></div>
  </div>
  <div class="stats" id="stats"></div>

  <section>
    <h2>Threads: work written to a sink</h2>
    <div class="bar">
      <select id="state" aria-label="Filter by state">
        <option value="">All states</option>
        <option value="open">Open</option>
        <option value="loose_ends">Loose ends</option>
        <option value="closed">Closed</option>
        <option value="forgotten">Forgotten</option>
      </select>
      <input id="q" type="search" placeholder="Search labels and components" aria-label="Search">
      <span class="note" id="count"></span>
    </div>
    <div class="grid" id="threads" style="margin-top:10px"></div>
  </section>

  <section>
    <h2>Loose work: no sink yet</h2>
    <div class="note">Work with no PR or branch of its own. It is not a thread until it writes to a sink.</div>
    <details style="margin-top:10px"><summary class="note" id="loose-sum"></summary>
      <div class="grid" id="loose" style="margin-top:10px"></div>
    </details>
  </section>

  <section>
    <h2>Notes: questions and comments</h2>
    <div class="note">Notes never join a thread and never close.</div>
    <div class="grid" id="notes" style="margin-top:10px"></div>
  </section>

  <section>
    <h2>Limits</h2>
    <ul class="limits" id="limits"></ul>
  </section>
</main>
<script>
const DATA = __DATA__;
const LABEL = {open: "Open", loose_ends: "Loose ends", closed: "Closed", forgotten: "Forgotten"};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ago = ts => ts ? new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ") : "";

function fill() {
  const c = DATA.counts;
  $("built").textContent = "Built " + DATA.built + " from " + c.sessions + " sessions";
  const cells = [["Sessions", c.sessions], ["Threads", c.threads], ["Loose work", c.loose_work],
    ["Notes", c.notes], ["Sinks joined", c.joined_sinks]];
  $("stats").innerHTML = cells.map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
  $("limits").innerHTML = DATA.limits.map(l => `<li>${esc(l)}</li>`).join("");
  $("loose-sum").textContent = DATA.loose_work.length + " loose work components";
}

function sinkChips(t) {
  return t.sinks.map(s => `<span class="pill ${s.completion === "done" ? "done" : s.completion === "open" ? "open_sink" : "unknown"}">${esc(s.label)} · ${esc(s.completion)}</span>`).join(" ");
}

function threadCard(t) {
  const comps = t.components.map(c => `<div class="item">
      <div class="t">${esc(c.title)}</div>
      <div class="d">${esc(c.description)}</div>
      <div class="d">${esc(c.session.replace("claude:", "").slice(0, 13))} · exchanges ${c.exchanges[0]}–${c.exchanges[1]}</div>
    </div>`).join("");
  return `<details class="thread">
    <summary>
      <span class="label">${esc(t.label)}</span>
      <span class="pill ${t.state}">${LABEL[t.state] || t.state}</span>
      <span class="meta">${sinkChips(t)} · ${t.components.length} component${t.components.length === 1 ? "" : "s"} · ${t.sessions.length} session${t.sessions.length === 1 ? "" : "s"} · ${esc(t.state_why)}</span>
    </summary>
    <div>${comps}</div>
  </details>`;
}

function renderThreads() {
  const state = $("state").value, q = $("q").value.trim().toLowerCase();
  const rows = DATA.threads.filter(t => {
    if (state && t.state !== state) return false;
    if (!q) return true;
    return (t.label + " " + t.components.map(c => c.title + " " + c.description).join(" ")).toLowerCase().includes(q);
  });
  $("count").textContent = rows.length + " of " + DATA.threads.length + " threads";
  $("threads").innerHTML = rows.map(threadCard).join("") || `<div class="note">No threads match.</div>`;
}

function renderLoose() {
  $("loose").innerHTML = DATA.loose_work.slice(0, 300).map(c => `<div class="thread">
      <div class="label">${esc(c.title)}</div>
      <div class="d">${esc(c.description)}</div>
      <div class="note">${esc(c.session.replace("claude:", "").slice(0, 13))} · ${ago(c.last_active)}</div>
    </div>`).join("");
}

function renderNotes() {
  $("notes").innerHTML = DATA.notes.map(n => `<div class="thread">
      <div class="label">${esc(n.title)} <span class="pill">${esc(n.kind)}</span></div>
      <div class="d">${esc(n.description)}</div>
      <div class="note">${esc(n.session.replace("claude:", "").slice(0, 13))} · ${ago(n.last_active)}</div>
    </div>`).join("") || `<div class="note">No notes.</div>`;
}

fill(); renderThreads(); renderLoose(); renderNotes();
["state", "q"].forEach(id => $(id).addEventListener("input", renderThreads));
</script>
</body>
</html>
"""


def main():
    data = json.loads((OUT / "v2.json").read_text())
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (OUT / "v2.html").write_text(PAGE.replace("__DATA__", blob))
    print(f"written: {OUT / 'v2.html'} ({(OUT / 'v2.html').stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
