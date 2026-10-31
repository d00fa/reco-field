from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).parents[1] / "reco_field" / "static"


class DashboardMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []
        self.unlabelled = []
        self.stack = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag in {"input", "select"} and attrs.get("type") != "hidden":
            if "label" not in self.stack and not attrs.get("aria-label"):
                self.unlabelled.append(attrs.get("id"))
        if tag not in {"input", "meta", "link", "use", "path", "circle", "rect", "br"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


def test_dashboard_preserves_unique_control_targets_and_navigation():
    import re

    markup = DashboardMarkup()
    markup.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert not [name for name, count in Counter(markup.ids).items() if count > 1]
    assert not markup.unlabelled
    assert {"#live", "#recordings", "#setup/cameras", "#setup/alignment",
            "#setup/network", "#setup/device"}.issubset(markup.links)
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    targets = set(re.findall(r'\$\("#([a-z][a-z0-9-]*)', script))
    assert targets.issubset(set(markup.ids)), targets - set(markup.ids)
