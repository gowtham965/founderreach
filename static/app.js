// FounderReach web client. Plain JS, no build step. All server text goes in via textContent.
const $ = (s, el = document) => el.querySelector(s);
const store = {
  get token() { try { return localStorage.getItem("fr_token"); } catch { return null; } },
  set token(v) { try { v ? localStorage.setItem("fr_token", v) : localStorage.removeItem("fr_token"); } catch {} },
};

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (store.token) headers.Authorization = `Bearer ${store.token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && store.token) { store.token = null; showAuth(); throw new Error("Signed out"); }
  const data = await res.json().catch(() => ({}));
  const msg = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg || `Request failed (${res.status})`;
  if (res.status === 402) openPlans(msg);  // trial over: offer a pass right where they hit the wall
  if (!res.ok) throw new Error(msg);
  return data;
}

/* ---------- auth ---------- */
let mode = "signup";
let emailSignupOpen = true;  // false while the server can't send confirmation emails
function setMode(m) {
  mode = m;
  document.querySelectorAll(".seg button").forEach(b => b.setAttribute("aria-selected", b.dataset.mode === m));
  const closed = m === "signup" && !emailSignupOpen;
  $("#name-field").hidden = m !== "signup" || closed;
  document.querySelectorAll("#auth-form .field:not(#name-field)").forEach(f => f.hidden = closed);
  $("#auth-submit").hidden = closed;
  $("#signup-closed").hidden = !closed;
  $("#auth-submit").textContent = m === "signup" ? "Create account" : "Sign in";
  $("#auth-error").textContent = "";
}
document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));
$("#auth-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = { email: f.get("email"), password: f.get("password") };
  if (mode === "signup") body.name = f.get("name") || "";
  try {
    const { token } = await api(`/api/auth/${mode}`, { method: "POST", json: body });
    store.token = token;
    boot();
  } catch (err) { $("#auth-error").textContent = err.message; }
});
/* ---------- landing page ---------- */
document.documentElement.classList.add("js");
function goToForm(m) {
  setMode(m);
  const form = $("#auth-form");
  form.scrollIntoView({ behavior: "smooth", block: "center" });
  form.classList.remove("flash"); void form.offsetWidth; form.classList.add("flash");
  setTimeout(() => form.querySelector(m === "signup" ? "[name=name]" : "[name=email]").focus({ preventScroll: true }), 500);
}
// Signed-in people who came back to the home page: every "get started" button opens their chat instead
document.querySelectorAll("[data-goto]").forEach(b => b.addEventListener("click", () => store.token ? location.assign("/") : goToForm(b.dataset.goto)));
const nav = $(".nav");
addEventListener("scroll", () => nav.classList.toggle("scrolled", scrollY > 8), { passive: true });
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: 0.12 });
  document.querySelectorAll(".reveal").forEach(el => io.observe(el));
} else document.querySelectorAll(".reveal").forEach(el => el.classList.add("in"));

/* ---------- hero: animated example, then the real "What do you do?" preview ---------- */
const sleep = ms => new Promise(r => setTimeout(r, ms));
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const SQ_COLORS = ["#ff7a45", "#5b6cff", "#12a150", "#a35bff", "#e0457b", "#0ea5b7"];
const sqColor = name => SQ_COLORS[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % SQ_COLORS.length];
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

const DEMO = [
  { name: "Kirana AI", tag: "raised 2 days ago", m: "HSR Layout · Seed ₹12 Cr · AI for kirana stores", who: "Priya Sharma, CEO · priya@kirana.ai", fit: 92 },
  { name: "Ledgerline", m: "Indiranagar · Seed $2M · Fintech APIs", who: "Arjun Mehta, CTO · arjun@ledgerline.io", fit: 87 },
  { name: "Tiffin Labs", m: "Koramangala · Pre-seed · Cloud kitchens OS", who: "Rohan Iyer, Founder · rohan@tiffin.in", fit: 81 },
];
const DEMO_QUERY = "backend · python · fintech";
const DEMO_BODY = "Hi Priya, congrats on the seed round. I built payment reconciliation at Acme that cut manual work 70%, and Kirana AI's ledger problem looks familiar. 15 minutes next week?";

const exampleLabel = () => el("p", "ex-row", "Example · type your skills above to see real startups");
function previewCard({ name, tag, m, who, blur, chips, fit }, i) {
  const card = el("div", `pv demo-${i + 1}`);
  const sq = el("span", "sq", name[0]); sq.style.background = sqColor(name);
  const t = el("div", "t");
  t.append(el("b", "", name));
  if (tag) t.append(el("span", "tag", tag));
  t.append(el("div", "m", m));
  if (chips?.length) { const c = el("div", "chips"); chips.forEach(x => c.append(el("span", "chip", x))); t.append(c); }
  if (who) { const w = el("div", `who${blur ? " blur" : ""}`, who); if (!blur && who.includes("@")) w.append(" ", el("span", "ok", "✓ verified")); t.append(w); }
  card.append(sq, t);
  if (fit) { const f = el("span", "fit"); f.style.setProperty("--p", fit); f.append(el("span", "", fit)); card.append(f); }
  return card;
}

let demoOn = true;  // false once the visitor starts typing: from then on it's their search
async function typeInto(node, text, speed, prop = "value") {
  node[prop] = "";
  for (const ch of text) { if (!demoOn) return; node[prop] += ch; await sleep(speed); }
}
function showDemoFinal() {
  const res = $("#results"); res.textContent = ""; res.append(exampleLabel());
  DEMO.forEach((d, i) => { const c = previewCard({ ...d, blur: false }, i); c.classList.add("in"); c.style.transition = "none"; res.append(c); });
  $("#try-input").placeholder = DEMO_QUERY;
  if (matchMedia("(min-width: 980px)").matches) {
    $("#stage").classList.add("drafting"); $("#demo-body").textContent = DEMO_BODY; $("#demo-send").textContent = "Sent ✓";
    $("#demo-draft").classList.add("in"); $("#demo-toast").classList.add("in");
  }
}
async function demoLoop() {
  const input = $("#try-input"), res = $("#results"), stage = $("#stage");
  const wide = () => matchMedia("(min-width: 980px)").matches;
  while (demoOn) {
    if ($("#auth").hidden || document.hidden) { await sleep(1000); continue; }
    if (!demoOn) break;
    res.textContent = ""; stage.classList.remove("drafting");
    ["#demo-draft", "#demo-toast"].forEach(id => $(id).classList.remove("in"));
    $("#demo-body").textContent = ""; $("#demo-send").textContent = "Open in Gmail";
    await sleep(500);
    await typeInto(input, DEMO_QUERY, 55); if (!demoOn) break;
    await sleep(350);
    if (!demoOn) break;
    res.append(exampleLabel());
    const cards = DEMO.map((d, i) => previewCard({ ...d, blur: true }, i));
    for (const c of cards) {
      if (!demoOn) break;
      res.append(c); await sleep(30); c.classList.add("in"); await sleep(240);
    }
    await sleep(900); if (!demoOn) break;
    cards[0].querySelector(".who").classList.remove("blur");
    cards[0].querySelector(".who").append(" ", el("span", "ok", "✓ verified"));
    await sleep(700); if (!demoOn) break;
    if (wide()) {
      stage.classList.add("drafting"); await sleep(250); if (!demoOn) break;
      $("#demo-draft").classList.add("in");
      await typeInto($("#demo-body"), DEMO_BODY, 18, "textContent"); if (!demoOn) break;
      await sleep(500); if (!demoOn) break; $("#demo-send").textContent = "Sent ✓";
      await sleep(900); if (!demoOn) break; $("#demo-toast").classList.add("in");
    }
    await sleep(3200); if (!demoOn) break;
    input.value = "";
  }
}
let demoVisible = true;  // the example (animated or still) is on screen
function stopDemo() {
  if (!demoVisible) return;
  demoVisible = false;
  demoOn = false;
  $("#stage").classList.remove("drafting");
  ["#demo-draft", "#demo-toast"].forEach(id => $(id).classList.remove("in"));
  $("#results").textContent = "";
  $("#try-input").value = "";
  $("#try-hint").textContent = "Type your skills or the kind of startup you want, then press Show me.";
}
$("#try-input").addEventListener("focus", stopDemo);
$("#try-input").addEventListener("pointerdown", stopDemo);
$("#try-form").addEventListener("submit", async e => {
  e.preventDefault();
  // pressed "Show me" while the example was playing: search the example's skills for real
  const q = (demoVisible ? DEMO_QUERY : $("#try-input").value).replaceAll("·", " ").trim(), res = $("#results");
  stopDemo();
  if (!q) { $("#try-input").focus(); return; }
  $("#try-input").value = q.replace(/\s+/g, " ");
  res.textContent = "";
  let rows;
  try { rows = await api(`/api/public/preview?q=${encodeURIComponent(q)}`); }
  catch (err) { res.textContent = ""; res.append(el("div", "pv-empty", err.message)); return; }
  res.textContent = "";  // anything the example added while we waited goes
  if (!rows.length) {
    res.append(el("div", "pv-empty", "No close match in the Bangalore pool yet. Try broader words, like “backend”, “sales” or “fintech”."));
    return;
  }
  rows.forEach((r, i) => {
    const c = previewCard({
      name: r.name, tag: r.stage, chips: r.matched,
      m: [r.area, r.what].filter(Boolean).join(" · "),
      // never real founder data: a placeholder shape, blurred
      who: r.has_founder ? "Founder name · founder@company.com" : "Founder details after sign-up", blur: true,
    }, i);
    if (r.map_url) {  // credit the directory this startup came from, with a link to its page there
      const via = Object.assign(el("a", "via", "via Bangalore Startup Map"), { href: r.map_url, target: "_blank", rel: "noopener" });
      c.querySelector(".m").append(" · ", via);
    }
    res.append(c); setTimeout(() => c.classList.add("in"), 40 + i * 140);
  });
  const lock = el("button", "pv lockrow in", "🔓 Sign up free to see the founders and their emails →");
  lock.type = "button"; lock.addEventListener("click", () => goToForm("signup"));
  res.append(lock);
  $("#try-hint").textContent = "Real Bangalore startups from today's pool. Founders unlock when you sign up.";
});

/* ---------- ticker + startup count: real data or nothing ---------- */
async function loadPublic() {
  try {
    const { startups } = await api("/api/public/stats");
    if (startups >= 10) $("#badge-text").textContent = `${Math.floor(startups / 10) * 10}+ seed-stage startups across Namma Bengaluru`;
  } catch {}
  try {
    const rows = await api("/api/public/fresh");
    if (rows.length < 3) return;
    const track = $("#ticker-track"); track.textContent = "";
    const lead = () => [el("span", "item live", "● Fresh in Namma Bengaluru"), el("span", "item kn", "ನಮ್ಮ ಬೆಂಗಳೂರು")];
    const item = r => {
      const it = el("span", "item");
      const href = r.news_url || r.map_url;  // news story, or the startup's page on Bangalore Startup Map
      const name = href ? Object.assign(el("a", "", r.name), { href, target: "_blank", rel: "noopener" }) : el("b", "", r.name);
      it.append(name, el("span", "amt", r.stage), el("span", "", r.area), el("span", "sep", "◆"));
      return it;
    };
    for (let k = 0; k < 2; k++) { track.append(...lead()); rows.forEach(r => track.append(item(r))); }  // twice, for a seamless loop
    $("#ticker").hidden = false;
  } catch {}
}

/* WhatsApp-first: when the bot number is set up, "Chat on WhatsApp" becomes the main way in */
const WA_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm0 18.2c-1.5 0-3-.4-4.3-1.2l-.3-.2-3 .8.8-2.9-.2-.3A8.2 8.2 0 1 1 12 20.2Zm4.5-6.1c-.2-.1-1.5-.7-1.7-.8-.2-.1-.4-.1-.6.1l-.8 1c-.1.2-.3.2-.5.1-.7-.3-1.4-.7-2-1.3-.6-.5-1-1.2-1.4-1.9-.1-.2 0-.4.1-.5l.4-.5.2-.4v-.4l-.8-1.9c-.2-.5-.4-.4-.6-.4h-.5c-.2 0-.4.1-.6.3-.6.6-.9 1.4-.9 2.2.1 1 .4 1.9 1 2.7 1.1 1.6 2.5 2.9 4.2 3.7.5.2 1 .4 1.4.5.6.2 1.2.1 1.7-.1.6-.3 1-.8 1.2-1.4.1-.3.1-.6 0-.9l-.5-.2Z"/></svg>';
function waButton(href, label, cls) {
  const a = document.createElement("a");
  a.href = href; a.target = "_blank"; a.rel = "noopener"; a.className = `btn wa ${cls}`;
  a.innerHTML = WA_ICON;  // static markup only
  a.append(label);
  return a;
}
function showWhatsApp(href) {
  if (document.querySelector(".btn.wa")) return;
  const hero = document.querySelector(".cta-row [data-goto=signup]");
  hero.before(waButton(href, "Chat on WhatsApp", "lg"));
  hero.className = "btn ghost lg"; hero.textContent = "Or sign up with email";
  document.querySelector(".nav-cta [data-goto=signup]").replaceWith(waButton(href, "Chat on WhatsApp", ""));
  $(".join-copy").append(waButton(href, "Start on WhatsApp →", "lg join-wa"));
  $(".join-copy p").textContent = "Message us on WhatsApp, send your resume, and get your first Bangalore matches today. Prefer email? Sign up here.";
}

let landingStarted = false;
function showAuth() {
  $("#app").hidden = true; $("#auth").hidden = false; setMode("signup");
  if (!landingStarted) {
    landingStarted = true;
    loadPublic();
    if (reduceMotion) { demoOn = false; showDemoFinal(); } else demoLoop();
  }
  if (justVerified) {
    setMode("login");
    $("#auth-error").textContent = justVerified === "1" ? "Email confirmed ✓ Sign in to continue." : "That confirmation link has expired. Sign in to get a new one.";
    $("#auth-form").scrollIntoView({ block: "center" });
  }
  api("/api/auth/providers").then(p => {
    emailSignupOpen = p.email_signup !== false;
    setMode(mode);
    // Shown either way; until Google keys are in .env, explain instead of hitting a 503 page.
    $(".btn.google").onclick = p.google ? null : e => {
      e.preventDefault();
      $("#auth-error").textContent = "Google sign-in isn't switched on yet. Use email for now.";
    };
    if (p.whatsapp) showWhatsApp(p.whatsapp);
  }).catch(() => {});
  const g = new URLSearchParams(location.search).get("google");
  if (g) {
    $("#auth-error").textContent = g === "denied" ? "Google sign-in was cancelled." : "Google sign-in didn't work. Try again or use email.";
    history.replaceState({}, "", "/");
  }
}

// Coming back from Google: the server puts the session token in the #fragment.
const googleToken = new URLSearchParams(location.hash.slice(1)).get("token");
if (googleToken) { store.token = googleToken; history.replaceState({}, "", "/"); }

/* ---------- tabs ---------- */
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(x => x.setAttribute("aria-selected", x === b));
  $("#chat").hidden = b.dataset.tab !== "chat";
  $("#pipeline").hidden = b.dataset.tab !== "pipeline";
  if (b.dataset.tab === "pipeline") loadPipeline();
}));

/* ---------- chat ---------- */
const list = $("#messages");
let lastId = 0;
const URL_RE = /https?:\/\/[^\s<>"')]+/g;
function linkify(el, text) {
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    const url = m[0].replace(/[.,;:!?]+$/, "");  // don't swallow sentence punctuation
    el.append(text.slice(last, m.index));
    const a = document.createElement("a"); a.href = url; a.target = "_blank"; a.rel = "noopener"; a.textContent = url;
    el.append(a);
    last = m.index + url.length;
  }
  el.append(text.slice(last));
}
function addMsg(role, text, extra = "") {
  const li = document.createElement("li");
  li.className = `msg ${role} ${extra}`.trim();
  if (extra === "typing") {  // three bouncing dots; the words are for screen readers
    li.setAttribute("aria-label", text);
    li.append(el("i"), el("i"), el("i"));
  } else linkify(li, text);
  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
  return li;
}
function addCards(cards) {
  for (const c of cards || []) {
    const wrap = document.createElement("li");
    wrap.className = "cards";
    const items = c.type === "matches" ? c.items : [c.item];
    if (c.type === "matches" && items.length > 1)
      wrap.appendChild(el("p", "cards-head", `${items.length} Bengaluru startups that fit you`));
    items.forEach(it => wrap.appendChild(letter(it)));
    list.appendChild(wrap);
  }
  list.scrollTop = list.scrollHeight;
}
let myName = "";
function hello() {  // the top of the chat: a namaskara, like the landing page
  const li = el("li", "hello");
  const k = el("span", "kn namaskara"); k.append(el("span", "flag", "ನಮಸ್ಕಾರ"));
  li.append(k, el("h2", "", myName ? `Hi ${myName.split(" ")[0]}, let's find your next startup.` : "Let's find your next startup."),
    el("p", "", "Seed-stage teams in Namma Bengaluru that just raised, and the founder's inbox for each."));
  list.appendChild(li);
}
const QUICK = [
  ["📎 Add resume", () => $("#resume").click(), p => !p?.ready],
  ["Find startups for me", "Find startups that fit me"],
  ["Only fintech", "Show me fintech startups"],
  ["Draft to my top match", "Draft an email to my best match"],
  ["What's due?", "Anything I should follow up on?"],
];
function quick(profile) {
  const q = $("#quick"); q.textContent = "";
  for (const [label, what, when] of QUICK) {
    if (when && !when(profile)) continue;
    const b = el("button", "qchip", label); b.type = "button";
    b.addEventListener("click", () => {
      if (typeof what === "function") return what();
      if (busy) return;
      input.value = what; $("#composer").requestSubmit();
    });
    q.appendChild(b);
  }
}
async function loadChat() {
  list.textContent = "";
  hello();
  const rows = await api("/api/chat");
  rows.forEach(r => { addMsg(r.role, r.content, r.unread ? "unread" : ""); addCards(r.cards); });
  lastId = rows.length ? rows[rows.length - 1].id : 0;
}
async function poll() {
  if (!store.token || document.hidden) return;
  try {
    if (busy) return;
    const fresh = await api(`/api/chat?after=${lastId}`);  // only what's new since the last look
    if (fresh.length && !busy) { fresh.forEach(r => { addMsg(r.role, r.content, r.unread ? "unread" : ""); addCards(r.cards); }); lastId = fresh[fresh.length - 1].id; }
  } catch {}
}
let busy = false;
const input = $("#input");
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = input.scrollHeight + "px"; });
input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#composer").addEventListener("submit", async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = ""; input.style.height = "auto";
  addMsg("user", text);
  busy = true; $("#send-btn").disabled = true; $("#quick").classList.add("busy");
  const typing = addMsg("assistant", "thinking…", "typing");
  try {
    const { reply, cards, paywall } = await api("/api/chat", { method: "POST", json: { message: text } });
    typing.remove();
    addMsg("assistant", reply);
    if (paywall) openPlans(reply);
    addCards(cards);
    const rows = await api("/api/chat"); lastId = rows.length ? rows[rows.length - 1].id : lastId;
  } catch (err) { typing.textContent = err.message; typing.classList.remove("typing"); }
  busy = false; $("#send-btn").disabled = false; $("#quick").classList.remove("busy");
});
$("#resume").addEventListener("change", async e => {
  const file = e.target.files[0]; if (!file) return;
  addMsg("user", `📎 ${file.name}`);
  const typing = addMsg("assistant", "reading your resume…", "typing");
  const fd = new FormData(); fd.append("file", file);
  try {
    const { reply } = await api("/api/resume", { method: "POST", body: fd });
    typing.remove(); addMsg("assistant", reply);
    refreshMe().catch(() => {});  // the "Add resume" chip goes once the profile is ready
  } catch (err) { typing.textContent = err.message; typing.classList.remove("typing"); }
  e.target.value = "";
});

/* ---------- the letter card ---------- */
const CONF_LABEL = { verified: "verified", public: "published", likely: "likely", inbox: "shared inbox", guess: "guess", user: "you added" };
const CONF_HELP = {
  verified: "A verifier confirmed this mailbox exists.",
  public: "The company publishes this address on its site or GitHub.",
  likely: "Found by a lookup, or the domain accepts all mail so it can't be confirmed.",
  inbox: "A shared inbox the founders likely read.",
  guess: "Built from the founder's name. Unverified, may bounce.",
  user: "You set this address.",
};
function letter(m) {
  const el = $("#letter-tpl").content.firstElementChild.cloneNode(true);
  const render = (m) => {
    const co = $(".co", el); co.textContent = "";
    if (m.domain) { const a = document.createElement("a"); a.href = `https://${m.domain}`; a.target = "_blank"; a.rel = "noopener"; a.textContent = m.name; co.appendChild(a); }
    else co.textContent = m.name;
    const num = document.createElement("small"); num.textContent = `#${m.match_id}`; co.append(" ", num);
    const mono = $(".mono", el); mono.textContent = (m.name || "?").trim()[0].toUpperCase();
    mono.style.setProperty("--h", [...(m.name || "")].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7));
    $(".what", el).textContent = m.one_liner || "";
    const tags = $(".tags", el); tags.textContent = "";
    const tag = (text, cls = "") => tags.append(Object.assign(document.createElement("span"), { className: `tag ${cls}`.trim(), textContent: text }));
    if (m.stage) tag(m.stage.replace(/^./, c => c.toUpperCase()), "t-stage");
    if (m.location) tag(`📍 ${m.location.replace(/,?\s*(Bengaluru|Bangalore)(,.*)?$/i, "") || "Bengaluru"}`);
    const signal = (m.signal || "").replace(/^(pre-?seed|seed)( stage)?,?\s*/i, "");  // the stage is already a tag
    if (signal) tag(signal.replace(/^./, c => c.toUpperCase()), "t-signal");
    if (m.source === "startup_map" && m.source_url) {  // credit the directory this came from
      const a = document.createElement("a"); a.href = m.source_url; a.target = "_blank"; a.rel = "noopener";
      a.className = "via"; a.textContent = "via Bangalore Startup Map";
      tags.append(a);
    }
    $(".postmark b", el).textContent = Math.round(m.score);
    $(".postmark", el).style.setProperty("--p", Math.round(m.score));
    $(".why-text", el).textContent = m.reason || "";
    $(".why", el).hidden = !m.reason;
    const o = m.outreach, env = $(".envelope", el), acts = $(".actions", el), state = $(".state", el);
    acts.textContent = ""; state.className = "state"; state.textContent = "";
    env.hidden = !o || !o.body;
    if (o && o.body) {
      $(".to-addr", el).textContent = `To: ${o.to_name ? o.to_name + " " : ""}${o.to_email || "no founder email yet"}`;
      const conf = $(".conf", el);
      conf.className = `conf ${o.to_confidence || ""}`;
      conf.textContent = CONF_LABEL[o.to_confidence] || "";
      conf.title = CONF_HELP[o.to_confidence] || "";
      const alts = $(".alts", el); alts.textContent = "";
      const others = (m.contacts || []).filter(c => c.email !== o.to_email);
      if (o.status === "draft" && others.length) {
        alts.append("Or send to ");
        others.slice(0, 3).forEach((c, i) => {
          const b = document.createElement("button"); b.type = "button";
          b.textContent = `${c.email} (${CONF_LABEL[c.confidence] || c.confidence})`;
          b.addEventListener("click", async () => render(await api(`/api/matches/${m.match_id}/draft`, { method: "PATCH", json: { to_email: c.email, to_name: c.name || "" } })));
          alts.append(b, i < Math.min(others.length, 3) - 1 ? ", " : "");
        });
      }
      $(".subj", el).textContent = o.subject;
      $(".body", el).textContent = o.body;
    }
    renderExtra(m);
    const btn = (label, cls, fn) => { const b = document.createElement("button"); b.className = `btn ${cls}`; b.textContent = label;
      b.addEventListener("click", async () => { b.disabled = true; try { await fn(); } catch (err) { state.textContent = err.message; b.disabled = false; } }); acts.appendChild(b); };
    const act = async (path, opts) => render(await api(`/api/matches/${m.match_id}/${path}`, opts || { method: "POST" }));

    // FounderReach never sends: the user sends from their own email via prefilled compose links
    const linkBtn = (label, cls, href) => { const a = document.createElement("a"); a.className = `btn ${cls}`; a.href = href;
      if (href.startsWith("http")) { a.target = "_blank"; a.rel = "noopener"; } a.textContent = label; acts.appendChild(a); };
    const copyBtn = (label, text) => btn(label, "ghost", async () => {
      try { await navigator.clipboard.writeText(text); state.textContent = "Copied. Paste it into a new email."; } catch { state.textContent = "Select the text above and copy it."; }
    });
    el.querySelector(".fu")?.remove();

    if (m.status === "skipped") { state.textContent = "Skipped"; return; }
    if (!o || o.status === "cancelled" || o.status === "failed") {
      if (o?.error) state.textContent = `Last attempt failed: ${o.error}`;
      btn("Draft email", "primary", () => act("draft"));
      btn("Skip", "ghost", () => act("skip"));
      return;
    }
    if (o.status === "draft") {
      if (o.compose) {
        state.textContent = "Send it from your own email, then tap “I sent it”.";
        linkBtn("Open in Gmail", "primary", o.compose.gmail);
        linkBtn("Email app", "ghost", o.compose.mailto);
        copyBtn("Copy", `Subject: ${o.subject}\n\n${o.body}`);
        btn("I sent it", "dark", async () => { await act("sent"); el.classList.add("sent-now"); });
      } else {
        state.textContent = "No founder email yet. Add one with Edit, or use the LinkedIn note below.";
      }
      btn("Edit", "ghost", () => editMode(m));
      btn("Rewrite", "ghost", () => act("draft"));
      btn("Skip", "ghost", () => act("skip"));
      return;
    }
    if (o.status === "replied") { state.classList.add("replied"); state.textContent = `They replied 🎉${o.reply_snippet ? `: “${o.reply_snippet}”` : ""}`; return; }
    const when = o.sent_at ? new Date(o.sent_at).toLocaleDateString() : "";
    if (o.followup_draft) {
      const fu = document.createElement("div"); fu.className = "fu";
      const h = document.createElement("h4"); h.textContent = "⏰ Time to follow up";
      const p = document.createElement("p"); p.className = "fu-body"; p.textContent = o.followup_draft;
      const tip = document.createElement("p"); tip.className = "fu-tip"; tip.textContent = "Best sent as a reply to your first email so it stays in one thread.";
      fu.append(h, p, tip); acts.before(fu);
      state.textContent = `Sent ${when}. No reply yet?`;
      if (o.followup_compose) linkBtn("Open in Gmail", "primary", o.followup_compose.gmail);
      copyBtn("Copy", o.followup_draft);
      btn("I sent the follow-up", "dark", () => act("followup-sent"));
      btn("They replied", "ghost", () => act("replied"));
      btn("Stop reminders", "ghost", () => act("stop-followups"));
      return;
    }
    const next = o.next_followup_at ? ` I'll remind you to follow up on ${new Date(o.next_followup_at).toLocaleDateString()}.` : "";
    state.textContent = `Sent ${when}.${next}`;
    btn("They replied", "ghost", () => act("replied"));
    if (o.next_followup_at) btn("Stop reminders", "ghost", () => act("stop-followups"));
  };
  const renderExtra = (m) => {
    const box = $(".extra", el), people = $(".people", el), roles = $(".roles", el), dm = $(".dm", el);
    [people, roles, dm].forEach(n => n.textContent = "");
    const link = (href, text) => { const a = document.createElement("a"); a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = text; return a; };
    const section = (host, title, items) => {
      if (!items.length) return;
      const h = document.createElement("h4"); h.textContent = title;
      const ul = document.createElement("ul"); items.forEach(li => ul.appendChild(li));
      host.append(h, ul);
    };
    section(people, "Founders", (m.founders || []).map(f => {
      const li = document.createElement("li"); li.append(`${f.name}${f.title ? ", " + f.title : ""} `);
      if (f.linkedin) li.append(link(f.linkedin, "LinkedIn"), " ");
      else if (f.linkedin_search) li.append(link(f.linkedin_search, "Find on LinkedIn"), " ");
      if (f.x) li.append(link(f.x, "X"), " ");
      if (f.profile_url) li.append(link(f.profile_url, "YC profile"));
      return li;
    }));
    section(roles, "Open roles (apply here too)", (m.jobs || []).map(j => {
      const li = document.createElement("li");
      const where = [j.location, j.remote ? "remote" : ""].filter(Boolean).join(", ");
      li.append(link(j.url, j.title), where ? ` (${where})` : "");
      return li;
    }));
    const o = m.outreach;
    if (o && o.status === "draft") {
      const h = document.createElement("h4"); h.textContent = "Message on LinkedIn or X instead";
      dm.appendChild(h);
      if (o.dm_note) {
        const p = document.createElement("p"); p.className = "dm-note"; p.textContent = o.dm_note;
        const copy = document.createElement("button"); copy.className = "btn ghost"; copy.textContent = "Copy note";
        copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(o.dm_note); copy.textContent = "Copied"; } catch { copy.textContent = "Select and copy the text"; } });
        dm.append(p, copy);
      } else {
        const b = document.createElement("button"); b.className = "btn ghost"; b.textContent = "Write a note";
        b.addEventListener("click", async () => { b.disabled = true; try { render(await api(`/api/matches/${m.match_id}/dm`, { method: "POST" })); el.querySelector(".extra").open = true; } catch (err) { b.textContent = err.message; } });
        dm.appendChild(b);
      }
    }
    box.hidden = !(people.childElementCount || roles.childElementCount || dm.childElementCount);
  };
  const editMode = (m) => {
    const subj = $(".subj", el), body = $(".body", el), to = $(".to-addr", el), acts = $(".actions", el);
    to.textContent = m.outreach.to_email || "";
    [subj, body, to].forEach(n => n.contentEditable = "true");
    body.focus();
    acts.textContent = "";
    const save = document.createElement("button"); save.className = "btn primary"; save.textContent = "Save draft";
    save.addEventListener("click", async () => {
      [subj, body, to].forEach(n => n.contentEditable = "false");
      const json = { subject: subj.textContent.trim(), body: body.innerText.trim() };
      const email = to.textContent.trim(); if (email && email !== m.outreach.to_email) json.to_email = email;
      try { m = await api(`/api/matches/${m.match_id}/draft`, { method: "PATCH", json }); render(m); }
      catch (err) { $(".state", el).textContent = err.message; }
    });
    acts.appendChild(save);
  };
  render(m);
  return el;
}

/* ---------- pipeline ---------- */
const GROUPS = [["replied", "Replied"], ["sent", "Waiting on a reply"], ["drafted", "Drafts"], ["new", "New fits"], ["skipped", "Skipped"]];
async function loadPipeline() {
  const { stats, matches } = await api("/api/dashboard");
  const s = $("#stats"); s.textContent = "";
  const parts = [[stats.sent, "sent"], [stats.replies, "replies"], [stats.drafts, "drafts"], [stats.matches, "startups matched"]];
  parts.forEach(([n, label], i) => { const b = document.createElement("b"); b.textContent = n; s.append(b, ` ${label}${i < parts.length - 1 ? ", " : ""}`); });
  const host = $("#pipe-list"); host.textContent = "";
  if (!matches.length) { const p = document.createElement("p"); p.className = "empty"; p.textContent = "No matches yet. Share your resume in Chat and ask for startups that fit."; host.appendChild(p); return; }
  for (const [status, label] of GROUPS) {
    const items = matches.filter(m => m.status === status);
    if (!items.length) continue;
    const g = document.createElement("section"); g.className = "group";
    const h = document.createElement("h2"); h.textContent = `${label} (${items.length})`;
    const l = document.createElement("div"); l.className = "list";
    items.forEach(m => l.appendChild(letter(m)));
    g.append(h, l); host.appendChild(g);
  }
}
$("#discover-btn").addEventListener("click", async e => {
  e.target.disabled = true;
  try { const r = await api("/api/admin/discover", { method: "POST" }); e.target.textContent = "Refreshing… check back in a minute"; }
  catch (err) { e.target.textContent = err.message; }
});
$("#logout-btn").addEventListener("click", () => { store.token = null; showAuth(); });
$("#delete-btn").addEventListener("click", async e => {
  const b = e.target;
  if (b.dataset.armed !== "1") {  // two taps instead of a browser dialog
    b.dataset.armed = "1"; b.textContent = "Tap again to delete everything"; setTimeout(() => { b.dataset.armed = ""; b.textContent = "Delete my account"; }, 5000);
    return;
  }
  b.disabled = true;
  try { await api("/api/me", { method: "DELETE" }); store.token = null; showAuth(); }
  catch (err) { b.textContent = err.message; b.disabled = false; }
});

/* ---------- email confirmation banner ---------- */
function banner(text, kind = "", resend = false) {
  $("#banner").hidden = !text;
  $("#banner").className = `banner ${kind}`.trim();
  $("#banner-text").textContent = text || "";
  $("#resend-btn").hidden = !resend;
}
$("#resend-btn").addEventListener("click", async e => {
  const b = e.target; b.disabled = true;
  try { await api("/api/auth/resend-verification", { method: "POST" }); b.textContent = "Sent ✓"; }
  catch (err) { b.textContent = err.message; }
  setTimeout(() => { b.disabled = false; b.textContent = "Resend"; }, 60000);
});
let justVerified = null;  // "1" or "expired", from the confirmation link's redirect
{
  const v = new URLSearchParams(location.search).get("verified");
  if (v) { justVerified = v; history.replaceState({}, "", "/"); }
}

/* ---------- trial & passes (PhonePe) ---------- */
let billing = null;
const fmtDate = iso => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
function renderBilling(b) {
  billing = b;
  const pill = $("#plan-pill");
  pill.hidden = !b?.payments;
  if (pill.hidden) return;
  pill.classList.toggle("locked", !b.access);
  pill.classList.toggle("paid", !!b.pass_until);
  if (b.pass_until) pill.textContent = `Pass · till ${fmtDate(b.pass_until)}`;
  else if (!b.access) pill.textContent = "Get a pass";
  else if (b.trial_ends) {
    const hrs = Math.max(0, (new Date(b.trial_ends) - Date.now()) / 36e5);
    const left = hrs >= 24 ? `${Math.ceil(hrs / 24)}d` : `${Math.ceil(hrs)}h`;
    pill.textContent = `Trial · ${left} · ${b.trial_drafts_left} drafts`;
  } else pill.textContent = `Free trial`;
}
function openPlans(why) {
  if (!billing?.payments) return;
  $("#plans-why").textContent = why || "New matches every morning, founder emails, drafts in your voice and follow-up reminders.";
  $("#plans-error").textContent = "";
  const grid = $("#plan-grid"); grid.textContent = "";
  for (const p of billing.plans) {
    const best = p.days >= 30;
    const card = el("div", `plan${best ? " best" : ""}`);
    if (best) card.append(el("span", "plan-tag", "Best value"));
    const price = el("p", "plan-price"); price.append(`₹${p.price}`, el("small", "", ` / ${p.days} days`));
    const buy = el("button", `btn ${best ? "primary" : "dark"}`, `Pay ₹${p.price} with PhonePe`); buy.type = "button";
    buy.addEventListener("click", async () => {
      grid.querySelectorAll("button").forEach(b => b.disabled = true);
      buy.textContent = "Opening PhonePe…";
      try {
        const { redirect_url } = await api("/api/pay", { method: "POST", json: { plan: p.id } });
        if (!/^https:\/\/[^/]*phonepe\.com\//.test(redirect_url)) throw new Error("Unexpected payment page. Please try again.");
        location.assign(redirect_url);
      } catch (err) {
        $("#plans-error").textContent = err.message;
        grid.querySelectorAll("button").forEach(b => b.disabled = false);
        buy.textContent = `Pay ₹${p.price} with PhonePe`;
      }
    });
    card.append(el("h3", "", p.label), price, el("p", "plan-per", `about ₹${Math.round(p.price / p.days)} a day`), buy);
    grid.appendChild(card);
  }
  const d = $("#plans");
  if (!d.open) d.showModal();
}
$("#plan-pill").addEventListener("click", () => openPlans(billing?.pass_until
  ? `Your pass runs till ${fmtDate(billing.pass_until)}. Buying another adds to it.` : ""));
$("#plans-close").addEventListener("click", () => $("#plans").close());
$("#plans").addEventListener("click", e => { if (e.target.id === "plans") $("#plans").close(); });  // tap outside

// Back from PhonePe: /pay/return sends people to /?paid=<order>. Ask our server (which asks PhonePe) what happened.
let paidOrder = null;
{
  const o = new URLSearchParams(location.search).get("paid");
  if (o) { paidOrder = o; history.replaceState({}, "", "/"); }
}
async function checkPayment(order) {
  banner("Checking your payment with PhonePe…");
  for (let i = 0; i < 10; i++) {
    let r;
    try { r = await api(`/api/pay/${encodeURIComponent(order)}/check`, { method: "POST" }); }
    catch (err) { banner(err.message, "warn"); return; }
    renderBilling(r.billing);
    if (r.state === "COMPLETED" && r.billing.pass_until) {
      banner(`Payment received ✓ Your pass is active till ${fmtDate(r.billing.pass_until)}. Happy hunting!`, "ok"); return;
    }
    if (r.state === "FAILED") {
      banner("The payment didn't go through, so no pass was added. If money left your account, write to us from the Contact page.", "warn"); return;
    }
    await new Promise(res => setTimeout(res, 3000));
  }
  banner("PhonePe is still confirming your payment. Your pass turns on by itself once it does; check back in a few minutes.");
}

/* ---------- account ---------- */
async function refreshMe() {
  const me = await api("/api/me");
  myName = me.name || "";
  $("#me-avatar").textContent = (me.name || me.email || "?").trim()[0].toUpperCase();
  quick(me.profile);
  renderBilling(me.billing);
  $("#discover-btn").hidden = !me.is_admin;  // refreshing spends AI credits: admin only
  if (me.email_verified) banner(justVerified === "1" ? "Email confirmed ✓ Founder emails and drafts are unlocked." : "", "ok");
  else if (justVerified === "expired") banner("That confirmation link has expired. Send yourself a new one:", "warn", true);
  else if (!me.can_confirm) banner(`To see founder emails and drafts, sign out and continue with Google using ${me.email}.`, "");
  else banner(`Confirm your email to see founder emails and drafts. We sent a link to ${me.email}.`, "", true);
  justVerified = null;
}

/* ---------- boot ---------- */
async function boot() {
  if (!store.token) return showAuth();
  if (new URLSearchParams(location.search).has("home")) {  // the logo in the app: show the home page, stay signed in
    history.replaceState({}, "", "/#top");
    document.body.classList.add("signed-in");
    $(".nav-cta [data-goto=signup]").textContent = "Open my chat →";
    return showAuth();
  }
  $("#auth").hidden = true; $("#app").hidden = false;
  try { await refreshMe(); await loadChat(); } catch { return; }
  if (paidOrder) { const o = paidOrder; paidOrder = null; checkPayment(o); }
}
setInterval(poll, 30000);
boot();
