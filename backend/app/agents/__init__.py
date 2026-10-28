"""FounderReach's AI agents, one place for each:

  chat/            the chat assistant: instructions, tools, the conversation loop (web and WhatsApp)
  research.py      before a draft, reads the startup's site and funding news for a specific, recent opener
  proof_of_work.py ideas for something small to make for a startup, a starter, and the link added to the email
  ops.py           the admins' daily report: numbers, rule-based alerts, an AI read of what needs attention

They're triggered from the web routes (app/web/), the scheduler and each other; the rules they rely on
(trial, caps, ownership) live in app/services/.
"""
