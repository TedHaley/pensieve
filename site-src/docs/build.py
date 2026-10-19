"""Writes the docs pages in site/docs from the page sources in this folder.

Edit the pages_*.py files, not the HTML in site/docs. Then run: python3 site-src/docs/build.py
The sidebar, the header and the footer are the same on each page, so this script writes them."""
import sys
from html import escape
from pathlib import Path

import pages_a
import pages_b  # noqa: F401  (registers pages)
import pages_c  # noqa: F401
import pages_d  # noqa: F401

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "site" / "docs"
SITE = "https://pensieve.tools"
by = {p["slug"]: p for p in pages_a.PAGES}

GROUPS = [
    ("Start", ["", "install", "faq"]),
    ("Agents", ["agents"]),
    ("Reference", ["mcp-tools", "context-cards", "connections"]),
    ("Evidence", ["benchmark", "privacy"]),
]
CLIENT_SLUGS = [s for s in by if s.startswith("agents/")]
ORDER = ["", "install", "agents", *CLIENT_SLUGS, "mcp-tools", "context-cards", "connections", "benchmark", "privacy", "faq"]
assert sorted(ORDER) == sorted(by), set(by) ^ set(ORDER)


def url(slug):
    return f"/docs/{slug}/" if slug else "/docs/"


def link(slug, cur):
    p = by[slug]
    here = ' aria-current="page"' if slug == cur else ""
    return f'<a href="{url(slug)}"{here}>{escape(p["nav"])}</a>'


def sidebar(cur):
    out = ['<details class="side" open id="docnav"><summary>Docs menu</summary><nav aria-label="Docs">']
    for name, slugs in GROUPS:
        out.append(f"<h2>{name}</h2><ul>")
        for s in slugs:
            if s == "agents":
                out.append(f"<li>{link(s, cur)}<ul>" + "".join(f"<li>{link(c, cur)}</li>" for c in CLIENT_SLUGS) + "</ul></li>")
            else:
                out.append(f"<li>{link(s, cur)}</li>")
        out.append("</ul>")
    out.append("</nav></details>")
    return "".join(out)


def pager(cur):
    i = ORDER.index(cur)
    prev = ORDER[i - 1] if i > 0 else None
    nxt = ORDER[i + 1] if i + 1 < len(ORDER) else None
    a = f'<a class="prev" href="{url(prev)}"><small>Previous</small>{escape(by[prev]["nav"])}</a>' if prev is not None else ""
    b = f'<a class="next" href="{url(nxt)}"><small>Next</small>{escape(by[nxt]["nav"])}</a>' if nxt is not None else ""
    return f'<nav class="pager" aria-label="Pages">{a}{b}</nav>'


_index = (OUT.parent / "index.html").read_text().splitlines()
MARK = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true">\n'
        + "\n".join(l for l in _index if '<symbol id="mark"' in l or '<symbol id="gh"' in l) + "\n</svg>")
assert MARK.count("<symbol") == 2


def render(p):
    slug = p["slug"]
    canon = SITE + url(slug)
    crumb = p["crumb"]
    ld = ('{"@context":"https://schema.org","@type":"TechArticle","headline":' + _j(p["h1"]) + ',"description":' + _j(p["desc"]) +
          ',"url":' + _j(canon) + ',"isPartOf":{"@type":"WebSite","name":"Pensieve","url":"https://pensieve.tools/"},'
          '"about":{"@type":"SoftwareApplication","name":"Pensieve","applicationCategory":"DeveloperApplication","operatingSystem":"macOS, Linux","offers":{"@type":"Offer","price":"0","priceCurrency":"USD"}}}')
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(p["title"])}</title>
<meta name="description" content="{escape(p["desc"])}">
<link rel="canonical" href="{canon}">
<meta name="robots" content="index,follow">
<meta name="theme-color" content="#f4f3ee" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#121211" media="(prefers-color-scheme: dark)">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Pensieve">
<meta property="og:title" content="{escape(p["title"])}">
<meta property="og:description" content="{escape(p["desc"])}">
<meta property="og:url" content="{canon}">
<meta property="og:image" content="https://pensieve.tools/og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.ico">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/docs/docs.css">
<script type="application/ld+json">{ld}</script>
</head>
<body>
{MARK}
<header class="top">
  <a class="brand" href="/" aria-label="Pensieve home"><svg aria-hidden="true"><use href="#mark"/></svg>Pensieve <span>Docs</span></a>
  <nav aria-label="Site">
    <a class="chip gh" href="https://github.com/TedHaley/pensieve"><svg aria-hidden="true"><use href="#gh"/></svg><span>GitHub</span></a>
    <a class="chip solid" href="/docs/install/">Install</a>
  </nav>
</header>
<div class="layout">
{sidebar(slug)}
<main>
<p class="crumb">{escape(crumb)}</p>
<h1>{escape(p["h1"])}</h1>
<p class="lede">{p["lede"]}</p>
{p["body"].strip()}
{pager(slug)}
<p class="edit">Something wrong or missing? <a href="https://github.com/TedHaley/pensieve/issues/new/choose">Tell us on GitHub</a>.</p>
</main>
</div>
<footer>
  <div class="in">
    <span>Pensieve · made by <a href="https://tedhaley.ca">Ted Haley</a> · MIT license</span>
    <nav><a href="/">Home</a><a href="/docs/">Docs</a><a href="https://github.com/TedHaley/pensieve">GitHub</a><a href="https://github.com/TedHaley/pensieve/releases">Releases</a><a href="https://github.com/TedHaley/pensieve/issues/new/choose">Feedback</a></nav>
  </div>
</footer>
<script>
// on a phone the docs menu starts closed, so the page starts with its own text
if (matchMedia('(max-width: 860px)').matches) document.getElementById('docnav').removeAttribute('open');
</script>
<script data-goatcounter="https://pensieve.goatcounter.com/count" async src="https://gc.zgo.at/count.js"></script>
</body>
</html>
"""


def _j(s):
    import json
    return json.dumps(s, ensure_ascii=False).replace("</", "<\\/")


for p in pages_a.PAGES:
    f = OUT / p["slug"] / "index.html" if p["slug"] else OUT / "index.html"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(render(p))
    print(f.relative_to(OUT.parent), len(p["title"]), len(p["desc"]))
