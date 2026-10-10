import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import redteam
from run import load_attacks

LIB = Path(__file__).parent.parent / "attacks" / "library.json"
TECHNIQUES = {"override", "fake_system", "hidden_text", "roleplay", "exfil_forward", "control"}


def fake_client(content: str):
    create = lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_library_covers_all_techniques():
    assert {a["technique"] for a in load_attacks([LIB])} == TECHNIQUES


def test_library_ids_unique_and_fields_present():
    attacks = load_attacks([LIB])
    assert len({a["id"] for a in attacks}) == len(attacks)
    for a in attacks:
        assert {"id", "technique", "subject", "body", "marker"} <= set(a)


def test_library_has_several_per_technique():
    attacks = load_attacks([LIB])
    for t in TECHNIQUES:
        assert sum(a["technique"] == t for a in attacks) >= 3, t


def test_controls_have_no_marker():
    assert all(a["marker"] is None for a in load_attacks([LIB]) if a["technique"] == "control")


def test_hidden_text_attacks_actually_hide_something():
    hidden = [a for a in load_attacks([LIB]) if a["technique"] == "hidden_text"]
    assert any("<!--" in a["body"] for a in hidden)
    assert any("​" in a["body"] for a in hidden)


def test_parse_variants_from_json_list():
    content = json.dumps([{"subject": "s", "body": "b"}, {"subject": "s2", "body": "b2"}])
    assert len(redteam.parse_variants(content)) == 2


def test_parse_variants_tolerates_fence_and_think():
    content = "<think>x</think>```json\n" + json.dumps([{"subject": "s", "body": "b"}]) + "\n```"
    assert len(redteam.parse_variants(content)) == 1


def test_parse_variants_drops_malformed():
    content = json.dumps([{"subject": "s"}, "str", {"subject": "s", "body": "b"}])
    assert len(redteam.parse_variants(content)) == 1


def test_parse_variants_garbage_gives_empty():
    assert redteam.parse_variants("nope") == []


def test_generate_assigns_ids_techniques_and_markers():
    seeds = [a for a in load_attacks([LIB]) if a["technique"] != "control"]
    client = fake_client(json.dumps([{"subject": "s", "body": "b"}]))
    out = redteam.generate(client, seeds, per_technique=1, model="m")
    assert {a["technique"] for a in out} == TECHNIQUES - {"control"}
    assert all(a["id"].startswith("gen-") and a["marker"] for a in out)
    assert len({a["id"] for a in out}) == len(out)


def test_generate_caps_per_technique():
    seeds = [a for a in load_attacks([LIB]) if a["technique"] == "override"]
    client = fake_client(json.dumps([{"subject": "s", "body": str(i)} for i in range(5)]))
    assert len(redteam.generate(client, seeds, per_technique=2, model="m")) == 2


def test_main_does_not_overwrite_without_force(tmp_path, monkeypatch, capsys):
    out = tmp_path / "generated.json"
    out.write_text("[]")
    monkeypatch.setattr(redteam, "make_client", lambda: pytest.fail("must not call API"))
    assert redteam.main(["--out", str(out)]) == 0
    assert "exists" in capsys.readouterr().out
