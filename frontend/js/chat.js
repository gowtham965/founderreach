// The chat: messages, startup cards in the conversation, suggestion chips, the composer and resume upload.
import { $, api, el, store } from "./core.js";
import { letter } from "./cards.js";
import { openPlans } from "./billing.js";
import { refreshMe } from "./shell.js";

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
export const setMyName = name => { myName = name; };
function hello() {  // the top of the chat: a namaskara, like the landing page
  const li = el("li", "hello");
  const k = el("span", "kn namaskara"); k.append(el("span", "flag", "ನಮಸ್ಕಾರ"));
  li.append(k, el("h2", "", myName ? `Hi ${myName.split(" ")[0]}, let's find your next startup.` : "Let's find your next startup."),
    el("p", "", "Seed-stage teams in Namma Bengaluru that just raised, and the founder's inbox for each."));
  const clear = el("button", "clear-chat", "Clear chat"); clear.type = "button";
  clear.addEventListener("click", async () => {
    if (clear.dataset.armed !== "1") {  // two taps instead of a browser dialog
      clear.dataset.armed = "1";
      clear.textContent = "Tap again to clear. Your matches and drafts stay in Pipeline.";
      setTimeout(() => { clear.dataset.armed = ""; clear.textContent = "Clear chat"; }, 5000);
      return;
    }
    clear.disabled = true;
    try { await api("/api/chat", { method: "DELETE" }); await loadChat(); }
    catch (err) { clear.textContent = err.message; clear.disabled = false; }
  });
  li.append(clear);
  list.appendChild(li);
}
const QUICK = [
  ["📎 Add resume", () => $("#resume").click(), p => !p?.ready],
  ["Find startups for me", "Find startups that fit me"],
  ["Only fintech", "Show me fintech startups"],
  ["Draft to my top match", "Draft an email to my best match"],
  ["What's due?", "Anything I should follow up on?"],
];
export function quick(profile) {
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
export async function loadChat() {
  list.textContent = "";
  hello();
  const rows = await api("/api/chat");
  list.querySelector(".clear-chat").hidden = !rows.length;  // nothing to clear yet
  rows.forEach(r => { addMsg(r.role, r.content, r.unread ? "unread" : ""); addCards(r.cards); });
  lastId = rows.length ? rows[rows.length - 1].id : 0;
}
export async function poll() {
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
    const { reply, paywall } = await api("/api/resume", { method: "POST", body: fd });
    typing.remove(); addMsg("assistant", reply);
    if (paywall) openPlans(reply);
    refreshMe().catch(() => {});  // the "Add resume" chip goes once the profile is ready
  } catch (err) { typing.textContent = err.message; typing.classList.remove("typing"); }
  e.target.value = "";
});
