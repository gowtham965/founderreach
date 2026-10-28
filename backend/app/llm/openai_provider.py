"""OpenAI via the Responses API (the newest models only support tool calling there).

Switch with: LLM_PROVIDER=openai  OPENAI_API_KEY=...
Same routing as Claude: chat on OPENAI_MODEL, extraction/scoring on OPENAI_MODEL_FAST, drafts on
OPENAI_MODEL_DRAFT, with the shared EFFORT_* settings. Prompt caching is automatic on OpenAI.

Stateless (store=False): nothing is kept on OpenAI's side between calls. Reasoning models need their
reasoning items sent back with tool results, so those come back encrypted and ride along in the
assistant turn (LLMResponse.raw) until the tool loop ends.
"""
import json
import logging
from types import SimpleNamespace

from openai import OpenAI

from app.config import get_settings
from app.llm import usage
from app.llm.anthropic_provider import DRAFT_TASKS, FAST_TASKS
from app.llm.base import LLM, LLMResponse, ToolCall

log = logging.getLogger(__name__)


def route(task: str) -> tuple[str, str]:
    """(model, effort) for a kind of work; effort "" means the model's default."""
    s = get_settings()
    if task in FAST_TASKS:
        return s.openai_model_fast or s.openai_model, s.effort_extract
    if task in DRAFT_TASKS:
        return s.openai_model_draft or s.openai_model, s.effort_draft
    return s.openai_model, s.effort_chat


class OpenAILLM(LLM):
    def __init__(self, api_key: str):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self.client = OpenAI(api_key=api_key)

    def chat(self, system, messages, tools=None, max_tokens=16000, task="chat", cache_history=False) -> LLMResponse:
        usage.check_budget()  # site-wide daily AI spend cap
        model, effort = route(task)
        kwargs = {
            "model": model,
            "instructions": system,
            "input": _to_openai(messages),
            "max_output_tokens": max_tokens,  # a backstop: reasoning tokens count toward it
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": f"founderreach-{task}",  # same-task calls share a cache
        }
        if tools:
            kwargs["tools"] = [{"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
                               for t in tools]
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        resp = self.client.responses.create(**kwargs)
        _record(task, model, resp)
        out = LLMResponse()
        for item in resp.output:
            if item.type == "message":
                text = "".join(c.text for c in item.content if getattr(c, "type", "") == "output_text")
                out.text += text
                out.raw.append({"role": "assistant", "content": text})
            elif item.type == "function_call":
                try:
                    args = json.loads(item.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out.tool_calls.append(ToolCall(id=item.call_id, name=item.name, input=args))
                out.raw.append({"type": "function_call", "call_id": item.call_id, "name": item.name, "arguments": item.arguments})
            elif item.type == "reasoning":
                out.raw.append({"type": "reasoning", "id": item.id, "summary": [s.model_dump() for s in item.summary or []],
                                "encrypted_content": item.encrypted_content})
        return out


def _record(task: str, model: str, resp) -> None:
    u = resp.usage
    cached = (getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0) if u else 0
    stop = "max_tokens" if resp.status == "incomplete" and getattr(resp.incomplete_details, "reason", "") == "max_output_tokens" else resp.status
    if stop == "max_tokens":
        log.warning("%s hit max_output_tokens on %s; the reply was cut off", task, model)
    # OpenAI counts cached tokens inside input_tokens; the usage table keeps them apart
    usage.record(task, model, SimpleNamespace(input_tokens=(u.input_tokens - cached) if u else 0, cache_read_input_tokens=cached,
                                              cache_creation_input_tokens=0, output_tokens=u.output_tokens if u else 0), stop)


def _to_openai(messages: list[dict]) -> list[dict]:
    """Neutral → Responses API input items."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            if m.get("raw"):  # this provider's own turn: send back exactly what it produced
                out.extend(m["raw"])
                continue
            if m.get("content"):
                out.append({"role": "assistant", "content": m["content"]})
            for tc in m.get("tool_calls", []):
                out.append({"type": "function_call", "call_id": tc["id"], "name": tc["name"], "arguments": json.dumps(tc["input"])})
        elif role == "tool":
            out.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
    return out
