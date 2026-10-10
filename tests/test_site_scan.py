import re
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent.parent / "site"
FILES = ["index.html", "style.css", "app.js", "data.js"]
BANNED = ["innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write", "http://", "https://", "@import"]
SVG_NS = "http://www.w3.org/2000/svg"


def _text(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_no_banned_apis_or_external_urls(name):
    text = _text(name).replace(SVG_NS, "")
    hits = [b for b in BANNED if b in text]
    assert not hits, f"{name}: {hits}"


def test_scan_catches_violations():
    assert "innerHTML" in "el.innerHTML = x"
    assert "https://" in '<script src="https://cdn.x/y.js">'


def test_only_local_assets_referenced():
    html = _text("index.html")
    refs = re.findall(r'(?:src|href)="([^"#][^"]*)"', html)
    assert set(refs) <= {"style.css", "data.js", "app.js"}


def test_no_build_step_or_extra_files():
    names = {p.name for p in SITE.iterdir()}
    assert names == set(FILES)


def test_motion_respects_reduced_motion():
    assert "prefers-reduced-motion" in _text("style.css")
    assert "prefers-reduced-motion" in _text("app.js")


def test_text_contrast_tokens_meet_4_5():
    css = _text("style.css")
    root = css.split(":root", 1)[1].split("}", 1)[0]
    tok = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", root))

    def lum(h: str) -> float:
        c = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a: str, b: str) -> float:
        la, lb = sorted((lum(tok[a]), lum(tok[b])), reverse=True)
        return (la + 0.05) / (lb + 0.05)

    for fg in ("text", "muted", "accent", "good", "bad", "warn"):
        for bg in ("bg", "panel"):
            assert ratio(fg, bg) >= 4.5, (fg, bg, ratio(fg, bg))
    for fg in ("on-accent",):
        assert ratio(fg, "accent-bg") >= 4.5
