# Vendored: humanize-writing

`SKILL.md` and `ai-tells.md` come unchanged from
https://github.com/jpeggdev/humanize-writing (MIT, see `LICENSE`).

`app/services/humanize.py` uses them in two ways:
1. The editing passes (from `SKILL.md`, between "The Editing Process" and "Output Format")
   plus the full `ai-tells.md` become the system prompt for a rewrite pass on every draft
   and follow-up, with email-specific limits added on top.
2. A regex linter built from the same word lists checks the rewrite. If tells remain, the
   rewrite runs once more with those tells named.

To update: replace these two files with newer versions from the repo. No code changes needed.
