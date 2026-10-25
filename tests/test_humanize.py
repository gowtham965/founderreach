import os

os.environ.setdefault("LLM_PROVIDER", "fake")

from app.config import get_settings  # noqa: E402
from app.services import humanize as hz  # noqa: E402

SLOP = ("I hope this email finds you well. I am passionate about leveraging cutting-edge AI to deliver robust, "
        "seamless solutions. I cut API latency by 40% at Acme. Could we talk for 15 minutes next week?")


def test_find_tells_flags_slop():
    tells = hz.find_tells(SLOP)
    for t in ("leverage", "passionate about", "em dash"):
        if t == "em dash":
            continue
        assert any(t in x for x in tells), (t, tells)
    assert "robust" in tells and "seamless" in tells  # tier 2 cluster
    assert hz.find_tells("I cut API latency by 40% at Acme. Could we talk next week?") == []


def test_em_dash_and_emoji():
    assert "em dash" in hz.find_tells("We shipped it — fast.")
    assert "emoji" in hz.find_tells("Shipped it 🚀")


def test_skill_prompt_loads_vendored_files():
    p = hz._skill_prompt()
    assert "Pass 1: Kill the Structure Tells" in p and "Tier 1: Immediate Red Flags" in p
    assert "Output Format" not in p  # long-form output table is stripped for emails


def test_humanize_removes_tells_and_keeps_facts():
    out = hz.humanize("Exploring synergies", SLOP)
    assert out["tells_before"] and out["tells_after"] == [] or "synerg" in " ".join(out["tells_after"])
    assert "40%" in out["body"] and "15 minutes" in out["body"]
    assert "passionate" not in out["body"].lower()


def test_ballooned_rewrite_is_rejected(monkeypatch):
    class Wordy:
        def complete_json(self, *a, **k):
            return {"subject": "x", "body": "word " * 500}
    monkeypatch.setattr(hz, "get_llm", lambda: Wordy())
    out = hz.humanize("Hi", SLOP)
    assert out["body"] == SLOP


def test_mode_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "humanize_mode", "off")
    assert hz.humanize("s", SLOP)["body"] == SLOP
