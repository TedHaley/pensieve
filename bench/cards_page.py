"""Write the thread cards page from cards.json and card_titles.json (when present).

  .venv/bin/python bench/cards_page.py     # writes bench/out/graph/cards.html

Each card lists its open sinks first, then its stale and unknown sinks, then its closed sinks under one dropdown. The
dropdown shows at most 20 items. Partners sit on the same line: a branch with its PR and ticket, a PR with its branch
and ticket."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "out" / "graph"


def main():
    cards = json.loads((OUT / "cards.json").read_text())
    titles_path = OUT / "card_titles.json"
    titles = json.loads(titles_path.read_text()) if titles_path.exists() else {}
    blob = json.dumps({"cards": cards, "titles": titles}, ensure_ascii=False).replace("</", "<\\/")
    html = (ROOT / "bench" / "cards_template.html").read_text().replace("__DATA__", blob)
    (OUT / "cards.html").write_text(html)
    print(f"written {OUT / 'cards.html'}: {len(cards)} cards, {len(titles)} titles")


if __name__ == "__main__":
    main()
