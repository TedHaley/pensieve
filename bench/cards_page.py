"""Write the thread cards page from cards.json and card_titles.json (when present).

  .venv/bin/python bench/cards_page.py     # writes bench/out/graph/cards.html

Each card shows its pairs in one direction: branch to PR, then PR to ticket. A branch with no PR pairs with its
tickets. A branch with neither stands alone. Open and stale pairs come first. Closed and unknown pairs go under one
dropdown, at most 20 items."""
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
