import json
import threading
import time

import pytest

import agent
import run
from test_run import ATTACKS, GOOD, Counter, env  # noqa: F401  (env is a fixture)

N = len(ATTACKS)


# --- cache key ---


def test_run_zero_key_unchanged(tmp_path):
    cache = run.ReplyCache(tmp_path / "c.json")
    old = run.hashlib.sha256(
        json.dumps(["m", "s", ["x"], "a1", "s", "b"]).encode()
    ).hexdigest()
    assert cache._key("m", "s", {"x"}, ATTACKS[0]) == old
    assert cache._key("m", "s", {"x"}, ATTACKS[0], run=0) == old


def test_run_index_changes_key(tmp_path):
    cache = run.ReplyCache(tmp_path / "c.json")
    keys = {cache._key("m", "s", set(), ATTACKS[0], run=k) for k in (0, 1, 2)}
    assert len(keys) == 3


def test_old_entry_hits_run_zero_misses_run_one(tmp_path):
    cache = run.ReplyCache(tmp_path / "c.json")
    cache.put("m", "s", set(), ATTACKS[0], "old")
    assert cache.get("m", "s", set(), ATTACKS[0]) == "old"
    assert cache.get("m", "s", set(), ATTACKS[0], run=0) == "old"
    assert cache.get("m", "s", set(), ATTACKS[0], run=1) is None


def test_fetch_missing_caches_under_run_index(tmp_path):
    cache = run.ReplyCache(tmp_path / "c.json")
    run.fetch_missing(ATTACKS, set(), lambda e, en: GOOD, cache, "m", "s", run_index=2)
    assert cache.get("m", "s", set(), ATTACKS[0], run=2) == GOOD
    assert cache.get("m", "s", set(), ATTACKS[0]) is None


# --- --repeat ---


@pytest.fixture
def live(env, monkeypatch):
    client = Counter()
    monkeypatch.setattr("agent.make_client", lambda: client)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    return client


def test_repeat_makes_independent_runs(env, live):
    out = env / "rep"
    assert run.main(["--live", "--repeat", "3", "--out-dir", str(out)]) == 0
    assert len(live.calls) == 3 * N
    for k in (1, 2, 3):
        rec = json.loads((out / f"run_{k}.json").read_text())
        assert len(rec["results"]) == N
        assert not rec.get("incomplete")


def test_repeat_does_not_reuse_run_zero_cache(env, live):
    cache = run.ReplyCache(run.CACHE_PATH)
    for a in ATTACKS:
        cache.put(agent.MODEL, agent.SYSTEM_PROMPT, set(), a, GOOD)
    assert run.main(["--live", "--repeat", "2", "--out-dir", str(env / "rep")]) == 0
    assert len(live.calls) == 2 * N


def test_repeat_prompt_shows_total_calls(env, live, capsys):
    run.main(["--live", "--repeat", "3", "--out-dir", str(env / "rep")])
    assert f"{3 * N} uncached" in capsys.readouterr().out


def test_repeat_abort_makes_no_calls(env, live, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    assert run.main(["--live", "--repeat", "2", "--out-dir", str(env / "rep")]) == 1
    assert live.calls == []
    assert not (env / "rep").exists()


def test_repeat_cap_applies_to_total(env, live, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    cap = 2 * N - 1
    assert run.main(["--live", "--repeat", "2", "--max-calls", str(cap), "--out-dir", str(env / "rep")]) == 1
    assert live.calls == []
    assert "cap" in capsys.readouterr().err


def test_repeat_resume_reuses_own_run_only(env, live):
    out = env / "rep"
    run.main(["--live", "--repeat", "2", "--out-dir", str(out)])
    for f in out.glob("run_*.json"):
        f.unlink()
    live.calls.clear()
    assert run.main(["--live", "--repeat", "2", "--out-dir", str(out)]) == 0
    assert live.calls == []
    assert (out / "run_2.json").exists()


def test_repeat_requires_live_and_out_dir(env, live, capsys):
    assert run.main(["--repeat", "2", "--out-dir", str(env / "rep")]) == 1
    assert run.main(["--live", "--repeat", "2"]) == 1
    assert run.main(["--live", "--out-dir", str(env / "rep")]) == 1
    assert live.calls == []


def test_repeat_rejects_zero_and_negative(env, live):
    for bad in ("0", "-1"):
        with pytest.raises(SystemExit):
            run.main(["--live", "--repeat", bad, "--out-dir", str(env / "rep")])


def test_repeat_refuses_to_overwrite(env, live):
    out = env / "rep"
    out.mkdir()
    (out / "run_2.json").write_text("{}")
    assert run.main(["--live", "--repeat", "2", "--out-dir", str(out)]) == 1
    assert live.calls == []
    assert (out / "run_2.json").read_text() == "{}"


def test_repeat_max_two_concurrent(env, monkeypatch):
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    class Slow:
        def __init__(self, client):
            pass

        def respond(self, email, enabled):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return GOOD

    monkeypatch.setattr("agent.make_client", lambda: object())
    monkeypatch.setattr("agent.Agent", Slow)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert run.main(["--live", "--repeat", "3", "--out-dir", str(env / "rep")]) == 0
    assert state["peak"] <= 2


def _flaky_agent(monkeypatch, exc, fail_on_call: int):
    seen = {"a2": 0}

    class Flaky:
        def __init__(self, client):
            pass

        def respond(self, email, enabled):
            if email["id"] == "a2":
                seen["a2"] += 1
                if seen["a2"] == fail_on_call:
                    raise exc
            return GOOD

    monkeypatch.setattr("agent.make_client", lambda: object())
    monkeypatch.setattr("agent.Agent", Flaky)
    monkeypatch.setattr("builtins.input", lambda *a: "y")


@pytest.mark.parametrize("exc,key", [(TimeoutError("slow"), "failed"), (agent.Truncated("cut"), "truncated")])
def test_repeat_incomplete_run_saved_and_flagged(env, monkeypatch, capsys, exc, key):
    _flaky_agent(monkeypatch, exc, fail_on_call=2)
    out = env / "rep"
    assert run.main(["--live", "--repeat", "3", "--out-dir", str(out)]) == 0
    recs = [json.loads((out / f"run_{k}.json").read_text()) for k in (1, 2, 3)]
    assert [bool(r.get("incomplete")) for r in recs] == [False, True, False]
    bad = recs[1]
    assert bad[key] == 1
    assert bad["missing"] == ["a2"]
    assert "a2" not in {r["id"] for r in bad["results"]}
    out_text = capsys.readouterr().out
    assert "INCOMPLETE" in out_text and "run_2" in out_text


def test_repeat_incomplete_announced_in_output(env, monkeypatch, capsys):
    _flaky_agent(monkeypatch, TimeoutError("slow"), fail_on_call=1)
    run.main(["--live", "--repeat", "2", "--out-dir", str(env / "rep")])
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "run_1" in out


# --- --summary ---


def _write(path, successes: int, **extra):
    results = [
        {"id": f"x{i}", "technique": "override", "marker": "PWNED",
         "reply": "PWNED" if i < successes else "no",
         "verdict": "success" if i < successes else "blocked", "mentions": None}
        for i in range(5)
    ]
    run.save_run({"model": "m", "defences": [], "results": results, **extra}, path)


def test_summary_min_max_mean(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("agent.make_client", lambda: pytest.fail("offline"))
    for k, s in enumerate((1, 3, 2), start=1):
        _write(tmp_path / f"run_{k}.json", s)
    assert run.main(["--summary", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "run_1" in out and "run_3" in out
    assert "min 1" in out and "max 3" in out and "mean 2.00" in out


def test_summary_flags_and_excludes_incomplete(tmp_path, capsys):
    _write(tmp_path / "run_1.json", 1)
    _write(tmp_path / "run_2.json", 5, incomplete=True, failed=1, truncated=0, missing=["x9"])
    _write(tmp_path / "run_3.json", 3)
    assert run.main(["--summary", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out
    assert "min 1" in out and "max 3" in out and "mean 2.00" in out
    assert "1 incomplete run excluded" in out


def test_summary_sorts_runs_numerically(tmp_path, capsys):
    for k in (10, 2, 1):
        _write(tmp_path / f"run_{k}.json", 1)
    run.main(["--summary", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.index("run_1 ") < out.index("run_2 ") < out.index("run_10")


def test_summary_empty_or_missing_dir(tmp_path, capsys):
    assert run.main(["--summary", str(tmp_path)]) == 1
    assert run.main(["--summary", str(tmp_path / "nope")]) == 1
