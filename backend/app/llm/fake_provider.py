"""Deterministic stand-in for tests and offline demos (LLM_PROVIDER=fake).

It routes on keywords so the full agent loop — tools, drafts, approvals — can be
exercised without an API key. It is not meant to be smart.
"""
import json
import re
import uuid

from app.llm.base import LLM, LLMResponse, ToolCall


class FakeLLM(LLM):
    def chat(self, system, messages, tools=None, max_tokens=16000, task="chat", cache_history=False) -> LLMResponse:
        last = messages[-1]
        if last["role"] == "tool":
            ok = '"ok": true' in last["content"]
            return LLMResponse(text="Done. Details are in the card below." if ok else f"That didn't work: {last['content'][:200]}")
        text = last["content"]
        low = text.lower()
        tool_names = {t.name for t in tools or []}
        m = re.search(r"#(\d+)", text)
        mid = int(m.group(1)) if m else None

        def call(name, **kw):
            return LLMResponse(tool_calls=[ToolCall(id=uuid.uuid4().hex[:8], name=name, input=kw)])

        if "replied" in low and mid and "mark_replied" in tool_names:
            return call("mark_replied", match_id=mid)
        if "follow-up" in low and "sent" in low and mid and "mark_followup_sent" in tool_names:
            return call("mark_followup_sent", match_id=mid)
        if "sent" in low and mid and "mark_sent" in tool_names:
            return call("mark_sent", match_id=mid)
        if "skip" in low and mid and "skip_match" in tool_names:
            return call("skip_match", match_id=mid)
        if ("draft" in low or "write" in low or "shorter" in low) and mid and "draft_email" in tool_names:
            return call("draft_email", match_id=mid, instructions=text)
        if ("match" in low or "find" in low or "startups" in low) and "find_matches" in tool_names:
            return call("find_matches")
        if ("status" in low or "how's" in low) and "get_status" in tool_names:
            return call("get_status")
        if ("remote" in low or "prefer" in low or "want" in low) and "update_profile" in tool_names:
            return call("update_profile", preferences={"notes": text})
        return LLMResponse(text="Got it. Share your resume or tell me what roles you want next.")

    def complete_json(self, system, prompt, task="", max_tokens=2000):
        if task == "extract_profile":
            return {
                "name": "Test User",
                "summary": "Backend engineer with Python and data experience.",
                "skills": ["python", "fastapi", "sql", "llm"],
                "highlights": ["Cut API latency by 40%"],
                "target_roles": ["AI engineer"],
                "links": {},
            }
        if task == "score_matches":
            ids = [int(i) for i in re.findall(r'"id": (\d+)', prompt)]
            return [{"id": i, "score": 80 - n, "reason": "Stack overlaps with your Python/LLM work."} for n, i in enumerate(ids)]
        if task == "draft_email":
            return {"subject": "Python + LLM engineer, quick intro",
                    "body": "Hi,\n\nSaw your recent launch. I build LLM apps in Python and cut API latency 40% at my last job. 15 min next week?\n\nTest"}
        if task == "followup":
            return {"body": "Hi, bumping this in case it got buried. Happy to share a quick demo."}
        if task == "humanize":
            # crude stand-in: drop any sentence that contains a detected tell
            from app.services.humanize import find_tells

            m = re.search(r"Subject: (.*?)\n\n(.*)\n\nReturn JSON", prompt, re.S)
            subject, body = (m.group(1), m.group(2)) if m else ("", "")
            keep = [s for s in re.split(r"(?<=[.!?])\s+", body) if not find_tells(s)]
            return {"subject": subject, "body": " ".join(keep) or body}
        if task == "extract_founders":
            names = re.findall(r"([A-Z][a-z]+ [A-Z][a-z]+), (?:co-)?founder", prompt, re.I)
            return [{"name": n, "title": "Founder"} for n in names]
        if task == "dm_note":
            return {"note": "Saw your seed round. I build LLM apps in Python and cut API latency 40%. Open to a quick chat? Test"}
        if task == "extract_startups":
            return []
        if task == "pow_ideas":
            return {"ideas": [{"title": f"Idea {n}", "what": f"Make thing {n}.", "why": "They just launched.",
                               "effort": "about 2 hours", "kind": "teardown"} for n in (1, 2, 3)]}
        if task == "pow_starter":
            return {"starter": "Goal: a 1-page teardown.\nSteps: ...\n[what you saw in their app]\nCheck before sharing: the flow."}
        if task == "research_pick":
            return {"urls": re.findall(r"^(https?://\S+) \|", prompt, re.M)[:3]}
        if task == "research":
            src = re.findall(r"^\[(https?://[^\]]+)\]", prompt, re.M)
            first = src[-1] if src else ""
            return {"what_they_do": "Builds software for testing.", "building": ["inference"],
                    "recent": [{"fact": "Launched a new product last month.", "source": first}] if first else [],
                    "hooks": [{"line": "Saw you launched a new product last month.", "source": first}] if first else []}
        return json.loads("{}")
