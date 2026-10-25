from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLM, LLMResponse, Tool, ToolCall, parse_json

__all__ = ["LLM", "LLMResponse", "Tool", "ToolCall", "parse_json", "get_llm"]


@lru_cache
def get_llm() -> LLM:
    s = get_settings()
    provider = s.llm_provider.lower()
    if provider == "anthropic":
        from app.llm.anthropic_provider import AnthropicLLM

        return AnthropicLLM(s.anthropic_api_key, s.anthropic_model)
    if provider == "ollama":
        from app.llm.ollama_provider import OllamaLLM

        return OllamaLLM(s.ollama_base_url, s.ollama_model)
    if provider == "fake":
        from app.llm.fake_provider import FakeLLM

        return FakeLLM()
    raise ValueError(f"Unknown LLM_PROVIDER {s.llm_provider!r} (use anthropic, ollama or fake)")
