"""Local models via Ollama's /api/chat (supports tool calling on models like qwen2.5, llama3.1).

Switch with: LLM_PROVIDER=ollama  OLLAMA_MODEL=qwen2.5:14b
Smaller models are noticeably worse at tool use and at writing non-generic emails;
14B+ is the practical floor for this app.
"""
import uuid

import httpx

from app.llm.base import LLM, LLMResponse, ToolCall


class OllamaLLM(LLM):
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def chat(self, system, messages, tools=None, max_tokens=16000, task="chat", cache_history=False) -> LLMResponse:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}] + _to_ollama(messages),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t.schema()} for t in tools]
        r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=180)
        r.raise_for_status()
        msg = r.json().get("message", {})
        out = LLMResponse(text=msg.get("content", "") or "")
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            out.tool_calls.append(ToolCall(id=uuid.uuid4().hex[:12], name=fn.get("name", ""), input=fn.get("arguments") or {}))
        return out


def _to_ollama(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant":
            entry = {"role": "assistant", "content": m.get("content", "")}
            if m.get("tool_calls"):
                entry["tool_calls"] = [{"function": {"name": tc["name"], "arguments": tc["input"]}} for tc in m["tool_calls"]]
            out.append(entry)
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
        else:
            out.append({"role": "user", "content": m["content"]})
    return out
