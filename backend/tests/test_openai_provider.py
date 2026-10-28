"""The OpenAI provider, against a fake Responses API."""
import os
import tempfile
from types import SimpleNamespace as NS

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.llm import get_llm, openai_provider  # noqa: E402
from app.llm.base import Tool  # noqa: E402
from app.models import LLMUsage  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_client():
    get_llm.cache_clear()  # the app keeps one client; each test switches provider settings
    yield
    get_llm.cache_clear()


def usage(inp=100, cached=0, out=20):
    return NS(input_tokens=inp, input_tokens_details=NS(cached_tokens=cached), output_tokens=out)


class FakeResponses:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


@pytest.fixture
def fake(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "openai")
    monkeypatch.setattr(s, "openai_api_key", "sk-test")

    def install(replies):
        f = FakeResponses(replies)
        monkeypatch.setattr(openai_provider, "OpenAI", lambda api_key: NS(responses=f))
        return f
    return install


def test_tool_loop_sends_reasoning_and_results_back(fake):
    init_db()
    first = NS(status="completed", incomplete_details=None, usage=usage(500, 400, 30), output=[
        NS(type="reasoning", id="rs_1", summary=[], encrypted_content="ENC"),
        NS(type="function_call", call_id="call_1", name="find_matches", arguments='{"q": "fintech"}'),
    ])
    second = NS(status="completed", incomplete_details=None, usage=usage(), output=[
        NS(type="message", content=[NS(type="output_text", text="Here are 3 startups.")]),
    ])
    f = fake([first, second])
    llm = get_llm()
    assert isinstance(llm, openai_provider.OpenAILLM)
    tools = [Tool("find_matches", "Find startups", {"type": "object", "properties": {"q": {"type": "string"}}})]
    msgs = [{"role": "user", "content": "find me fintech startups"}]

    r1 = llm.chat("You are FounderReach.", msgs, tools, task="chat", cache_history=True)
    assert [(t.id, t.name, t.input) for t in r1.tool_calls] == [("call_1", "find_matches", {"q": "fintech"})]
    call = f.calls[0]
    assert call["model"] == get_settings().openai_model and call["instructions"] == "You are FounderReach."
    assert call["store"] is False and call["include"] == ["reasoning.encrypted_content"]
    assert call["tools"][0] == {"type": "function", "name": "find_matches", "description": "Find startups",
                                "parameters": tools[0].parameters}
    assert call["reasoning"] == {"effort": get_settings().effort_chat}

    # the agent's next turn: assistant (with raw items) + tool result
    msgs += [{"role": "assistant", "content": r1.text, "tool_calls": [t.as_dict() for t in r1.tool_calls], "raw": r1.raw},
             {"role": "tool", "tool_call_id": "call_1", "name": "find_matches", "content": '{"ok": true}'}]
    r2 = llm.chat("You are FounderReach.", msgs, tools, task="chat")
    assert r2.text == "Here are 3 startups." and not r2.tool_calls
    assert f.calls[1]["input"] == [
        {"role": "user", "content": "find me fintech startups"},
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC"},
        {"type": "function_call", "call_id": "call_1", "name": "find_matches", "arguments": '{"q": "fintech"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'},
    ]
    with SessionLocal() as db:
        row = db.query(LLMUsage).filter_by(model=get_settings().openai_model).order_by(LLMUsage.id).first()
    assert (row.input_tokens, row.cache_read_tokens, row.output_tokens) == (100, 400, 30)  # cached split out


def test_drafts_and_extraction_use_the_small_model(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "effort_draft", "")
    reply = NS(status="completed", incomplete_details=None, usage=usage(), output=[
        NS(type="message", content=[NS(type="output_text", text='```json\n{"subject": "Hi", "body": "Hello"}\n```')])])
    f = fake([reply, reply])
    llm = get_llm()
    assert llm.complete_json("Write an email.", "context", task="draft_email") == {"subject": "Hi", "body": "Hello"}
    assert f.calls[0]["model"] == get_settings().openai_model_draft and "reasoning" not in f.calls[0]
    llm.complete_json("Extract.", "text", task="extract_startups")
    assert f.calls[1]["model"] == get_settings().openai_model_fast
    assert f.calls[1]["reasoning"] == {"effort": get_settings().effort_extract}


def test_history_from_another_provider_is_rebuilt():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Drafting.", "tool_calls": [{"id": "t1", "name": "draft_email", "input": {"match_id": 3}}]},
            {"role": "tool", "tool_call_id": "t1", "name": "draft_email", "content": "{}"}]
    assert openai_provider._to_openai(msgs) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Drafting."},
        {"type": "function_call", "call_id": "t1", "name": "draft_email", "arguments": '{"match_id": 3}'},
        {"type": "function_call_output", "call_id": "t1", "output": "{}"},
    ]


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    monkeypatch.setattr(get_settings(), "openai_api_key", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_llm()
