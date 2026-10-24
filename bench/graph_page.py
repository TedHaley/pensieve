"""Write the relationship graph as one HTML page, from bench/out/graph/relations.json.

  .venv/bin/python bench/graph_page.py     # writes bench/out/graph/graph.html

Sessions are grey circles. Branches, PRs and tickets have their own colours. Solid lines are writes; dashed lines are
mentions. Drag a node, hover for its label, and filter by kind and strength. Nothing is sent anywhere."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "out" / "graph"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Work graph</title>
<style>
:root { --bg:#f6f6f4; --ink:#1b1b1d; --muted:#6b6b70; --line:#c8c8c4; --panel:#ffffff; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#161617; --ink:#ececec; --muted:#9a9aa0;
  --line:#4a4a50; --panel:#1f1f21; color-scheme: dark; } }
:root[data-theme="dark"] { --bg:#161617; --ink:#ececec; --muted:#9a9aa0; --line:#4a4a50; --panel:#1f1f21; color-scheme: dark; }
body { margin:0; background:var(--bg); color:var(--ink); font:13px/1.4 -apple-system, system-ui, sans-serif;
  padding-left:max(16px, env(safe-area-inset-left, 0px)); padding-right:max(16px, env(safe-area-inset-right, 0px)); }
header { display:flex; flex-wrap:wrap; gap:12px; align-items:center; padding:12px 0; }
h1 { font-size:18px; margin:0; font-weight:650; }
.legend span { display:inline-flex; align-items:center; gap:5px; margin-right:12px; color:var(--muted); }
.legend i { width:10px; height:10px; border-radius:50%; display:inline-block; }
label { color:var(--muted); }
svg { width:100%; height:72vh; background:var(--panel); border:1px solid var(--line); border-radius:12px; touch-action:none; }
#tip { position:fixed; pointer-events:none; background:var(--panel); border:1px solid var(--line); padding:6px 9px;
  border-radius:8px; font-size:12px; display:none; max-width:360px; }
.note { color:var(--muted); font-size:12px; margin-top:8px; }
</style>
</head>
<body>
<header>
  <h1>Work graph</h1>
  <div class="legend">
    <span><i style="background:#8e8e93"></i>session</span>
    <span><i style="background:#0a84ff"></i>branch</span>
    <span><i style="background:#bf5af2"></i>PR</span>
    <span><i style="background:#ff9f0a"></i>ticket</span>
  </div>
  <label><input id="mentions" type="checkbox" checked> mentions</label>
  <label><input id="sessions" type="checkbox" checked> sessions</label>
  <span class="note" id="count"></span>
</header>
<svg id="g" viewBox="0 0 1000 720" preserveAspectRatio="xMidYMid meet"></svg>
<div id="tip"></div>
<p class="note">Solid: the session wrote to it. Dashed: the session named it in text only.</p>
<script>
const DATA = __DATA__;
const COLOR = {session:'#8e8e93', branch:'#0a84ff', pr:'#bf5af2', ticket:'#ff9f0a'};
const svg = document.getElementById('g'), tip = document.getElementById('tip');
const NS = 'http://www.w3.org/2000/svg';
let nodes = [], links = [], idx = {};

function build() {
  const showM = document.getElementById('mentions').checked, showS = document.getElementById('sessions').checked;
  const keep = new Set();
  const rel = DATA.relations.filter(r => showM || r.strength === 'write');
  rel.forEach(r => { keep.add(r.source); keep.add(r.target); });
  const pairs = new Map();
  rel.forEach(r => {
    const k = r.source + '|' + r.target, p = pairs.get(k) || {source: r.source, target: r.target, n: 0, strong: false};
    p.n++; p.strong = p.strong || r.strength === 'write'; pairs.set(k, p);
  });
  nodes = DATA.nodes.filter(n => keep.has(n.id) && (showS || n.kind !== 'session')).map(n => ({...n,
    x: 500 + (Math.random() - .5) * 400, y: 360 + (Math.random() - .5) * 300, vx: 0, vy: 0}));
  idx = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
  links = [...pairs.values()].filter(p => p.source in idx && p.target in idx).map(p => ({...p, s: idx[p.source], t: idx[p.target]}));
  document.getElementById('count').textContent = nodes.length + ' nodes, ' + links.length + ' links';
  draw();
  settle(400);
}

function step() {
  for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
    const a = nodes[i], b = nodes[j]; let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01;
    const f = 900 / d2, d = Math.sqrt(d2); dx /= d; dy /= d;
    a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f;
  }
  links.forEach(l => { const a = nodes[l.s], b = nodes[l.t]; const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) + 0.01, k = (d - 70) * 0.02 * (l.strong ? 1.3 : 0.6);
    a.vx += dx / d * k; a.vy += dy / d * k; b.vx -= dx / d * k; b.vy -= dy / d * k; });
  nodes.forEach(n => { n.vx += (500 - n.x) * 0.002; n.vy += (360 - n.y) * 0.002;
    n.x += n.vx * 0.4; n.y += n.vy * 0.4; n.vx *= 0.6; n.vy *= 0.6; });
}
function settle(k) { for (let i = 0; i < k; i++) step(); draw(); }

function draw() {
  svg.innerHTML = '';
  links.forEach(l => { const a = nodes[l.s], b = nodes[l.t];
    const ln = document.createElementNS(NS, 'line');
    ln.setAttribute('x1', a.x); ln.setAttribute('y1', a.y); ln.setAttribute('x2', b.x); ln.setAttribute('y2', b.y);
    ln.setAttribute('stroke', 'var(--line)'); ln.setAttribute('stroke-width', l.strong ? 1.4 : 1);
    if (!l.strong) ln.setAttribute('stroke-dasharray', '4 4');
    svg.appendChild(ln); });
  nodes.forEach(n => { const c = document.createElementNS(NS, 'circle');
    c.setAttribute('cx', n.x); c.setAttribute('cy', n.y); c.setAttribute('r', n.kind === 'session' ? 4 : 7);
    c.setAttribute('fill', COLOR[n.kind] || '#999');
    c.addEventListener('mouseenter', e => { tip.style.display = 'block'; tip.textContent = n.kind + ': ' + n.label; });
    c.addEventListener('mousemove', e => { tip.style.left = (e.clientX + 12) + 'px'; tip.style.top = (e.clientY + 12) + 'px'; });
    c.addEventListener('mouseleave', () => tip.style.display = 'none');
    svg.appendChild(c); });
}

document.getElementById('mentions').addEventListener('change', build);
document.getElementById('sessions').addEventListener('change', build);
build();
</script>
</body>
</html>
"""


def main():
    data = json.loads((OUT / "relations.json").read_text())
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (OUT / "graph.html").write_text(PAGE.replace("__DATA__", blob))
    print(f"written: {OUT / 'graph.html'} ({(OUT / 'graph.html').stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
