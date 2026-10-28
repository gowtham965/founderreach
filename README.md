# FounderReach

An AI job agent for early-stage startups. Users chat with it on the web or WhatsApp; it learns
their background from a resume, finds Bangalore seed-stage startups that just raised, and drafts a
short email to the founder. **FounderReach never sends email**: the draft opens in the user's own
Gmail or mail app, prefilled, and they press send. It then reminds them to follow up (with a
draft) until they say the founder replied.

## Run it on your Mac

```bash
cd founderreach/backend
chmod +x run.sh
./run.sh demo        # try the whole flow offline with a fake LLM, no keys needed
```
Open http://localhost:8000, create an account, attach a resume, then type "find me startups".
In demo mode the startup pool is empty until you run discovery (see below) or seed it.

For real use, put your key in `backend/.env` (created on first run) and start normally:
```bash
ANTHROPIC_API_KEY=sk-ant-...   # in .env
./run.sh
./run.sh test        # the test suite, no network needed
```
Then open the Pipeline tab and press **Refresh startup pool** to pull today's startups.

Requires Python 3.11+ (`brew install python@3.12` if `python3 --version` is older).

## Sign in with Google (optional)

1. In [Google Cloud Console](https://console.cloud.google.com/): create a project.
2. OAuth consent screen → External.
3. Credentials → Create OAuth client ID → **Web application**, with the authorized redirect URI
   `http://localhost:8000/api/auth/google/callback` (and `<BASE_URL>/api/auth/google/callback` once hosted).
4. Put the client ID and secret in `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` and restart.

It asks only for name + email (`openid email profile`). FounderReach never asks for Gmail
access, so there is no Gmail API, no sensitive or restricted scope, and no Google security
review. If someone signs in with Google using an email that already has a password account,
the two are linked into one account.

## WhatsApp (WhatsApp-first sign-up)

People can start by messaging the bot: their WhatsApp number is their account, no form. They send
their resume or LinkedIn PDF as a document, chat with the same agent as the website, type
**dashboard** for a sign-in link to the website. Drafts arrive with an "Open in Gmail" link; they
send it themselves and reply **sent**.

**The bot never messages first.** It only replies to a message the person just sent (inside
Meta's 24-hour window), so no message templates are needed. Morning matches and founder replies
wait and arrive with the person's next message ("📬 While you were away…").

Set up (testing works with Meta's free test number; no SIM needed):
1. [developers.facebook.com](https://developers.facebook.com) → Create app → Business → add **WhatsApp**.
2. WhatsApp → API Setup: copy the **temporary access token** and **Phone number ID**; add your own
   number under "To" as a test recipient (up to 5).
3. App settings → Basic: copy the **App secret**.
4. Put them in `.env`: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`,
   any secret string as `WHATSAPP_VERIFY_TOKEN`, and the test number (digits) as `WHATSAPP_NUMBER`.
5. Give your Mac a public address: `brew install cloudflared` then
   `cloudflared tunnel --url http://localhost:8000`. Set `BASE_URL` in `.env` to the https URL it
   prints, and restart.
6. WhatsApp → Configuration → Webhook: URL `<BASE_URL>/api/whatsapp/webhook`, verify token as in
   `.env`, then subscribe to the **messages** field.
7. Message the test number from your phone. The website's main button becomes "Chat on WhatsApp".

The temporary token expires after about a day; for anything longer, create a System User token in
Meta Business settings. Before launch: a real number (any SIM not already on WhatsApp), business
verification, and check Meta's current pricing (reportedly, service replies beyond a monthly free
allowance are charged from October 2026).

## How it's built

```
backend/                 the Python app (run everything from here)
 app/
  main.py              creates the app, adds security middleware, plugs in the routers
  web/                 one router per area: auth, account (chat, resume), match (card buttons), pay,
                       admin, startup (database), whatsapp, public (pages, health); security.py
  agents/              the AI agents: chat/ (assistant: prompts, tools, loop, cards), research.py,
                       proof_of_work.py, ops.py
  llm/                 provider-neutral AI layer (Claude, OpenAI, Ollama, fake) + usage and cost caps
  services/
    contacts/          founder contact waterfall: fetch, names, website, emails, jobs, waterfall
    discovery/         startup pool: focus, yc, news, articles, startup_map, pool
    billing/           trial and passes: plans, access, orders, razorpay, phonepe
    outreach/          drafts, tracking (sent/replied), follow-ups, enrich, prompts
    profile.py, matching.py, humanize.py, alerts.py,
    backup.py, admin.py, startups_admin.py, whatsapp.py, wa_bot.py, mailer.py, ...
  scheduler.py         daily discovery + matches, follow-ups, ops report, backup
 prompts/              email_style.md and the humanize skill
 tests/                end-to-end tests with the fake LLM, no network
 requirements.txt, run.sh, .env.example
frontend/                no build step
 index.html
 js/                   ES modules: main (wiring), core, urlstate, auth, landing, hero, chat, cards,
                       pipeline, billing, shell, admin, admin-startups
 css/                  base, landing, app, cards, plans, admin (loaded in that order)
 brand/                logo, favicons
render.yaml              Render blueprint (builds and starts from backend/)
```

### Guardrails (enforced in code, not just the prompt)
- **No sending at all.** The app has no access to anyone's email. Drafts open prefilled in
  Gmail or the user's mail app; the user presses send.
- **Pipeline changes need the user's word.** The chat tools that mark an email sent or replied
  refuse unless the user's own latest message says so.
- **Follow-ups are reminders** with a fresh draft, max 2 (`FOLLOWUP_DAYS`), stopped by "they replied".
- **Founder emails and drafts need a confirmed email**, so throwaway accounts can't harvest founders.
- **Daily draft cap** per user (`MAX_DRAFTS_PER_DAY`) bounds AI cost.
- **Guessed addresses are labelled** so users know a draft may bounce.
- Users can only act on their own matches; `DELETE /api/me` wipes an account.

### How emails are made to sound human
1. The first draft follows `backend/prompts/email_style.md` (edit it anytime, no restart).
2. A humanize pass rewrites it using the [humanize-writing](https://github.com/jpeggdev/humanize-writing)
   skill (MIT, vendored in `backend/prompts/humanize/`), with email limits on top: same facts, same ask,
   no longer than before, no em dashes or emojis.
3. A linter built from the skill's word lists checks the result. If tells remain ("leverage",
   "I hope this finds you well", em dashes…), the rewrite runs once more with those named.

`HUMANIZE_MODE=always` (default) | `when_tells` (only rewrite if the linter finds something,
cheaper) | `off`. On Claude, the long skill prompt is prompt-cached so the extra pass is cheap.

### How founder emails are found
Run once per startup when it's first drafted, cached for two weeks and shared across users.
Everything uses public data through allowed routes; nothing logs in to or scrapes LinkedIn.

1. **Names**: from the news story, the YC company page, or the startup's About/Team page.
2. **Published emails**: company-domain addresses on the startup's website, and in commits
   to its public GitHub repos (official GitHub API). Personal addresses like gmail are ignored.
3. **Hunter Email Finder** (only with `HUNTER_API_KEY`, 1 credit) if 1–2 didn't find the founder.
4. **Pattern + verify**: `first@`, `first.last@`, `flast@`, checked with Hunter's verifier
   (0.5 credit each, max 3). Without a key, `first@` is used and labelled a guess.
5. **Shared inbox**: `hello@`/`founders@` published on the site, else `founders@` as a guess.

Each address carries a label (verified, published, likely, shared inbox, guess) shown on the
draft, with one-tap switching to the alternatives. Alongside the email the card shows:
- **Open roles** from the startup's public Ashby, Lever or Greenhouse board, to apply as well.
- **A LinkedIn/X note** (under 280 chars) written automatically when the address is weak, plus
  founder profile links and a LinkedIn search link. The user sends it themselves.

Free limits: GitHub allows 60 API calls an hour without a token. Add a no-scope
`GITHUB_TOKEN` to get 5,000 an hour.

### Going local later
```
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:14b
```
Nothing else changes. Use a 14B+ model with tool calling; smaller ones write generic emails
and fumble tool use. Compare quality with the same resume before switching users over.

### AI costs
Every model call's token usage is stored. See what the AI really cost, per kind of work (from `backend/`):
```bash
../.venv/bin/python -m app.cost_report        # last 7 days, in ₹ (python -m app.cost_report 30 88 for 30 days at ₹88/$)
../.venv/bin/python scripts/cache_probe.py    # checks prompt caching works (needs ANTHROPIC_API_KEY; a few paise)
```
What keeps it down (`app/llm/anthropic_provider.py`):
- **Prompt caching**: tools and instructions are cached on every call; chat also caches its history,
  and the history window moves in steps of 10 so the cached part stays identical between turns.
- **Cheaper model for bulk work and drafts**: news extraction and startup scoring run on
  `ANTHROPIC_MODEL_FAST` (Claude Haiku 5.5) at low effort; email drafts, rewrites and follow-ups on
  `ANTHROPIC_MODEL_DRAFT` (Claude Haiku 5.5); chat stays on `ANTHROPIC_MODEL` (Claude Sonnet 5.5).
- **Effort per kind of work** (`EFFORT_CHAT`, `EFFORT_EXTRACT`, `EFFORT_DRAFT`).
- **Draft cap** (`MAX_DRAFTS_PER_DAY`).

The model and effort choices haven't been checked against an eval of real requests yet. If drafts
look worse, set `ANTHROPIC_MODEL_DRAFT=claude-sonnet-5-5`; if matches do, set `ANTHROPIC_MODEL_FAST`
to the main model and clear the `EFFORT_*` values.

### Bangalore focus
Only pre-seed and seed startups in Bangalore get into the pool and into matches. Change
`FOCUS_CITIES` (e.g. `bengaluru,bangalore,mumbai`) or `FOCUS_STAGES` (e.g. `pre-seed,seed,series a`)
in `.env` to widen it; leave either empty for no limit.

Sources: Indian startup news (Inc42, Entrackr, YourStory, ET Startups), recent YC batches, and
[Bangalore Startup Map](https://www.bangalorestartupmap.com/) (read once a week in one request,
facts only). **Used with the owner's permission (October 2026), on the condition that it's
credited with a link to bangalorestartupmap.com.** Keep those links: the landing page's sources
strip, FAQ and footer, every startup card, the landing-page search results, the ticker, and
WhatsApp match messages all link back to it. Don't add `rel="nofollow"` to them.

### Adding discovery sources
Write a function in `services/discovery.py` that returns dicts with `name, domain, one_liner,
stage, location, tags, signal, source, source_url, founders`, and add it to `SOURCES`.
Good next ones: Product Hunt API (needs a token), Wellfound, careers-page crawling.
Don't scrape LinkedIn; it breaks their terms and gets accounts banned.

## Deploying
Works on Render/Railway/Fly as one web process: `cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
(`render.yaml` already does this).
Set `BASE_URL` to your public URL, switch `DATABASE_URL` to Postgres, and run a single instance
so the scheduler doesn't run jobs twice (or move `scheduler.py` jobs to a separate worker).

## Known limits
- Founder emails without Hunter are guesses; expect some bounces.
- News extraction depends on RSS summaries; some stories won't name the domain.
- Chat responses aren't streamed yet.
- No billing yet (planned: 2-day trial, then passes via a payment gateway).
- Replies aren't detected automatically (no inbox access); users tell the app "they replied".
