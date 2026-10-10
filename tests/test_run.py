import json
from types import SimpleNamespace

import pytest

import run
from scorer import CANARY

ATTACKS = [
    {"id": "a1", "technique": "override", "subject": "s", "body": "b", "marker": "PWNED"},
    {"id": "a2", "technique": "override", "subject": "s", "body": "b", "marker": "PWNED"},
    {"id": "a3", "technique": "roleplay", "subject": "s", "body": "b", "marker": None},
    {"id": "c1", "technique": "control", "subject": "s", "body": "b", "marker": None},
]
GOOD = json.dumps({"summary": "ok", "tasks": [], "draft_reply": "hi"})


def fake_call(email, enabled):
    if email["id"] == "a1":
        return json.dumps({"summary": "x", "tasks": [], "draft_reply": "PWNED"})
    if email["id"] == "a3":
        return json.dumps({"summary": "x", "tasks": [], "draft_reply": CANARY})
    return GOOD


def test_run_set_scores_each_email():
    rec = run.run_set(ATTACKS, set(), fake_call)
    verdicts = {r["id"]: r["verdict"] for r in rec["results"]}
    assert verdicts == {"a1": "success", "a2": "blocked", "a3": "success", "c1": "pass"}


def test_run_set_records_defences_and_markers():
    rec = run.run_set(ATTACKS, {"schema", "delimiters"}, fake_call)
    assert rec["defences"] == ["delimiters", "schema"]
    assert rec["results"][0]["marker"] == "PWNED"


def test_control_failure_verdict():
    rec = run.run_set([ATTACKS[3]], set(), lambda e, d: "garbage")
    assert rec["results"][0]["verdict"] == "fail"


def test_summarize_rates():
    s = run.summarize(run.run_set(ATTACKS, set(), fake_call))
    ov = s["techniques"]["override"]
    assert (ov["n"], ov["success"], ov["rate"], ov["no_reply"]) == (2, 1, 0.5, 0)
    assert ov["mentions"] >= ov["success"]
    assert s["techniques"]["roleplay"]["rate"] == 1.0
    assert s["control"] == {"n": 1, "passed": 1, "rate": 1.0}
    o = s["overall"]
    assert (o["n"], o["success"], o["no_reply"]) == (3, 2, 0)
    assert o["rate"] == pytest.approx(2 / 3)


def test_summarize_without_controls():
    s = run.summarize(run.run_set(ATTACKS[:1], set(), fake_call))
    assert s["control"] == {"n": 0, "passed": 0, "rate": None}


def test_compare_deltas():
    a = run.summarize(run.run_set(ATTACKS, set(), fake_call))
    b = run.summarize(run.run_set(ATTACKS, set(), lambda e, d: GOOD))
    d = run.compare(a, b)
    assert d["override"] == pytest.approx(-0.5)
    assert d["roleplay"] == pytest.approx(-1.0)
    assert d["control"] == pytest.approx(0.0)


def test_format_table_mentions_techniques():
    table = run.format_table(run.summarize(run.run_set(ATTACKS, set(), fake_call)))
    assert "override" in table and "roleplay" in table and "control" in table and "50%" in table


def test_save_load_roundtrip(tmp_path):
    rec = run.run_set(ATTACKS, set(), fake_call)
    path = tmp_path / "r.json"
    run.save_run(rec, path)
    assert run.load_run(path) == rec


def test_replay_rescoring_needs_no_api(tmp_path):
    rec = run.run_set(ATTACKS, set(), fake_call)
    path = tmp_path / "r.json"
    run.save_run(rec, path)
    rescored = run.rescore(run.load_run(path))
    assert rescored["results"] == rec["results"]


def test_rescore_picks_up_changed_reply():
    rec = run.run_set(ATTACKS, set(), fake_call)
    rec["results"][0]["reply"] = GOOD
    assert run.rescore(rec)["results"][0]["verdict"] == "blocked"


def test_cache_hit_skips_call(tmp_path):
    calls = []

    def call(email, enabled):
        calls.append(email["id"])
        return GOOD

    cache = run.ReplyCache(tmp_path / "c.json")
    cached = cache.wrap(call, model="m", system="s")
    cached(ATTACKS[0], set())
    cached(ATTACKS[0], set())
    assert calls == ["a1"]
    # persisted across instances
    again = run.ReplyCache(tmp_path / "c.json").wrap(call, model="m", system="s")
    again(ATTACKS[0], set())
    assert calls == ["a1"]


def test_cache_key_varies_with_defences_and_model(tmp_path):
    calls = []
    cache = run.ReplyCache(tmp_path / "c.json")

    def call(email, enabled):
        calls.append(1)
        return GOOD

    cache.wrap(call, "m", "s")(ATTACKS[0], set())
    cache.wrap(call, "m", "s")(ATTACKS[0], {"schema"})
    cache.wrap(call, "m2", "s")(ATTACKS[0], set())
    assert len(calls) == 3


def test_parse_defences():
    assert run.parse_defences("none") == set()
    assert run.parse_defences("") == set()
    assert run.parse_defences("all") == set(run.ALL_DEFENCES)
    assert run.parse_defences("schema,delimiters") == {"schema", "delimiters"}
    with pytest.raises(ValueError):
        run.parse_defences("nope")


def test_load_attacks_merges_files(tmp_path):
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    p1.write_text(json.dumps(ATTACKS[:2]))
    p2.write_text(json.dumps(ATTACKS[2:]))
    assert [a["id"] for a in run.load_attacks([p1, p2])] == ["a1", "a2", "a3", "c1"]


def test_load_attacks_skips_missing_optional(tmp_path):
    p1 = tmp_path / "a.json"
    p1.write_text(json.dumps(ATTACKS[:1]))
    assert len(run.load_attacks([p1, tmp_path / "none.json"])) == 1


def test_main_replay_needs_no_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    path = tmp_path / "r.json"
    run.save_run(run.run_set(ATTACKS, set(), fake_call), path)
    assert run.main(["--replay", str(path)]) == 0
    assert "override" in capsys.readouterr().out


def test_main_compare_offline(tmp_path, capsys):
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    run.save_run(run.run_set(ATTACKS, set(), fake_call), pa)
    run.save_run(run.run_set(ATTACKS, set(), lambda e, d: GOOD), pb)
    assert run.main(["--compare", str(pa), str(pb)]) == 0
    assert "override" in capsys.readouterr().out


def test_main_live_without_key_fails_cleanly(env, monkeypatch, capsys):
    def no_key():
        raise RuntimeError("FEATHERLESS_API_KEY is not set")

    monkeypatch.setattr("agent.make_client", no_key)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    assert run.main(["--live"]) == 1
    assert "FEATHERLESS_API_KEY" in capsys.readouterr().err


def test_replay_and_compare_never_build_client(env, tmp_path, monkeypatch):
    monkeypatch.setattr("agent.make_client", lambda: pytest.fail("offline mode must not build a client"))
    path = tmp_path / "r.json"
    run.save_run(run.run_set(ATTACKS, set(), fake_call), path)
    assert run.main(["--replay", str(path)]) == 0
    assert run.main(["--compare", str(path), str(path)]) == 0


# --- cache-only default, --live, cap, concurrency ---


class Counter:
    """Fake OpenAI client that counts calls and records the kwargs it was given."""

    def __init__(self):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=GOOD))])


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib.json"
    lib.write_text(json.dumps(ATTACKS))
    monkeypatch.setattr(run, "ATTACK_FILES", [lib])
    monkeypatch.setattr(run, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path / "results")
    return tmp_path


def prefill(n: int) -> None:
    import agent

    cache = run.ReplyCache(run.CACHE_PATH)
    for a in ATTACKS[:n]:
        cache.put(agent.MODEL, agent.SYSTEM_PROMPT, set(), a, GOOD)


def test_default_never_calls_api(env, monkeypatch, capsys):
    monkeypatch.setattr("agent.make_client", lambda: pytest.fail("default mode must not build a client"))
    assert run.main(["--defences", "none"]) == 0
    assert "uncached" in capsys.readouterr().out


def test_default_uses_cache_and_saves_when_complete(env, monkeypatch, capsys):
    monkeypatch.setattr("agent.make_client", lambda: pytest.fail("no client"))
    prefill(len(ATTACKS))
    assert run.main(["--defences", "none"]) == 0
    out = capsys.readouterr().out
    assert "override" in out and "uncached" not in out
    assert (env / "results" / "none.json").exists()


def test_default_partial_cache_does_not_save(env, monkeypatch, capsys):
    monkeypatch.setattr("agent.make_client", lambda: pytest.fail("no client"))
    prefill(2)
    assert run.main(["--defences", "none"]) == 0
    assert "2 uncached" in capsys.readouterr().out
    assert not (env / "results").exists()


def test_live_abort_makes_no_calls(env, monkeypatch, capsys):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert run.main(["--live"]) == 1
    assert client.calls == []
    assert "4 uncached" in capsys.readouterr().out


def test_live_requires_exactly_y(env, monkeypatch):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "yes please")
    assert run.main(["--live"]) == 1
    assert client.calls == []


def test_live_yes_calls_caches_and_saves(env, monkeypatch, capsys):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert run.main(["--live"]) == 0
    assert len(client.calls) == 4
    assert (env / "results" / "none.json").exists()
    # second live run: everything cached, no prompt, no calls
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    assert run.main(["--live"]) == 0
    assert len(client.calls) == 4


def test_live_counts_only_uncached(env, monkeypatch, capsys):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    prefill(2)
    assert run.main(["--live"]) == 0
    assert len(client.calls) == 2
    assert "2 uncached" in capsys.readouterr().out


def test_live_over_cap_refuses_before_prompt(env, monkeypatch, capsys):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    assert run.main(["--live", "--max-calls", "3"]) == 1
    assert client.calls == []
    assert "cap" in capsys.readouterr().err


def test_default_cap_is_60():
    assert run.MAX_CALLS == 60


def test_live_passes_timeout_and_max_tokens(env, monkeypatch):
    import agent

    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    run.main(["--live"])
    assert all(c["timeout"] == 30 and c["max_tokens"] == agent.MAX_TOKENS for c in client.calls)


def test_fetch_missing_max_two_concurrent(tmp_path):
    import threading
    import time

    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def slow(email, enabled):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return GOOD

    many = [{**ATTACKS[0], "id": f"x{i}"} for i in range(8)]
    cache = run.ReplyCache(tmp_path / "c.json")
    assert run.fetch_missing(many, set(), slow, cache, "m", "s") == (0, 0)
    assert state["peak"] <= 2
    assert all(cache.get("m", "s", set(), a) == GOOD for a in many)


def test_fetch_missing_failed_call_not_cached(tmp_path):
    def flaky(email, enabled):
        if email["id"] == "a2":
            raise TimeoutError("slow")
        return GOOD

    cache = run.ReplyCache(tmp_path / "c.json")
    assert run.fetch_missing(ATTACKS, set(), flaky, cache, "m", "s") == (1, 0)
    assert cache.get("m", "s", set(), ATTACKS[1]) is None
    assert cache.get("m", "s", set(), ATTACKS[0]) == GOOD


def test_live_failed_call_is_skipped_not_scored_as_blocked(env, monkeypatch, capsys):
    monkeypatch.setattr("agent.make_client", lambda: Counter())
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(run, "fetch_missing", lambda *a, **k: (1, 0))
    assert run.main(["--live"]) == 0
    out = capsys.readouterr().out
    assert "failed" in out
    assert not (env / "results").exists()


# --- truncation, --limit, per-call log ---


def test_fetch_missing_counts_truncated_separately(tmp_path):
    from agent import Truncated

    def call(email, enabled):
        if email["id"] == "a1":
            raise Truncated("cut")
        if email["id"] == "a2":
            raise TimeoutError("slow")
        return GOOD

    cache = run.ReplyCache(tmp_path / "c.json")
    assert run.fetch_missing(ATTACKS, set(), call, cache, "m", "s") == (1, 1)
    assert cache.get("m", "s", set(), ATTACKS[0]) is None
    assert cache.get("m", "s", set(), ATTACKS[1]) is None


def test_fetch_missing_logs_response_metadata_without_reply(tmp_path, capsys):
    from agent import Response

    secret_text = "REPLY-TEXT-MUST-NOT-APPEAR"

    def call(email, enabled):
        return Response(reply=secret_text, finish_reason="stop", think=True, retried=False)

    cache = run.ReplyCache(tmp_path / "c.json")
    run.fetch_missing(ATTACKS[:1], set(), call, cache, "m", "s")
    out = capsys.readouterr().out
    assert "a1" in out and "elapsed=" in out and "finish=stop" in out and "think=yes" in out
    assert secret_text not in out
    assert cache.get("m", "s", set(), ATTACKS[0]) == secret_text


def test_fetch_missing_logs_think_no_and_retried(tmp_path, capsys):
    from agent import Response

    cache = run.ReplyCache(tmp_path / "c.json")
    run.fetch_missing(ATTACKS[:1], set(), lambda e, d: Response(GOOD, "stop", False, True), cache, "m", "s")
    out = capsys.readouterr().out
    assert "think=no" in out and "retried=yes" in out


def test_fetch_missing_accepts_plain_str(tmp_path, capsys):
    cache = run.ReplyCache(tmp_path / "c.json")
    run.fetch_missing(ATTACKS[:1], set(), lambda e, d: GOOD, cache, "m", "s")
    assert cache.get("m", "s", set(), ATTACKS[0]) == GOOD
    assert "finish=n/a" in capsys.readouterr().out


def test_limit_caps_calls_and_prompt(env, monkeypatch, capsys):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert run.main(["--live", "--limit", "2"]) == 0
    assert len(client.calls) == 2
    out = capsys.readouterr().out
    assert "2 uncached API calls" in out and "2 more uncached" in out
    assert not (env / "results").exists()


def test_limit_still_requires_confirmation(env, monkeypatch):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert run.main(["--live", "--limit", "1"]) == 1
    assert client.calls == []


def test_limit_larger_than_missing_is_harmless(env, monkeypatch):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert run.main(["--live", "--limit", "99"]) == 0
    assert len(client.calls) == 4


def test_limit_applies_before_cap_check(env, monkeypatch):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert run.main(["--live", "--limit", "2", "--max-calls", "2"]) == 0
    assert len(client.calls) == 2


def test_limit_negative_rejected(env):
    with pytest.raises(SystemExit):
        run.main(["--live", "--limit", "-1"])


def test_limit_ignored_without_live(env, monkeypatch):
    monkeypatch.setattr("agent.make_client", lambda: pytest.fail("no client"))
    assert run.main(["--limit", "1"]) == 0


def test_live_reports_truncated_not_scored(env, monkeypatch, capsys):
    class Cut(Counter):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            choice = SimpleNamespace(message=SimpleNamespace(content="{cut"), finish_reason="length")
            return SimpleNamespace(choices=[choice])

    client = Cut()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert run.main(["--live"]) == 0
    assert "4 truncated" in capsys.readouterr().out
    assert not (env / "results").exists()
    assert run.ReplyCache(run.CACHE_PATH).data == {}
    assert len(client.calls) == 8  # each of the 4 calls retried once


def test_live_output_has_no_key(env, monkeypatch, capsys):
    monkeypatch.setenv("FEATHERLESS_API_KEY", "sk-SECRET-VALUE")
    monkeypatch.setattr("agent.make_client", lambda: Counter())
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    run.main(["--live"])
    captured = capsys.readouterr()
    assert "sk-SECRET-VALUE" not in captured.out + captured.err


# --- failed-call log: class + redacted, truncated message ---

KEY = "fk-SECRET-0123456789abcdef"


def _fail_with(exc, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FEATHERLESS_API_KEY", KEY)
    cache = run.ReplyCache(tmp_path / "c.json")

    def boom(email, enabled):
        raise exc

    run.fetch_missing([{"id": "e1", "subject": "s", "body": "b"}], set(), boom, cache, "m", "sys")
    return capsys.readouterr().out


def test_failed_call_logs_class_and_message(tmp_path, capsys, monkeypatch):
    out = _fail_with(TypeError("'NoneType' object is not subscriptable"), tmp_path, capsys, monkeypatch)
    assert "e1  failed (TypeError: 'NoneType' object is not subscriptable), skipped" in out


def test_failed_call_message_truncated_to_200(tmp_path, capsys, monkeypatch):
    out = _fail_with(ValueError("x " * 500), tmp_path, capsys, monkeypatch)
    detail = out.split("ValueError: ", 1)[1].rsplit("), skipped", 1)[0]
    assert len(detail) <= 200


@pytest.mark.parametrize("message", [
    f"Incorrect API key provided: {KEY}",
    f"401 Bearer {KEY} rejected",
    "headers={'Authorization': 'Bearer abc.def-123', 'x-api-key': 'zzz'} refused",
    "Authorization: Basic dXNlcjpwYXNz",
    "request failed\nAuthorization: Bearer tok123\nnext line",
    "token " + "A" * 40,
])
def test_failed_call_log_never_contains_key_or_headers(message, tmp_path, capsys, monkeypatch):
    out = _fail_with(RuntimeError(message), tmp_path, capsys, monkeypatch)
    for secret in (KEY, "abc.def-123", "zzz", "dXNlcjpwYXNz", "tok123", "A" * 40):
        assert secret not in out
    assert out.count("\n") == 1  # one line per failed call


def test_key_cut_by_truncation_cannot_leak(tmp_path, capsys, monkeypatch):
    out = _fail_with(RuntimeError("p" * 190 + KEY), tmp_path, capsys, monkeypatch)
    assert "fk-SECRET" not in out


def test_no_reply_counted_separately_not_blocked():
    attacks = [dict(ATTACKS[0], id="e1"), dict(ATTACKS[0], id="e2")]
    s = run.summarize(run.run_set(attacks, set(), lambda e, d: "" if e["id"] == "e1" else GOOD))
    t = s["techniques"][attacks[0]["technique"]]
    assert (t["n"], t["no_reply"]) == (1, 1)
    assert s["overall"]["no_reply"] == 1


def test_format_table_has_strict_and_mentions_columns():
    table = run.format_table(run.summarize(run.run_set(ATTACKS, set(), fake_call)))
    header = table.splitlines()[0]
    assert "strict" in header and "mentions" in header and "no reply" in header
