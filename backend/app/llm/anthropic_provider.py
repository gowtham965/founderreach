"""Claude via the Anthropic Messages API, with native tool use.

Cost levers live here (see README → "AI costs"):
  * prompt caching: the tools + system prefix always carry a breakpoint; multi-step chat also
    caches its growing history (top-level automatic caching), so each tool step and each new
    message re-reads earlier turns at the cache rate instead of full price
  * per-task model and effort (ANTHROPIC_MODEL_FAST, ANTHROPIC_MODEL_DRAFT, EFFORT_*): extraction,
    scoring and drafts run on the cheaper model; chat stays on the main model
  * every call's token usage is recorded (app/llm/usage.py) so costs are measured, not guessed
"""
import logging

from anthropic import Anthropic

from app.config import get_settings
from app.llm.base import LLM, LLMResponse, Tool, ToolCall
from app.llm import usage

log = logging.getLogger(__name__)

# Structured, checkable, high-volume work: the fast model handles it.
FAST_TASKS = {"extract_startups", "score_matches", "extract_founders", "extract_profile", "research_pick", "research"}
# The user (and founders) read these word for word: their own model setting.
DRAFT_TASKS = {"draft_email", "humanize", "followup", "dm_note"}


def route(task: str) -> tuple[str, str]:
    """(model, effort) for a kind of work; effort "" means the model's default."""
    s = get_settings()
    if task in FAST_TASKS:
        return s.anthropic_model_fast or s.anthropic_model, s.effort_extract
    if task in DRAFT_TASKS:
        return s.anthropic_model_draft or s.anthropic_model, s.effort_draft
    return s.anthropic_model, s.effort_chat


class AnthropicLLM(LLM):
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def chat(self, system, messages, tools=None, max_tokens=16000, task="chat", cache_history=False) -> LLMResponse:
        model, effort = route(task)
        kwargs = {
            "model": model,
            # A backstop, not a tuning knob: with thinking on, a tight cap cuts replies off mid-way
            "max_tokens": max_tokens,
            # Tools render before system, so this one breakpoint caches tools + system together.
            # The system text must stay byte-stable: per-request notes go in the user turn instead.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": _to_anthropic(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        if cache_history:
            # Multi-turn chat: cache the conversation as it grows (the breakpoint moves forward itself).
            # Not for one-shot prompts, whose unique tail would only pay the cache-write premium.
            kwargs["cache_control"] = {"type": "ephemeral"}
        if effort:
            kwargs["output_config"] = {"effort": effort}
        resp = self.client.messages.create(**kwargs)
        usage.record(task, model, resp.usage, resp.stop_reason)
        if resp.stop_reason == "max_tokens":
            log.warning("%s hit max_tokens=%d on %s; the reply was cut off", task, max_tokens, model)
        out = LLMResponse()
        for block in resp.content:
            if block.type == "text":
                out.text += block.text
            elif block.type == "tool_use":
                out.tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        return out


def _to_anthropic(messages: list[dict]) -> list[dict]:
    """Neutral → Anthropic. Consecutive tool results are grouped into one user turn."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            out.append({"role": "assistant", "content": blocks or m.get("content", "")})
        elif role == "tool":
            result = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out
