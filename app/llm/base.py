"""Provider-neutral LLM interface.

Everything else in the app talks to `LLM`, never to a vendor SDK, so swapping
Claude for a local model is a one-line config change (LLM_PROVIDER=ollama).

Neutral message format:
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall-as-dict, ...]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "input": self.input}


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-specific items to send back verbatim in the next call of the same tool loop
    # (OpenAI reasoning models need their reasoning items returned with tool results). JSON-safe.
    raw: list[dict] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema object

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class LLM(ABC):
    @abstractmethod
    def chat(self, system: str, messages: list[dict], tools: list[Tool] | None = None, max_tokens: int = 16000,
             task: str = "chat", cache_history: bool = False) -> LLMResponse:
        """One model turn. May return text, tool calls, or both.

        `task` names the kind of work (picks model/effort and labels usage); `cache_history` marks a
        multi-turn conversation whose history should be cached as it grows."""

    def complete_json(self, system: str, prompt: str, task: str = "", max_tokens: int = 8000):
        """Ask for a JSON answer and parse it. `task` is a label used for routing, usage and the fake provider."""
        resp = self.chat(system + "\n\nRespond with JSON only. No prose, no code fences.",
                         [{"role": "user", "content": prompt}], max_tokens=max_tokens, task=task)
        return parse_json(resp.text)


def parse_json(text: str):
    """Tolerant JSON parse: strips code fences and finds the outermost object/array."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError(f"No JSON found in model output: {text[:200]!r}")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start : end + 1])
