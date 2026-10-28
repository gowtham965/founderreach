"""Check that chat prompt caching really works: sends one chat-shaped request twice, byte-identical.

    .venv/bin/python scripts/cache_probe.py

Needs ANTHROPIC_API_KEY in .env. Costs a few paise. Exits 1 if the second call read nothing from the cache.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.chat import SYSTEM, TOOLS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.anthropic_provider import AnthropicLLM  # noqa: E402
from app.services.discovery import focus_label  # noqa: E402


def main() -> int:
    s = get_settings()
    llm = AnthropicLLM(s.anthropic_api_key, s.anthropic_model)
    seen = []
    original = llm.client.messages.create

    def spy(**kw):
        resp = original(**kw)
        seen.append(resp.usage)
        return resp

    llm.client.messages.create = spy
    system = SYSTEM.format(profile="Summary: Backend engineer\nSkills: python, go", focus=focus_label())
    messages = [{"role": "user", "content": "hi, what can you do?"}]
    for _ in range(2):
        llm.chat(system, messages, [t for t in TOOLS], max_tokens=300, task="cache_probe", cache_history=True)
    for n, u in enumerate(seen, 1):
        print(f"call {n}: input={u.input_tokens} cache_write={u.cache_creation_input_tokens} "
              f"cache_read={u.cache_read_input_tokens} output={u.output_tokens}")
    if not seen[1].cache_read_input_tokens:
        print("✗ The second call read nothing from the cache. Something in the prompt is changing between calls.")
        return 1
    print("✓ Caching works: the second call re-read the instructions and tools at the cache rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
