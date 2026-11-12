// The Pipeline tab: every match grouped by status, plus sign out and delete account.
import { $, api, store } from "./core.js";
import { letter } from "./cards.js";
import { showAuth } from "./auth.js";

const GROUPS = [["replied", "Replied"], ["sent", "Waiting on a reply"], ["drafted", "Drafts"], ["new", "New fits"], ["skipped", "Skipped"]];
export async function loadPipeline() {
  const { stats, matches } = await api("/api/dashboard");
  const s = $("#stats"); s.textContent = "";
  const parts = [[stats.sent, "sent"], [stats.replies, "replies"], [stats.drafts, "drafts"], [stats.matches, "startups matched"]];
  for (const [n, label] of parts) {
    const d = document.createElement("div"), b = document.createElement("b"), t = document.createElement("span");
    b.textContent = n; t.textContent = label; d.append(b, t); s.append(d);
  }
  const host = $("#pipe-list"); host.textContent = "";
  if (!matches.length) { const p = document.createElement("p"); p.className = "empty"; p.textContent = "No matches yet. Share your resume in Chat and ask for startups that fit."; host.appendChild(p); return; }
  for (const [status, label] of GROUPS) {
    const items = matches.filter(m => m.status === status);
    if (!items.length) continue;
    const g = document.createElement("section"); g.className = "group";
    const h = document.createElement("h2"); h.textContent = `${label} (${items.length})`;
    const l = document.createElement("div"); l.className = "list";
    items.forEach(m => l.appendChild(letter(m, { route: true })));
    g.append(h, l); host.appendChild(g);
  }
}
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
