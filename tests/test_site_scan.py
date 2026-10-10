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


NUMBER_WORDS = (
    "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    "sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|hundred|most|half|majority"
)


def _visible_text(html: str) -> str:
    """Text a reader sees in index.html: no script, style, svg or tags."""
    html = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"<[^>]*>", " ", html)


def test_index_html_has_no_hardcoded_count_claims():
    html = _text("index.html")
    meta = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    for label, text in (("visible text", _visible_text(html)), ("meta description", meta), ("title", re.search(r"<title>(.*?)</title>", html).group(1))):
        hits = re.findall(rf"\d+|\b(?:{NUMBER_WORDS})\b", text, flags=re.I)
        assert not hits, f"index.html {label} has count claims that are not generated: {hits}"


def test_count_scan_catches_violations():
    assert re.findall(rf"\d+|\b(?:{NUMBER_WORDS})\b", _visible_text("<p>20 attacks</p>"), flags=re.I)
    assert re.findall(rf"\d+|\b(?:{NUMBER_WORDS})\b", _visible_text("<h2>Most attacks worked</h2>"), flags=re.I)
    assert not re.findall(rf"\d+|\b(?:{NUMBER_WORDS})\b", _visible_text('<svg><text>7</text></svg><p>Replay</p>'), flags=re.I)


def test_headline_is_filled_from_data_with_textcontent():
    assert re.search(r'<h2 id="h-head"></h2>', _text("index.html"))
    js = _text("app.js")
    assert '$("h-head").textContent' in js
    parts = (
        "With no defences, ",
        " scored attacks succeeded under the strict rule. With all ",
        'last.defences.length === 5 ? "five " : ""',
        "defences, ",
        " did.",
    )
    for part in parts:
        assert part in js
    assert "sf.success" in js and "sl.success" in js


def test_pipeline_packet_is_stopped_for_reduced_motion():
    css = _text("style.css")
    block = css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".packet { display: none; }" in block
    js = _text("app.js")
    assert 'querySelector(".packet animateMotion")' in js and "removeChild(anim)" in js
