"""Palette and CSS for the Tripwire console. System fonts only, no external resources."""

PALETTE: dict[str, str] = {
    "bg": "#0b0d10",
    "panel": "#12161b",
    "border": "#232a33",
    "text": "#e6edf3",
    "muted": "#9aa7b4",
    "accent": "#00e5ff",
    "danger": "#ff4d5e",
    "mention": "#ffb020",
    "noreply": "#8a96a5",
}

# Heatmap outcome -> colour. Order is the legend order.
STATE_COLOURS: dict[str, str] = {
    "strict success": PALETTE["danger"],
    "mentions only": PALETTE["mention"],
    "blocked": PALETTE["accent"],
    "no reply": PALETTE["noreply"],
}

# Heatmap outcome -> emoji square for button labels, where CSS colour is not possible. Approximate colours only.
STATE_EMOJI: dict[str, str] = {
    "strict success": "🟥",
    "mentions only": "🟧",
    "blocked": "🟦",
    "no reply": "⬜",
}

FONT_UI ='system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
FONT_MONO = 'ui-monospace, "Cascadia Mono", "SF Mono", Consolas, monospace'

# Vega-Lite config so charts match the page.
VEGA_CONFIG: dict = {
    "background": None,
    "view": {"stroke": None},
    "axis": {
        "labelColor": PALETTE["muted"],
        "titleColor": PALETTE["muted"],
        "domainColor": PALETTE["border"],
        "gridColor": PALETTE["border"],
        "tickColor": PALETTE["border"],
        "labelFont": FONT_MONO,
        "titleFont": FONT_UI,
    },
    "legend": {"labelColor": PALETTE["text"], "titleColor": PALETTE["muted"], "labelFont": FONT_MONO},
}

CSS = """
:root {
  --bg: #0b0d10; --panel: #12161b; --border: #232a33; --text: #e6edf3; --muted: #9aa7b4;
  --accent: #00e5ff; --danger: #ff4d5e; --mention: #ffb020; --noreply: #8a96a5;
  --font-ui: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-mono: ui-monospace, "Cascadia Mono", "SF Mono", Consolas, monospace;
}
.stApp { background: var(--bg); color: var(--text); font-family: var(--font-ui); }
h1, h2, h3 { letter-spacing: -0.01em; }
code, pre, .stText, [data-testid="stText"], [data-testid="stDataFrame"] { font-family: var(--font-mono); }
.hero { border: 1px solid var(--border); border-left: 3px solid var(--accent); background: var(--panel);
  padding: 1.4rem 1.6rem; border-radius: 6px; margin-bottom: 1rem; }
.hero h2 { margin: 0 0 .4rem 0; font-size: 1.6rem; }
.hero p { margin: 0; color: var(--muted); }
.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: .8rem; margin: 1rem 0 .4rem; }
.tile { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 1rem; }
.tile .label { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; }
.tile .num { font-family: var(--font-mono); font-size: 2.4rem; font-weight: 700; display: block; }
.tile.t-bad .num { color: var(--danger); }
.tile.t-good .num { color: var(--accent); }
@property --num { syntax: "<integer>"; initial-value: 0; inherits: false; }
.count { counter-reset: num var(--num); --num: var(--n); animation: countup 1.2s ease-out; }
.count::after { content: counter(num); }
@keyframes countup { from { --num: 0; } }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.steps { display: block; width: 100%; height: auto; margin-top: .6rem; }
.steps text { fill: var(--text); font-family: var(--font-ui); font-size: 13px; }
.steps .sub { fill: var(--muted); font-size: 11px; }
.steps rect { fill: var(--panel); stroke: var(--border); }
.steps .arrow { stroke: var(--accent); fill: none; stroke-width: 1.5; }
[class*="st-key-card-"] button { width: 100%; text-align: left; background: var(--panel); border: 1px solid var(--border);
  font-family: var(--font-mono); transition: border-color .2s ease, background .2s ease; }
[class*="st-key-card-"] button:hover { border-color: var(--accent); }
.st-key-card_body { border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 6px;
  padding: 1rem; margin: .2rem 0 .8rem; overflow: hidden; animation: cardopen .3s ease-out;
  transition: max-height .3s ease, opacity .3s ease; }
@keyframes cardopen { from { max-height: 0; opacity: 0; } to { max-height: 4000px; opacity: 1; } }
.st-key-slide { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 1.2rem 1.6rem;
  animation: slidein .25s ease-out; }
@keyframes slidein { from { opacity: 0; transform: translateX(8px); } to { opacity: 1; transform: none; } }
@media (max-width: 800px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
@media (prefers-reduced-motion: reduce) {
  .count, .st-key-card_body, .st-key-slide { animation: none; transition: none; }
  [class*="st-key-card-"] button { transition: none; }
}
"""
