// Admin tab, overview: totals, the 30-day chart, the funnel, users and payments, and the admin buttons.
import { $, api, mk, store } from "./core.js";
import { loadChat } from "./chat.js";

export const ago = iso => {
  if (!iso) return "never";
  const m = (Date.now() - new Date(iso)) / 6e4;
  return m < 60 ? `${Math.max(1, Math.round(m))}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
};
export function table(tbl, heads, rows) {
  tbl.textContent = "";
  const tr = mk("tr"); heads.forEach(h => tr.append(mk("th", "", h))); tbl.append(mk("thead")); tbl.tHead.append(tr);
  const body = mk("tbody");
  for (const cells of rows) {
    const r = mk("tr");
    for (const c of cells) { const td = mk("td"); td.append(c instanceof Node ? c : String(c)); r.append(td); }
    body.append(r);
  }
  if (!rows.length) { const r = mk("tr"); const td = mk("td", "muted", "Nothing yet"); td.colSpan = heads.length; r.append(td); body.append(r); }
  tbl.append(body);
}
function chart(series) {  // sign-up bars + active line, plain SVG (no chart library)
  const W = 720, H = 180, P = 24, n = series.length, max = Math.max(1, ...series.map(d => Math.max(d.signups, d.active)));
  const x = i => P + (i + 0.5) * (W - 2 * P) / n, y = v => H - P - v * (H - 2 * P) / max, bw = (W - 2 * P) / n * 0.6;
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "chart");
  const add = (tag, attrs, title) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (title) { const t = document.createElementNS(ns, "title"); t.textContent = title; e.append(t); } svg.append(e); return e; };
  add("line", { x1: P, x2: W - P, y1: H - P, y2: H - P, class: "axis" });
  add("text", { x: P, y: P - 8, class: "lbl" }).textContent = `max ${max}`;
  series.forEach((d, i) => add("rect", { x: x(i) - bw / 2, y: y(d.signups), width: bw, height: Math.max(0, H - P - y(d.signups)), rx: 2, class: "bar" },
                               `${d.day}: ${d.signups} sign-ups, ${d.active} active`));
  add("polyline", { points: series.map((d, i) => `${x(i)},${y(d.active)}`).join(" "), class: "line" });
  [0, n - 1].forEach(i => add("text", { x: x(i), y: H - 6, class: "lbl", "text-anchor": i ? "end" : "start" }).textContent = series[i].day.slice(5));
  $("#chart").textContent = ""; $("#chart").append(svg);
}
let adminQ = "", adminTimer = null;
export async function loadAdmin() {
  let d;
  try { d = await api(`/api/admin/overview?q=${encodeURIComponent(adminQ)}`); } catch (err) { $("#kpis").textContent = err.message; return; }
  const k = $("#kpis"); k.textContent = "";
  for (const [key, label] of [["today", "Today"], ["7d", "Last 7 days"], ["30d", "Last 30 days"]]) {
    const w = d.windows[key], card = mk("div", "kpi");
    card.append(mk("h4", "", label));
    for (const [v, l] of [[w.signups, "sign-ups"], [w.active, "active"], [w.paying, "paying"], [`₹${w.revenue_inr}`, "revenue"], [`₹${w.ai_cost_inr}`, "AI cost"]]) {
      const p = mk("p"); p.append(mk("b", "", String(v)), ` ${l}`); card.append(p);
    }
    k.append(card);
  }
  chart(d.series);
  const f = $("#funnel"); f.textContent = "";
  const top = Math.max(1, d.funnel[0].users);
  d.funnel.forEach(s => {
    const row = mk("div", "fstep"), track = mk("span", "ftrack"), bar = mk("span", "fbar");
    bar.style.width = `${Math.max(2, s.users / top * 100)}%`;
    track.append(bar);
    row.append(mk("span", "fname", s.step), track, mk("span", "fnum", `${s.users} · ${Math.round(s.users / top * 100)}%`));
    f.append(row);
  });
  $("#users-h").textContent = `Users (${d.users_total})`;
  table($("#users-tbl"), ["User", "Joined", "Via", "Last seen", "Status", "Matches · drafts · sent · replies", "Paid"],
    d.users.map(u => {
      const who = mk("div"); who.append(mk("b", "", u.email), u.name ? mk("small", "", ` ${u.name}`) : "", u.confirmed ? "" : mk("small", "warn-t", " unconfirmed"));
      return [who, new Date(u.joined).toLocaleDateString("en-IN", { day: "numeric", month: "short" }), u.via, ago(u.last_seen), u.status,
              `${u.matches} · ${u.drafts} · ${u.sent} · ${u.replies}`, u.paid_inr ? `₹${u.paid_inr}` : "—"];
    }));
  table($("#pay-tbl"), ["When", "User", "Pass", "Amount", "Status", "Gateway"],
    d.payments.map(p => [ago(p.at), p.email, p.plan === "month" ? "30 days" : "7 days", `₹${p.amount_inr}`, p.state, p.gateway]));
}
$("#user-q").addEventListener("input", e => { adminQ = e.target.value; clearTimeout(adminTimer); adminTimer = setTimeout(loadAdmin, 300); });
$("#alert-btn").addEventListener("click", async e => {
  const b = e.target; b.disabled = true;
  try { await api("/api/admin/test-alert", { method: "POST" }); b.textContent = "Test alert sent ✓ check your phone"; }
  catch (err) { b.textContent = err.message; }
  b.disabled = false;
});
$("#backup-btn").addEventListener("click", async e => {  // admin: a fresh database copy, saved on this computer
  const b = e.target; b.disabled = true; b.textContent = "Making a backup…";
  try {
    const res = await fetch("/api/admin/backup", { headers: { Authorization: `Bearer ${store.token}` } });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Backup failed (${res.status})`);
    const name = (res.headers.get("content-disposition") || "").match(/filename="?([^"]+)"?/)?.[1] || "founderreach-backup.db.gz";
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    b.textContent = "Backup downloaded ✓";
  } catch (err) { b.textContent = err.message; }
  b.disabled = false;
});
$("#ops-btn").addEventListener("click", async e => {  // admin: today's ops report lands in the chat
  const b = e.target; b.disabled = true; b.textContent = "Writing the report…";
  try {
    await api("/api/admin/ops-report", { method: "POST" });
    b.textContent = "Ops report";
    document.querySelector("[data-tab=chat]").click(); await loadChat();
  } catch (err) { b.textContent = err.message; }
  b.disabled = false;
});
$("#roles-btn").addEventListener("click", async e => {
  e.target.disabled = true;
  try { e.target.textContent = (await api("/api/admin/refresh-roles", { method: "POST" })).message; }
  catch (err) { e.target.textContent = err.message; e.target.disabled = false; }
});
$("#discover-btn").addEventListener("click", async e => {
  e.target.disabled = true;
  try { const r = await api("/api/admin/discover", { method: "POST" }); e.target.textContent = "Refreshing… check back in a minute"; }
  catch (err) { e.target.textContent = err.message; }
});
