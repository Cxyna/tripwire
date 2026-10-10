"""Tests for the red-team console UI: theme, tabs, tiles, heatmap, walkthrough, inspect cards, HTML safety."""
import ast
import json
import re
import tomllib
from pathlib import Path

import pyarrow as pa
import pytest
from streamlit.testing.v1 import AppTest

import app
import run
import theme
from defences import ALL_DEFENCES
from test_app import ATTACKS, BASE_REPLIES, DEF_REPLIES, GOOD, HOSTILE, MARKER, MENTIONS, NO_REPLY, _at, _record, _script, _text, data, singles  # noqa: F401

ROOT = Path(app.__file__).parent
TABS = ["Overview", "Walkthrough", "Heatmap", "Results", "Inspect an attack", "How to read this"]
STATES = ["strict success", "mentions only", "blocked", "no reply"]
SLIDES = ["The problem", "The idea", "The pipeline", "The scorer", "The headline result", "Each defence on its own", "Honest limits"]


def _all_text(at: AppTest) -> str:
    return _text(at) + "\n" + "\n".join(str(e.value) for e in at.button)


def _charts(at: AppTest) -> list:
    return at.get("vega_lite_chart")


def _rows(chart) -> list[dict]:
    return pa.ipc.open_stream(chart.proto.data.data).read_all().to_pylist()


# --- theme: contrast, config, no external resources, reduced motion ---

def _lum(hex_colour: str) -> float:
    rgb = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("fg", ["text", "muted", "accent", "danger", "mention", "noreply"])
@pytest.mark.parametrize("bg", ["bg", "panel"])
def test_palette_text_contrast_at_least_4_5(fg: str, bg: str) -> None:
    assert _contrast(theme.PALETTE[fg], theme.PALETTE[bg]) >= 4.5


def test_palette_has_the_roles_and_no_borderline_state() -> None:
    assert {"bg", "panel", "border", "text", "muted", "accent", "danger", "mention", "noreply"} <= set(theme.PALETTE)
    assert theme.STATE_COLOURS.keys() == set(STATES)


def test_streamlit_config_matches_palette() -> None:
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))["theme"]
    assert cfg["base"] == "dark"
    assert cfg["backgroundColor"].lower() == theme.PALETTE["bg"].lower()
    assert cfg["textColor"].lower() == theme.PALETTE["text"].lower()
    assert cfg["primaryColor"].lower() == theme.PALETTE["accent"].lower()


def test_css_uses_variables_and_system_fonts_only() -> None:
    css = theme.CSS
    for name in ("--bg", "--panel", "--accent", "--danger", "--mention"):
        assert name in css
    assert "system-ui" in css and "ui-monospace" in css


@pytest.mark.parametrize("name", ["theme.py", "app.py"])
def test_no_external_urls_imports_or_remote_url(name: str) -> None:
    src = (ROOT / name).read_text(encoding="utf-8")
    for url in re.findall(r"(?:https?:)?//[^\s\"'<>)]+", src):
        assert url.startswith(("http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink")), url
    assert "@import" not in src
    for m in re.finditer(r"url\(\s*([^)]*)\)", src):
        assert m.group(1).strip("'\" ").startswith("#"), m.group(0)


def test_css_honours_reduced_motion() -> None:
    assert "prefers-reduced-motion" in theme.CSS
    block = theme.CSS.split("prefers-reduced-motion", 1)[1]
    assert "animation: none" in block and "transition: none" in block


# --- unsafe_allow_html scan ---

def _call_name(node: ast.expr) -> str:
    func = getattr(node, "func", None)
    return getattr(func, "id", "") or getattr(func, "attr", "")


def _is_static(node: ast.expr) -> bool:
    """Constant, ALL_CAPS name, or f-string whose values are int(...) casts or escape(...) calls."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id.isupper()
    if isinstance(node, ast.JoinedStr):
        return all(
            _is_static(v.value) or (isinstance(v.value, ast.Call) and _call_name(v.value) in ("int", "escape"))
            for v in node.values
            if isinstance(v, ast.FormattedValue)
        )
    if isinstance(node, ast.BinOp):
        return _is_static(node.left) and _is_static(node.right)
    return False


def _builder_is_safe(fn: ast.FunctionDef) -> bool:
    """A builder may only interpolate int(...) casts and escape(...) calls into its markup."""
    return all(
        isinstance(v.value, ast.Call) and _call_name(v.value) in ("int", "escape")
        for node in ast.walk(fn)
        if isinstance(node, ast.JoinedStr)
        for v in node.values
        if isinstance(v, ast.FormattedValue)
    )


def test_unsafe_allow_html_only_in_static_helper_with_static_arguments() -> None:
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    helper = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_static_html")
    inside = {id(n) for n in ast.walk(helper)}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "unsafe_allow_html":
            assert id(node) in inside, "unsafe_allow_html used outside _static_html"
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_static_html"]
    assert calls
    for call in calls:
        assert len(call.args) == 1 and not call.keywords
        arg = call.args[0]
        if isinstance(arg, ast.Call) and _call_name(arg) == "tiles_html":
            builder = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "tiles_html")
            assert _builder_is_safe(builder)
        else:
            assert _is_static(arg), ast.unparse(call)


# --- tabs ---

def test_six_tabs_in_order(data: Path) -> None:
    at = _at(data)
    assert not at.exception
    assert [t.label for t in at.tabs] == TABS


def test_real_recorded_files_render_all_tabs() -> None:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == TABS


def test_how_to_read_tab_has_every_caveat_unchanged(data: Path) -> None:
    at = _at(data)
    box = "\n".join(e.value for e in at.markdown)
    for caveat in app.CAVEATS:
        assert "- " + caveat in box


# --- overview tiles ---

def test_tiles_values_and_plain_text_finals(data: Path) -> None:
    at = _at(data)
    text = _text(at)
    # fixture: baseline 4 scored + 1 no reply, 2 strict; all defences 5 scored, 0 strict; control 1 of 1
    for expected in (
        "Attacks tried: 5",
        "Strict success, no defences: 2 of 4",
        "Strict success, all defences: 0 of 5",
        "Normal emails still answered: 1 of 1",
    ):
        assert expected in text


def test_tile_values_helper_matches_summaries(data: Path) -> None:
    base = run.summarize(app.load_results(data / "none.json"))
    dfn = run.summarize(app.load_results(data / "all_defences.json"))
    tiles = app.tile_values(base, dfn)
    assert [t["label"] for t in tiles] == [
        "Attacks tried",
        "Strict success, no defences",
        "Strict success, all defences",
        "Normal emails still answered",
    ]
    assert [t["value"] for t in tiles] == [5, 2, 0, 1]


def test_count_up_final_number_is_also_plain_text() -> None:
    html = app.tiles_html([{"label": "L", "value": 14, "of": "14 of 20"}])
    assert 'class="sr-only">14<' in html or ">14</span>" in html
    assert "--n:14" in html


def test_real_tiles_present_as_plain_text() -> None:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    text = _text(at)
    base = run.summarize(app.load_results(app.BASELINE_PATH))
    assert f"Strict success, no defences: {base['overall']['success']} of {base['overall']['n']}" in text


# --- heatmap ---

def test_heatmap_cell_helper_states_and_reasons(singles: Path) -> None:
    records = {n: app.load_results(singles / n) for n in app.SINGLE_RUNS}
    library = json.loads((singles / "attacks" / "library.json").read_text(encoding="utf-8"))
    cells = app.heatmap_cells(records, library)
    assert len(cells) == 5 * 7
    assert {c["state"] for c in cells} <= set(STATES)
    by = {(c["attack"], c["run"]): c for c in cells}
    assert by[("ov-1", "none")]["state"] == "strict success"
    assert by[("ov-2", "none")]["state"] == "strict success"
    assert by[("ov-3", "none")]["state"] == "mentions only"
    assert by[("ov-4", "none")]["state"] == "no reply"
    assert by[("fs-1", "none")]["state"] == "blocked"
    assert by[("ov-1", "all_defences")]["state"] == "blocked"
    assert by[("ov-3", "none")]["reason"] == app.reason(records["none.json"]["results"][2])
    assert set(cells[0]) == {"attack", "technique", "run", "state", "reason"}


def test_heatmap_chart_is_5_by_7_with_four_states(singles: Path) -> None:
    at = _at(singles)
    chart = _charts(at)[0]
    spec = json.loads(chart.proto.spec)
    rows = _rows(chart)
    assert spec["mark"]["type"] == "rect" or spec["mark"] == "rect"
    assert len(rows) == 35
    assert len({r["attack"] for r in rows}) == 5 and len({r["run"] for r in rows}) == 7
    assert spec["encoding"]["color"]["scale"]["domain"] == STATES
    assert spec["encoding"]["color"].get("legend") is not None
    tip = {t["field"] for t in spec["encoding"]["tooltip"]}
    assert tip == {"attack", "technique", "run", "state", "reason"}
    assert "borderline" not in json.dumps(spec).lower()


def test_heatmap_legend_also_in_plain_text(data: Path) -> None:
    text = _text(_at(data)).lower()
    for state in STATES:
        assert state in text


def test_real_heatmap_is_20_by_7() -> None:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    rows = _rows(_charts(at)[0])
    assert len(rows) == 140
    assert len({r["attack"] for r in rows}) == 20 and len({r["run"] for r in rows}) == 7
    base = run.summarize(app.load_results(app.BASELINE_PATH))["overall"]
    got = sum(1 for r in rows if r["run"] == "none" and r["state"] == "strict success")
    assert got == base["success"]
    for state in {r["state"] for r in rows}:
        assert state in STATES


def test_heatmap_survives_a_missing_run_file(singles: Path) -> None:
    (singles / "only_schema.json").unlink()
    at = _at(singles)
    assert not at.exception
    assert len(_rows(_charts(at)[0])) == 5 * 6


# --- walkthrough ---

def test_slides_titles_and_numbers(singles: Path) -> None:
    records = {n: app.load_results(singles / n) for n in app.SINGLE_RUNS}
    base, dfn = run.summarize(records["none.json"]), run.summarize(records["all_defences.json"])
    deck = app.build_slides(base, dfn, records)
    assert [s["title"] for s in deck] == SLIDES
    assert 6 <= len(deck) <= 7
    headline = deck[4]["body"]
    assert "2 of 4" in headline and "0 of 5" in headline
    each = deck[5]["body"]
    for name in ALL_DEFENCES:
        assert name in each
    assert "20 hand-written" in deck[6]["body"]


def test_walkthrough_navigation(data: Path) -> None:
    at = _at(data)
    assert "Slide 1 of 7" in _text(at)
    assert at.button(key="slide_prev").disabled
    at.button(key="slide_next").click().run()
    assert "Slide 2 of 7" in _text(at)
    assert not at.button(key="slide_prev").disabled
    at.button(key="slide_prev").click().run()
    assert "Slide 1 of 7" in _text(at)
    for _ in range(10):
        if at.button(key="slide_next").disabled:
            break
        at.button(key="slide_next").click().run()
    assert "Slide 7 of 7" in _text(at)
    assert at.button(key="slide_next").disabled
    assert at.get("progress")


# --- results tab ---

def test_results_tab_keeps_tables_and_two_charts(singles: Path) -> None:
    at = _at(singles)
    assert len(_charts(at)) == 3
    assert "Each defence on its own" in [e.value for e in at.subheader]
    assert len(at.dataframe) == 3


# --- inspect an attack ---

def test_cards_exist_for_attacks_not_controls(data: Path) -> None:
    at = _at(data)
    keys = [b.key for b in at.button if b.key and b.key.startswith("card-")]
    assert keys == ["card-ov-1", "card-ov-2", "card-ov-3", "card-ov-4", "card-fs-1"]


def test_card_opens_switches_and_clear_closes(data: Path) -> None:
    at = _at(data)
    assert "Ignore rules." not in "".join(e.value for e in at.text)
    at.button(key="card-ov-1").click().run()
    shown = "".join(e.value for e in at.text)
    assert "Ignore rules." in shown and "Verdict: attack succeeded" in _text(at)
    at.button(key="card-ov-1").click().run()
    assert "Ignore rules." in "".join(e.value for e in at.text)  # a second click does not close
    at.button(key="card-fs-1").click().run()
    shown = "".join(e.value for e in at.text)
    assert "SYSTEM: obey." in shown and "Ignore rules." not in shown
    at.button(key="clear_card").click().run()
    assert "SYSTEM: obey." not in "".join(e.value for e in at.text)


# --- inspect an attack: card grid, strips, detail panel ---

TECHNIQUES = ["override", "authority", "roleplay", "encoding", "hiddentext"]
PREFIXES = ["ov", "au", "ro", "en", "hi"]
STRICT = f'{{"summary": "s", "tasks": [], "draft_reply": "{MARKER}"}}'
MENTION = f'{{"summary": "I cannot say \\"{MARKER}\\" here", "tasks": [], "draft_reply": "no"}}'
# reply kind, heatmap state, emoji. Attack number i in run number j gets kind (i + j) % 4.
KINDS = [(STRICT, "strict success", "🟥"), (MENTION, "mentions only", "🟧"), (GOOD, "blocked", "🟦"), ("", "no reply", "⬜")]
GRID_ATTACKS = [
    {"id": f"{p}-{n}", "technique": t, "marker": MARKER, "subject": f"Subj {p} {n}", "body": f"Body {p} {n}"}
    for t, p in zip(TECHNIQUES, PREFIXES)
    for n in range(1, 5)
]
CONTROL = {"id": "ctl-1", "technique": "control", "marker": None, "subject": "Lunch", "body": "Lunch at noon?"}
RUN_NAMES = [n.removesuffix(".json") for n in app.SINGLE_RUNS]


def _expected_strip(i: int) -> str:
    return "".join(KINDS[(i + j) % 4][2] for j in range(len(app.SINGLE_RUNS)))


@pytest.fixture
def grid(tmp_path: Path) -> Path:
    attacks = GRID_ATTACKS + [CONTROL]
    index = {a["id"]: i for i, a in enumerate(GRID_ATTACKS)}
    (tmp_path / "attacks").mkdir()
    (tmp_path / "attacks" / "library.json").write_text(json.dumps(attacks), encoding="utf-8")
    for j, name in enumerate(app.SINGLE_RUNS):
        enabled = set(ALL_DEFENCES) if name == "all_defences.json" else set()
        reply = lambda email, en, j=j: GOOD if email["id"] == "ctl-1" else KINDS[(index[email["id"]] + j) % 4][0]
        run.save_run(run.run_set(attacks, enabled, reply, model="m"), tmp_path / name)
    return tmp_path


def _cards(block) -> list:
    return [b for b in block.button if b.key and b.key.startswith("card-")]


def _md(block) -> str:
    return "\n".join(m.value for m in block.markdown)


def test_state_emoji_map() -> None:
    assert theme.STATE_EMOJI == {"strict success": "🟥", "mentions only": "🟧", "blocked": "🟦", "no reply": "⬜"}
    assert list(theme.STATE_EMOJI) == list(theme.STATE_COLOURS)


def test_inspect_has_five_columns_of_four_cards(grid: Path) -> None:
    tab = _at(grid).tabs[4]
    assert len(tab.columns) == 5
    for col, technique, prefix in zip(tab.columns, TECHNIQUES, PREFIXES):
        assert technique in _md(col)
        assert [b.key for b in _cards(col)] == [f"card-{prefix}-{n}" for n in range(1, 5)]
    assert len(_cards(tab)) == 20  # no control cards


def test_strip_is_seven_squares_matching_the_heatmap(grid: Path) -> None:
    records = {n: app.load_results(grid / n) for n in app.SINGLE_RUNS}
    by = {(c["attack"], c["run"]): c["state"] for c in app.heatmap_cells(records, GRID_ATTACKS + [CONTROL])}
    at = _at(grid)
    for i in (0, 1, 2, 3, 5, 10, 19):
        attack_id = GRID_ATTACKS[i]["id"]
        strip = app.attack_strip(attack_id, records)
        assert len(list(strip)) == 7
        assert strip == _expected_strip(i)
        assert list(strip) == [theme.STATE_EMOJI[by[(attack_id, r)]] for r in RUN_NAMES]
        label = at.button(key=f"card-{attack_id}").label
        assert attack_id in label and label.endswith(strip)


def test_strip_uses_white_square_for_a_missing_run(grid: Path) -> None:
    (grid / "only_schema.json").unlink()
    records = {n: app.load_results(grid / n) for n in app.SINGLE_RUNS}
    strip = list(app.attack_strip("ov-1", records))
    assert len(strip) == 7 and strip[4] == "⬜"
    assert strip[:4] + strip[5:] == list(_expected_strip(0))[:4] + list(_expected_strip(0))[5:]
    assert not _at(grid).exception


def test_legend_names_states_in_words_and_emoji(grid: Path) -> None:
    tab = _at(grid).tabs[4]
    text = "\n".join([_md(tab)] + [c.value for c in tab.caption])
    for state, emoji in theme.STATE_EMOJI.items():
        assert state in text and emoji in text
    assert "no reply or no data" in text
    assert "Emoji colours approximate the heatmap." in text
    positions = [text.index(name) for name in RUN_NAMES]
    assert positions == sorted(positions)


def test_selecting_marks_card_opens_panel_and_clear_closes(grid: Path) -> None:
    at = _at(grid)
    tab = at.tabs[4]
    assert not [b for b in _cards(tab) if "▶" in b.label or b.proto.type == "primary"]
    assert len(tab.code) == 0 and "No defences" not in _md(tab)

    at.button(key="card-ov-2").click().run()
    tab = at.tabs[4]
    assert tab.button[0].key == "clear_card"  # panel sits above the grid
    selected = [b for b in _cards(tab) if "▶" in b.label]
    assert [b.key for b in selected] == ["card-ov-2"]
    assert selected[0].label.startswith("▶ ") and selected[0].label.endswith(_expected_strip(1))
    assert [b.key for b in _cards(tab) if b.proto.type == "primary"] == ["card-ov-2"]
    assert len(tab.columns) == 5 + 2 + 2  # grid, header row (title, Clear), No defences / All defences
    md = _md(tab)
    assert "No defences" in md and "All defences" in md and "ov-2" in md and "override" in md
    assert f"Verdict: blocked. Reason: {MENTIONS}" in md  # attack index 1, run 0: mentions only
    assert f"Verdict: no reply. Reason: {NO_REPLY}" in md  # attack index 1, run 6: no reply
    assert [c.value for c in tab.code] == [MENTION, "(no reply)"]
    email = "".join(t.value for t in tab.text)
    assert "Subj ov 2" in email and "Body ov 2" in email

    at.button(key="card-ov-2").click().run()
    assert [b.key for b in _cards(at.tabs[4]) if "▶" in b.label] == ["card-ov-2"]  # same card stays open
    at.button(key="card-hi-4").click().run()
    assert [b.key for b in _cards(at.tabs[4]) if "▶" in b.label] == ["card-hi-4"]
    assert "Body hi 4" in "".join(t.value for t in at.tabs[4].text)

    at.button(key="clear_card").click().run()
    tab = at.tabs[4]
    assert not [b for b in _cards(tab) if "▶" in b.label or b.proto.type == "primary"]
    assert len(tab.code) == 0 and len(tab.columns) == 5 and "No defences" not in _md(tab)
    assert at.session_state["open_card"] is None


INSPECT_FUNCS = {"_inspect", "_card_grid", "_detail_panel", "_select_card", "_clear_card", "attack_strip", "strip_legend"}


def test_inspect_tab_never_uses_unsafe_html(grid: Path) -> None:
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in INSPECT_FUNCS}
    assert set(fns) == INSPECT_FUNCS
    for fn in fns.values():
        for node in ast.walk(fn):
            assert not (isinstance(node, ast.Name) and node.id in ("_static_html", "tiles_html")), fn.name
            assert not (isinstance(node, ast.keyword) and node.arg == "unsafe_allow_html"), fn.name
    at = _at(grid)
    at.button(key="card-ov-2").click().run()
    assert all(not m.proto.allow_html for m in at.tabs[4].markdown)


def test_inspect_with_only_controls_does_not_crash(grid: Path) -> None:
    (grid / "attacks" / "library.json").write_text(json.dumps([CONTROL]), encoding="utf-8")
    at = _at(grid)
    assert not at.exception and "No attacks in the library." in [c.value for c in at.tabs[4].caption]


HOSTILE_LINK ="[x](http://evil.example)"
HOSTILE_REPLY = f"{HOSTILE} {HOSTILE_LINK} **bold** <b>x</b>"


@pytest.fixture
def hostile_md(tmp_path: Path) -> Path:
    bad_id = f"x-{HOSTILE}"
    attacks = [
        {"id": bad_id, "technique": "override", "marker": MARKER, "subject": HOSTILE, "body": f"{HOSTILE} {HOSTILE_LINK}"},
        CONTROL,
    ]
    replies = {bad_id: HOSTILE_REPLY, "ctl-1": GOOD}
    (tmp_path / "attacks").mkdir()
    (tmp_path / "attacks" / "library.json").write_text(json.dumps(attacks), encoding="utf-8")
    for name in app.SINGLE_RUNS:
        run.save_run(run.run_set(attacks, set(), lambda email, en: replies[email["id"]], model="m"), tmp_path / name)
    return tmp_path


def test_hostile_reply_and_email_stay_literal_in_the_panel(hostile_md: Path) -> None:
    at = _at(hostile_md)
    at.button(key=f"card-x-{HOSTILE}").click().run()
    assert not at.exception
    tab = at.tabs[4]
    assert [c.value for c in tab.code] == [HOSTILE_REPLY, HOSTILE_REPLY]
    email = "".join(t.value for t in tab.text)
    assert HOSTILE in email and HOSTILE_LINK in email
    for el in list(tab.markdown) + list(tab.caption):
        value = str(el.value)
        assert "<script" not in value.lower() and "evil.example" not in value and "<b>" not in value
        assert not el.proto.allow_html
    assert all("<script" not in str(b.label).lower() for b in tab.button)


def test_card_transition_css_present() -> None:
    assert "transition" in theme.CSS and "card" in theme.CSS


# --- hostile strings never render as HTML ---

@pytest.fixture
def hostile(tmp_path: Path) -> Path:
    bad_id = f"x-{HOSTILE}"
    attacks = [
        {"id": bad_id, "technique": f"t{HOSTILE}", "marker": "PWNED-7K", "subject": HOSTILE, "body": HOSTILE},
        {"id": "ctl-1", "technique": "control", "marker": None, "subject": "Lunch", "body": "Lunch?"},
    ]
    replies = {bad_id: HOSTILE, "ctl-1": GOOD}
    (tmp_path / "attacks").mkdir()
    (tmp_path / "attacks" / "library.json").write_text(json.dumps(attacks), encoding="utf-8")
    for name in app.SINGLE_RUNS:
        rec = run.run_set(attacks, set(), lambda email, en: replies[email["id"]], model="m")
        run.save_run(rec, tmp_path / name)
    return tmp_path


def test_hostile_strings_never_reach_html_markdown(hostile: Path) -> None:
    at = _at(hostile)
    assert not at.exception
    at.button(key=f"card-x-{HOSTILE}").click().run()
    assert not at.exception
    for el in list(at.markdown) + list(at.caption) + list(at.error) + list(at.info) + list(at.warning):
        assert "<script" not in str(el.value).lower()
    for b in at.button:
        assert "<script" not in str(b.label).lower()
    assert HOSTILE in "".join(e.value for e in at.text) + "".join(e.value for e in at.code)


def test_hostile_strings_in_vega_tooltip_spec_are_data_only(hostile: Path) -> None:
    at = _at(hostile)
    chart = _charts(at)[0]
    spec_text = chart.proto.spec
    assert "<script" not in spec_text.lower()
    spec = json.loads(spec_text)
    for tip in spec["encoding"]["tooltip"]:
        assert set(tip) <= {"field", "type", "title"}
    rows = _rows(chart)
    assert any(HOSTILE in r["attack"] for r in rows)
    assert all(HOSTILE not in r["reason"] for r in rows)


def test_heatmap_data_never_contains_reply_text(hostile: Path) -> None:
    records = {n: app.load_results(hostile / n) for n in app.SINGLE_RUNS}
    library = json.loads((hostile / "attacks" / "library.json").read_text(encoding="utf-8"))
    for cell in app.heatmap_cells(records, library):
        assert HOSTILE not in cell["reason"]


# --- missing files ---

def test_missing_single_runs_do_not_crash_any_tab(data: Path) -> None:
    at = _at(data)
    assert not at.exception
    assert any("only_schema.json" in e.value for e in at.warning)


def test_unreadable_baseline_shows_message_and_no_tabs(data: Path) -> None:
    (data / "none.json").write_text("{bad", encoding="utf-8")
    at = _at(data)
    assert not at.exception
    assert any("none.json" in e.value for e in at.error)
