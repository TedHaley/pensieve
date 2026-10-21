"""Write the threads board as one HTML page, from bench/out/board/board.json.

  .venv/bin/python bench/board_page.py    # writes bench/out/board/board.html

The page holds the data inline. It filters by state and area, and it shows each thread's components. Items (questions
and comments) are listed in their own section. Nothing is sent anywhere."""
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
<title>Threads board</title>
<style>
:root {
  --bg: #f6f6f4; --panel: #ffffff; --ink: #1b1b1d; --muted: #6b6b70; --line: #dcdcd8;
  --open: #1f8a3b; --loose: #b26a00; --closing: #0a7ea4; --closed: #6b6b70; --forgotten: #b3261e;
  --chip: #ececE8;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161617; --panel: #1f1f21; --ink: #ececec; --muted: #9a9aa0; --line: #34343a;
    --open: #4cd46e; --loose: #f0a33a; --closing: #5bc8ec; --closed: #9a9aa0; --forgotten: #ff7a70;
    --chip: #2a2a2e; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #161617; --panel: #1f1f21; --ink: #ececec; --muted: #9a9aa0; --line: #34343a;
  --open: #4cd46e; --loose: #f0a33a; --closing: #5bc8ec; --closed: #9a9aa0; --forgotten: #ff7a70;
  --chip: #2a2a2e; color-scheme: dark;
}
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 20px 40px; }
main { max-width: 1040px; margin: 0 auto; display: flex; flex-direction: column; gap: 18px; min-width: 0; }
h1 { font-size: 22px; margin: 0; font-weight: 650; }
h2 { font-size: 15px; margin: 0 0 8px; font-weight: 650; }
.sub { color: var(--muted); font-size: 13px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
.stat b { display: block; font-size: 20px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 12px; }
.bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
select, input { font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 9px; min-width: 0; }
input { flex: 1 1 200px; }
.pill { display: inline-block; font-size: 12px; padding: 2px 8px; border-radius: 99px; background: var(--chip); white-space: nowrap; }
.open { color: var(--open); } .loose_ends { color: var(--loose); } .closing { color: var(--closing); }
.closed { color: var(--closed); } .forgotten { color: var(--forgotten); }
.thread { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.thread summary { cursor: pointer; display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; list-style: none; }
.thread summary::-webkit-details-marker { display: none; }
.goal { font-weight: 600; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 12px; grid-column: 1 / -1; }
.list { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
.comp { border-top: 1px solid var(--line); padding-top: 6px; min-width: 0; }
.comp .t { font-weight: 550; overflow-wrap: anywhere; }
.comp .d { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.links { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.grid { display: flex; flex-direction: column; gap: 8px; }
.note { color: var(--muted); font-size: 12px; }
.card-wrap { border: 1px solid var(--line); border-radius: 12px; padding: 12px; background: var(--bg); min-width: 0; }
.card-head { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; margin-bottom: 10px; min-width: 0; }
.card-head .goal { overflow-wrap: anywhere; }
.limits li { margin: 4px 0; color: var(--muted); }
</style>
</head>
<body>
<main>
  <div>
    <h1>Threads board</h1>
    <div class="sub" id="built"></div>
  </div>

  <div class="stats" id="stats"></div>

  <section>
    <h2>Threads</h2>
    <div class="bar">
      <select id="state" aria-label="Filter by state">
        <option value="">All states</option>
        <option value="open">Open</option>
        <option value="loose_ends">Loose ends</option>
        <option value="closing">Closing</option>
        <option value="closed">Closed</option>
        <option value="forgotten">Forgotten</option>
      </select>
      <label class="note"><input id="small" type="checkbox" style="flex:none"> Show small threads with no sink</label>
      <input id="q" type="search" placeholder="Search goals and components" aria-label="Search">
      <span class="note" id="count"></span>
    </div>
    <div class="grid" id="threads" style="margin-top:10px"></div>
  </section>

  <section>
    <h2>Items: questions and comments</h2>
    <div class="note">Items never join a thread. They sit in their placeholder area.</div>
    <div class="grid" id="items" style="margin-top:10px"></div>
  </section>

  <section>
    <h2>Limits of this board</h2>
    <ul class="limits" id="limits"></ul>
  </section>
</main>
<script>
const DATA = __DATA__;
const LABEL = {open: "Open", loose_ends: "Loose ends", closing: "Closing", closed: "Closed", forgotten: "Forgotten", todo: "To do"};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LAST = {action: "Action left", question: "Question open", nothing: "Nothing left", unclear: "Unclear"};

function fill() {
  const c = DATA.counts;
  $("built").textContent = "Built " + DATA.built + " from " + c.sessions + " sessions";
  const cells = [
    ["Sessions", c.sessions], ["Components", c.components], ["Work components", c.work],
    ["Threads", c.threads], ["Single-component threads", c.single_component_threads], ["Items", c.items]
  ];
  $("stats").innerHTML = cells.map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
  const small = DATA.threads.filter(isSmall).length;
  $("small").parentElement.lastChild.textContent = ` Show small threads with no sink (${small})`;
  $("limits").innerHTML = DATA.limits.map(l => `<li>${esc(l)}</li>`).join("");
}

function isSmall(t) {
  return t.component_count <= 2 && !t.has_sink;
}

function threadCard(t) {
  const e = t.ending;
  const ends = `<div class="comp">
      <div class="t">Ends with: ${esc(e.title)} <span class="pill">${LAST[e.last] || esc(e.last)}</span></div>
      <div class="d">${esc(e.description)}</div>
      <div class="links">${esc(e.session.replace("claude:", "").slice(0, 13))} · exchanges ${e.exchanges[0]}–${e.exchanges[1]}</div>
    </div>`;
  const links = t.context.length
    ? `<details class="comp"><summary class="links">${t.context.length} context link${t.context.length === 1 ? "" : "s"}</summary>
       <div class="links" style="margin-top:6px">${t.context.map(l => `<div>${esc(l.kind)} ${esc(l.key)}</div>`).join("")}</div></details>`
    : `<div class="links">No context links.</div>`;
  return `<details class="thread">
    <summary>
      <span class="goal">${esc(t.goal)}</span>
      <span class="pill ${t.state}">${LABEL[t.state] || t.state}</span>
      <span class="meta">${t.component_count} component${t.component_count === 1 ? "" : "s"} · ${t.sessions.length} session${t.sessions.length === 1 ? "" : "s"} · ${t.sinks && t.sinks.length ? "sink: " + t.sinks.map(esc).join(", ") : "no sink"} · ${esc(t.state_why)}</span>
    </summary>
    <div class="list">${ends}${links}</div>
  </details>`;
}

function renderThreads() {
  const state = $("state").value, showSmall = $("small").checked, q = $("q").value.trim().toLowerCase();
  const rows = DATA.threads.filter(t => {
    if (state && t.state !== state) return false;
    if (!showSmall && isSmall(t)) return false;
    if (!q) return true;
    const hay = (t.goal + " " + t.ending.title + " " + t.ending.description + " " +
      t.context.map(l => l.key).join(" ")).toLowerCase();
    return hay.includes(q);
  });
  $("count").textContent = rows.length + " of " + DATA.threads.length + " threads";
  const shown = new Set(rows.map(t => t.id));
  const html = DATA.cards.map(card => {
    const members = card.threads.filter(id => shown.has(id)).map(id => DATA.threads.find(t => t.id === id));
    if (!members.length) return "";
    const title = card.sink === "No sink" ? "No sink" : card.sink;
    return `<section class="card-wrap">
      <div class="card-head"><span class="goal">${esc(title)}</span>
        <span class="meta">${members.length} thread${members.length === 1 ? "" : "s"}</span></div>
      <div class="grid">${members.map(threadCard).join("")}</div>
    </section>`;
  }).join("");
  $("threads").innerHTML = html || `<div class="note">No threads match.</div>`;
}

function renderItems() {
  $("items").innerHTML = DATA.items.map(i => `<div class="thread">
      <div class="goal">${esc(i.title)} <span class="pill">${esc(i.area)}</span></div>
      <div class="d">${esc(i.description)}</div>
      <div class="links">${esc((i.repo ? i.repo + " · " : "") + i.session.replace("claude:", "").slice(0, 13))}</div>
    </div>`).join("") || `<div class="note">No items.</div>`;
}

fill(); renderThreads(); renderItems();
["state", "small", "q"].forEach(id => $(id).addEventListener("input", renderThreads));
</script>
</body>
</html>
"""


def main():
    board = json.loads((OUT / "board.json").read_text())
    data = json.dumps(board, ensure_ascii=False).replace("</", "<\\/")
    (OUT / "board.html").write_text(PAGE.replace("__DATA__", data))
    print(f"written: {OUT / 'board.html'} ({(OUT / 'board.html').stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
