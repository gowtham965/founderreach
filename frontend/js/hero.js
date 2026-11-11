// The home page's hero: the animated example (phones play it once) and the real "What do you do?" search.
import { $, api, el } from "./core.js";
import { goToForm } from "./landing.js";

const sleep = ms => new Promise(r => setTimeout(r, ms));
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const SQ_COLORS = ["#6b3fe4", "#17141f", "#9a5b13", "#2f5d8a", "#a3195b", "#3d6b5a"];
const sqColor = name => SQ_COLORS[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % SQ_COLORS.length];

const DEMO = [
  { name: "Kirana AI", tag: "raised 2 days ago", m: "HSR Layout, Seed ₹12 Cr. AI for kirana stores", who: "Priya Sharma, CEO, priya@kirana.ai", fit: 92 },
  { name: "Ledgerline", m: "Indiranagar, Seed $2M. Fintech APIs", who: "Arjun Mehta, CTO, arjun@ledgerline.io", fit: 87 },
  { name: "Tiffin Labs", m: "Koramangala, Pre-seed. Cloud kitchens OS", who: "Rohan Iyer, Founder, rohan@tiffin.in", fit: 81 },
];
const DEMO_QUERY = "backend, python, fintech";
const DEMO_BODY = "Hi Priya, congrats on the seed round. I built payment reconciliation at Acme that cut manual work 70%, and Kirana AI's ledger problem looks familiar. 15 minutes next week?";

const exampleLabel = () => el("p", "ex-row", "Example. Type your skills above to see real startups.");
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
// Reserve the example's full height before the first paint: the cards appearing one by one then fill space
// that's already there instead of pushing the rest of the page down (phones stack it above everything).
function reserveDemoSpace() {
  const res = $("#results");
  if (res.style.minHeight) return;
  res.append(exampleLabel(), ...DEMO.map((d, i) => previewCard({ ...d, blur: true }, i)));
  res.style.minHeight = `${res.offsetHeight}px`;
  res.textContent = "";
}
async function demoLoop() {
  const input = $("#try-input"), res = $("#results"), stage = $("#stage");
  reserveDemoSpace();
  const wide = () => matchMedia("(min-width: 980px)").matches;
  while (demoOn) {
    if ($("#auth").hidden || document.hidden) { await sleep(1000); continue; }
    if (!demoOn) break;
    // keep the box's height while it refills: on phones it sits above the rest of the page, and an emptied box
    // made everything below jump ~500px mid-scroll
    if (res.offsetHeight) res.style.minHeight = `${res.offsetHeight}px`;
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
    if (!wide()) break;  // phones: play it once and leave the example showing
    await sleep(3200); if (!demoOn) break;
    input.value = "";
  }
}
let demoVisible = true;  // the example (animated or still) is on screen
function stopDemo() {
  if (!demoVisible) return;
  demoVisible = false;
  demoOn = false;
  $("#results").style.minHeight = "";  // their own results take the space they need
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
  const q = (demoVisible ? DEMO_QUERY : $("#try-input").value).replace(/[·,]/g, " ").trim(), res = $("#results");
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
      m: [r.area, r.what].filter(Boolean).join(". "),
      // never real founder data: a placeholder shape, blurred
      who: r.has_founder ? "Founder name · founder@company.com" : "Founder details after sign-up", blur: true,
    }, i);
    if (r.map_url) {  // credit the directory this startup came from, with a link to its page there
      const via = Object.assign(el("a", "via", "via Bangalore Startup Map"), { href: r.map_url, target: "_blank", rel: "noopener" });
      c.querySelector(".m").append(". ", via);
    }
    res.append(c); setTimeout(() => c.classList.add("in"), 40 + i * 140);
  });
  const lock = el("button", "pv lockrow in", "Sign up free to see the founders and their emails");
  lock.type = "button"; lock.addEventListener("click", () => goToForm("signup"));
  res.append(lock);
  $("#try-hint").textContent = "Real Bangalore startups from today's pool. Founders unlock when you sign up.";
});

export function startDemo() {
  if (reduceMotion) { demoOn = false; showDemoFinal(); } else demoLoop();
}
