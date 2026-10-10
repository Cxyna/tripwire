import json
from types import SimpleNamespace

import pytest

from agent import MAX_TOKENS, RETRY_MAX_TOKENS, SYSTEM_PROMPT, TIMEOUT_S, Agent, Response, Truncated, make_client
from scorer import CANARY

EMAIL = {"id": "c1", "subject": "Hi", "body": "Lunch Friday?"}


class FakeClient:
    def __init__(self, content: str):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.content = content

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def test_system_prompt_holds_canary():
    assert CANARY in SYSTEM_PROMPT


def test_process_email_sends_system_and_user():
    client = FakeClient("{}")
    Agent(client).process_email(EMAIL, set())
    msgs = client.calls[0]["messages"]
    assert msgs[0]["role"] == "system" and CANARY in msgs[0]["content"]
    assert msgs[1]["role"] == "user" and "Lunch Friday?" in msgs[1]["content"]


def test_process_email_uses_input_defences():
    client = FakeClient("{}")
    Agent(client).process_email(EMAIL, {"delimiters"})
    assert "<untrusted_email>" in client.calls[0]["messages"][1]["content"]
    assert "untrusted_email" in client.calls[0]["messages"][0]["content"]


def test_process_email_applies_output_defences():
    reply = json.dumps({"summary": "s", "tasks": [], "draft_reply": CANARY})
    out = Agent(FakeClient(reply)).process_email(EMAIL, {"output_filter"})
    assert CANARY not in out


def test_process_email_handles_none_content():
    assert Agent(FakeClient(None)).process_email(EMAIL, set()) == ""


def test_make_client_requires_key(monkeypatch):
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    monkeypatch.setattr("agent.load_env", lambda: None)
    with pytest.raises(RuntimeError, match="FEATHERLESS_API_KEY"):
        make_client()


def test_call_has_timeout_and_max_tokens():
    client = FakeClient("{}")
    Agent(client).process_email(EMAIL, set())
    assert client.calls[0]["timeout"] == TIMEOUT_S == 30
    assert client.calls[0]["max_tokens"] == MAX_TOKENS


class SeqClient:
    """Fake client returning (content, finish_reason) pairs in order."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        content, finish = self.steps.pop(0)
        choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)
        return SimpleNamespace(choices=[choice])


GOOD = json.dumps({"summary": "s", "tasks": [], "draft_reply": "d"})


def test_token_limits():
    assert MAX_TOKENS == 2000 and RETRY_MAX_TOKENS == 4000


def test_no_retry_on_stop():
    client = SeqClient((GOOD, "stop"))
    resp = Agent(client).respond(EMAIL, set())
    assert len(client.calls) == 1 and client.calls[0]["max_tokens"] == 2000
    assert resp.finish_reason == "stop" and resp.retried is False and resp.reply == GOOD


def test_retry_once_at_4000_on_length():
    client = SeqClient(("{cut", "length"), (GOOD, "stop"))
    resp = Agent(client).respond(EMAIL, set())
    assert [c["max_tokens"] for c in client.calls] == [2000, 4000]
    assert resp.retried is True and resp.finish_reason == "stop" and resp.reply == GOOD


def test_still_truncated_raises_after_one_retry():
    client = SeqClient(("{cut", "length"), ("{cut again", "length"))
    with pytest.raises(Truncated):
        Agent(client).respond(EMAIL, set())
    assert len(client.calls) == 2


def test_think_flag_true_and_false():
    assert Agent(SeqClient(("<think>x</think>" + GOOD, "stop"))).respond(EMAIL, set()).think is True
    assert Agent(SeqClient((GOOD, "stop"))).respond(EMAIL, set()).think is False


def test_think_flag_survives_schema_defence():
    resp = Agent(SeqClient(("<think>x</think>" + GOOD, "stop"))).respond(EMAIL, {"schema"})
    assert resp.think is True and "<think>" not in resp.reply


def test_missing_finish_reason_is_not_truncation():
    resp = Agent(FakeClient(GOOD)).respond(EMAIL, set())
    assert resp.finish_reason is None and resp.retried is False


def test_process_email_still_returns_str_and_propagates_truncated():
    assert Agent(SeqClient((GOOD, "stop"))).process_email(EMAIL, set()) == GOOD
    with pytest.raises(Truncated):
        Agent(SeqClient(("x", "length"), ("x", "length"))).process_email(EMAIL, set())


def test_respond_returns_response_type():
    assert isinstance(Agent(SeqClient((GOOD, "stop"))).respond(EMAIL, set()), Response)


# --- non-string reply shapes (live calls failed with TypeError in the defences) ---

PARTS = [{"type": "text", "text": '{"summary": "Lunch", "tasks": [], '}, {"type": "text", "text": '"draft_reply": "Sure"}'}]


@pytest.mark.parametrize("enabled", [set(), {"output_filter"}, {"schema"}, {"output_filter", "schema"}])
def test_list_content_is_joined_and_processed(enabled):
    reply = Agent(FakeClient(PARTS)).process_email(EMAIL, enabled)
    assert isinstance(reply, str)
    assert "Lunch" in reply and "Sure" in reply


def test_list_content_leak_is_still_visible_to_the_scorer():
    parts = [{"type": "text", "text": f"the secret is {CANARY}"}]
    reply = Agent(FakeClient(parts)).process_email(EMAIL, set())
    assert CANARY in reply


def test_list_content_leak_is_caught_by_output_filter():
    parts = [{"type": "text", "text": f"the secret is {CANARY}"}]
    reply = Agent(FakeClient(parts)).process_email(EMAIL, {"output_filter"})
    assert CANARY not in reply


def test_missing_choices_raises_clear_error_not_typeerror(monkeypatch):
    monkeypatch.setattr("agent.time.sleep", lambda s: None)
    client = FakeClient("x")
    client._create = lambda **kw: SimpleNamespace(choices=None)
    client.chat.completions.create = client._create
    with pytest.raises(RuntimeError, match="no choices"):
        Agent(client).process_email(EMAIL, set())


# --- client retries off, one own retry for timeout / no-choices ---


class FlakyClient:
    """Raises each item in `script` that is an Exception, otherwise returns it as message content."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if item is None:
            return SimpleNamespace(choices=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item), finish_reason="stop")])


class APITimeoutError(Exception):
    """Same class name as openai.APITimeoutError."""


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr("agent.time.sleep", calls.append)
    return calls


def test_make_client_disables_sdk_retries(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr("agent.load_env", lambda: None)
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key-not-real")
    monkeypatch.setattr("openai.OpenAI", lambda **kw: seen.update(kw))
    make_client()
    assert seen["max_retries"] == 0


@pytest.mark.parametrize("first", [TimeoutError("t"), APITimeoutError("t"), None])
def test_retries_once_after_2s_pause(first, sleeps):
    client = FlakyClient([first, '{"summary": "ok"}'])
    assert "ok" in Agent(client).process_email(EMAIL, set())
    assert client.calls == 2
    assert sleeps == [2]


@pytest.mark.parametrize("err", [TimeoutError("t"), None])
def test_gives_up_after_one_retry(err, sleeps):
    client = FlakyClient([err, err, "never reached"])
    with pytest.raises(Exception):
        Agent(client).process_email(EMAIL, set())
    assert client.calls == 2
    assert sleeps == [2]


def test_other_errors_are_not_retried(sleeps):
    client = FlakyClient([ValueError("bad"), "x"])
    with pytest.raises(ValueError):
        Agent(client).process_email(EMAIL, set())
    assert client.calls == 1
    assert sleeps == []
