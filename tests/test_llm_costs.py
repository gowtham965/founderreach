"""Cost levers: per-task model/effort routing, cache markers, usage recording, stable history window."""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.llm import anthropic_provider as ap  # noqa: E402
from app.llm.base import Tool  # noqa: E402
from app.models import ChatMessage, LLMUsage, User  # noqa: E402


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(input_tokens=100, cache_creation_input_tokens=50, cache_read_input_tokens=900, output_tokens=40)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": true}')], usage=usage, stop_reason="end_turn")


@pytest.fixture
def llm(monkeypatch):
    init_db()
    s = get_settings()
    for k, v in {"anthropic_model": "claude-sonnet-5-5", "anthropic_model_fast": "claude-haiku-5-5",
                 "anthropic_model_draft": "claude-haiku-5-5",
                 "effort_chat": "medium", "effort_extract": "low", "effort_draft": ""}.items():
        monkeypatch.setattr(s, k, v)
    client = ap.AnthropicLLM.__new__(ap.AnthropicLLM)
    client.client = SimpleNamespace(messages=FakeMessages())
    client.model = "claude-sonnet-5-5"
    return client


def test_routing():
    s = get_settings()
    assert ap.route("score_matches") == (s.anthropic_model_fast, s.effort_extract)
    assert ap.route("extract_startups")[0] == s.anthropic_model_fast
    assert ap.route("draft_email") == (s.anthropic_model_draft, s.effort_draft)
    assert ap.route("chat") == (s.anthropic_model, s.effort_chat)


def test_extraction_runs_on_fast_model_at_low_effort(llm):
    assert llm.complete_json("Extract startups.", "stories...", task="extract_startups") == {"ok": True}
    kw = llm.client.messages.calls[-1]
    assert kw["model"] == "claude-haiku-5-5" and kw["output_config"] == {"effort": "low"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}  # static instructions cache
    assert "cache_control" not in kw  # one-shot prompt: no history caching surcharge


def test_chat_caches_prefix_and_history(llm):
    tools = [Tool("get_status", "status", {"type": "object", "properties": {}})]
    llm.chat("You are FounderReach.", [{"role": "user", "content": "hi"}], tools, task="chat", cache_history=True)
    kw = llm.client.messages.calls[-1]
    assert kw["model"] == "claude-sonnet-5-5" and kw["output_config"] == {"effort": "medium"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and kw["cache_control"] == {"type": "ephemeral"}
    assert kw["max_tokens"] >= 16000  # backstop, not a tight cap that cuts replies off


def test_drafts_run_on_draft_model_at_default_effort(llm, monkeypatch):
    llm.complete_json("Write the email.", "facts...", task="draft_email")
    kw = llm.client.messages.calls[-1]
    assert kw["model"] == "claude-haiku-5-5" and "output_config" not in kw
    monkeypatch.setattr(get_settings(), "anthropic_model_draft", "claude-sonnet-5-5")  # the one-line way back
    llm.complete_json("Write the email.", "facts...", task="humanize")
    assert llm.client.messages.calls[-1]["model"] == "claude-sonnet-5-5"


def test_usage_recorded_and_reported(llm):
    with SessionLocal() as db:
        db.query(LLMUsage).delete()
        db.commit()
    llm.complete_json("x", "y", task="score_matches")
    with SessionLocal() as db:
        row = db.query(LLMUsage).one()
    assert (row.task, row.model, row.input_tokens, row.cache_write_tokens, row.cache_read_tokens, row.output_tokens) == \
        ("score_matches", "claude-haiku-5-5", 100, 50, 900, 40)
    from app.cost_report import report
    text = report(days=1)
    assert "score_matches" in text and "Total: ₹" in text
    # 100*0.10 + 50*0.125 + 900*0.01 + 40*0.50 = 45.25 per million tokens → $0.00004525
    assert "$0.0000" in text


def test_history_window_moves_in_steps():
    """The prefix sent to the model stays identical for ten turns, then jumps."""
    from app.agent import _history
    init_db()
    with SessionLocal() as db:
        u = User(email=f"hist{os.urandom(3).hex()}@example.com", password_hash="x")
        db.add(u)
        db.commit()

        def add(n):
            for i in range(n):
                db.add(ChatMessage(user_id=u.id, role="user" if i % 2 == 0 else "assistant", content=f"m{db.query(ChatMessage).filter_by(user_id=u.id).count()}"))
                db.commit()

        add(34)
        first = _history(db, u)[0]["content"]
        add(4)  # 38 messages: still inside the same step
        assert _history(db, u)[0]["content"] == first
        add(4)  # 42: the window jumps forward one step
        assert _history(db, u)[0]["content"] != first
        assert len(_history(db, u)) <= 39
