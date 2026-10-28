// Admin tab, Startups: browse and filter the database, data quality, edit / add / hide, CSV export and import.
import { $, api, mk, store } from "./core.js";
import { ago, table } from "./admin.js";

document.querySelectorAll(".admin-seg button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".admin-seg button").forEach(x => x.setAttribute("aria-selected", x === b));
  $("#admin-overview").hidden = b.dataset.view !== "overview";
  $("#admin-startups").hidden = b.dataset.view !== "startups";
  if (b.dataset.view === "startups") loadStartups();
}));
const ISSUES = { no_founder_email: "No founder email", no_founder: "No founder name", no_website: "No website", not_checked: "Not looked up yet" };
const stState = { q: "", stage: "", city: "", source: "", focus: "", issue: "", hidden: "", sort: "new", hiring: "", page: 1 };
let stTimer = null, stRows = [], stEditing = null;
const fillSelect = (sel, counts, label) => {
  const keep = sel.value, first = sel.options[0];
  sel.textContent = ""; sel.append(first);
  Object.entries(counts).sort((a, b) => b[1] - a[1]).forEach(([k, n]) => sel.append(new Option(`${label ? label(k) : k} (${n})`, k)));
  sel.value = keep;
};
async function loadStartups() {
  const qs = new URLSearchParams(Object.entries(stState).filter(([, v]) => v !== "" && v != null));
  let d;
  try { d = await api(`/api/admin/startups?${qs}`); } catch (err) { $("#st-msg").textContent = err.message; return; }
  stRows = d.rows;
  const s = d.summary;
  $("#st-summary").textContent = `${s.all} startups in the database · ${s.in_focus} Bangalore seed (what users see) · ` +
    `${s.added_7d} added this week · ${s.hiring_focus} of the Bangalore seed ones hiring · ${s.hidden} hidden`;
  const box = $("#st-issues"); box.textContent = "";
  for (const [k, label] of Object.entries(ISSUES)) {
    const b = mk("button", `qchip${stState.issue === k ? " on" : ""}`, `${label} · ${s.issues[k] || 0}`); b.type = "button";
    b.addEventListener("click", () => { stState.issue = stState.issue === k ? "" : k; stState.page = 1; loadStartups(); });
    box.append(b);
  }
  if (stState.issue && stState.issue !== "no_website") {
    const r = mk("button", "btn ghost", "Look up 20 of these again"); r.type = "button";
    r.addEventListener("click", async () => {
      r.disabled = true;
      try { r.textContent = (await api("/api/admin/startups/recheck", { method: "POST", json: { issue: stState.issue } })).message; }
      catch (err) { r.textContent = err.message; }
    });
    box.append(r);
  }
  $("#st-quality-note").textContent = "Counts are for visible startups. Re-checks can use Hunter lookups.";
  fillSelect($("#st-stage"), s.stages); fillSelect($("#st-city"), s.cities); fillSelect($("#st-source"), s.sources);
  table($("#st-tbl"), ["Startup", "Stage", "Where", "Source", "Founder email", "Open roles", "Matched", "Added"], d.rows.map(r => {
    const who = mk("div"); who.append(mk("b", "", r.name), r.domain ? mk("small", "", ` ${r.domain}`) : "",
      r.hidden ? mk("small", "warn-t", " hidden") : "", r.in_focus ? "" : mk("small", "", " · outside focus"));
    return [who, r.stage || "?", (r.location || "?").slice(0, 40), r.source || "?", r.founder_email ? "✓" : "—", r.jobs_checked_at ? r.open_roles : "not checked", r.matches,
            r.discovered_at ? ago(r.discovered_at) : "?"];
  }));
  [...$("#st-tbl").tBodies[0].rows].forEach((tr, i) => { if (stRows[i]) { tr.classList.add("click"); tr.addEventListener("click", () => openStartup(stRows[i])); } });
  const pages = Math.max(1, Math.ceil(d.total / d.per_page));
  $("#st-page").textContent = `${d.total} found · page ${d.page} of ${pages}`;
  $("#st-prev").disabled = d.page <= 1; $("#st-next").disabled = d.page >= pages;
}
for (const [id, key] of [["#st-focus", "focus"], ["#st-stage", "stage"], ["#st-city", "city"], ["#st-source", "source"], ["#st-hidden", "hidden"], ["#st-sort", "sort"], ["#st-hiring", "hiring"]])
  $(id).addEventListener("change", e => { stState[key] = e.target.value; stState.page = 1; loadStartups(); });
$("#st-q").addEventListener("input", e => { stState.q = e.target.value; stState.page = 1; clearTimeout(stTimer); stTimer = setTimeout(loadStartups, 300); });
$("#st-prev").addEventListener("click", () => { stState.page--; loadStartups(); });
$("#st-next").addEventListener("click", () => { stState.page++; loadStartups(); });

function openStartup(r) {  // r = a row, or null to add a new one
  stEditing = r;
  const f = $("#st-form"); f.reset();
  $("#st-dlg-h").textContent = r ? r.name : "Add a startup";
  $("#st-err").textContent = "";
  if (r) {
    for (const k of ["name", "domain", "one_liner", "stage", "location", "signal", "source_url"]) f.elements[k].value = r[k] || "";
    f.elements.tags.value = (r.tags || []).join(", ");
    const fd = (r.founders || [])[0] || {};
    f.elements.founder_name.value = fd.name || ""; f.elements.founder_title.value = fd.title || "";
    f.elements.founder_email.value = r.founder_email || "";
  }
  const info = $("#st-info"); info.textContent = "";
  if (r) {
    const line = (label, text) => { const p = mk("p"); p.append(mk("b", "", `${label}: `), text); info.append(p); };
    line("Source", `${r.source || "?"}${r.edited.length ? ` · you edited: ${r.edited.join(", ")} (the morning refresh won't overwrite these)` : ""}`);
    if ((r.founders || []).length) line("Founders found", r.founders.map(x => `${x.name}${x.title ? ` (${x.title})` : ""}`).join(", "));
    if ((r.contacts || []).length) line("Emails found", r.contacts.slice(0, 5).map(c => `${c.email} [${c.confidence}]`).join(", "));
    if (r.research?.what_they_do) line("Research", r.research.what_they_do);
    line("Matched with", `${r.matches} user${r.matches === 1 ? "" : "s"}`);
    if (r.hidden) line("Hidden", r.hidden_reason || "no reason given");
  }
  $("#st-recheck").hidden = $("#st-hide").hidden = $("#st-hide-reason").hidden = !r;
  if (r) { $("#st-hide").textContent = r.hidden ? "Show to users again" : "Hide from users"; $("#st-hide-reason").hidden = r.hidden; $("#st-hide-reason").value = ""; }
  $("#st-recheck").textContent = "Look up founders & research again"; $("#st-recheck").disabled = false;
  $("#st-dlg").showModal();
}
async function saveStartup(extra = {}) {
  const f = $("#st-form"), body = {};
  for (const k of ["name", "domain", "one_liner", "stage", "location", "signal", "source_url", "founder_name", "founder_title", "founder_email"]) {
    const v = f.elements[k].value.trim();
    const was = !stEditing ? "" : k === "founder_name" ? (stEditing.founders?.[0]?.name || "") : k === "founder_title" ? (stEditing.founders?.[0]?.title || "")
      : k === "founder_email" ? (stEditing.founder_email || "") : (stEditing[k] || "");
    if (v !== was) body[k] = v;  // send only what changed, so untouched fields stay discovery's to update
  }
  const tags = f.elements.tags.value.split(",").map(t => t.trim()).filter(Boolean);
  if (!stEditing || tags.join(",") !== (stEditing.tags || []).join(",")) body.tags = tags;
  Object.assign(body, extra);
  if (!stEditing && !body.name) { $("#st-err").textContent = "A startup needs a name"; return; }
  try {
    const saved = stEditing ? await api(`/api/admin/startups/${stEditing.id}`, { method: "PATCH", json: body })
                            : await api("/api/admin/startups", { method: "POST", json: body });
    $("#st-dlg").close(); $("#st-msg").textContent = `Saved ${saved.name} ✓`; loadStartups();
  } catch (err) { $("#st-err").textContent = err.message; }
}
$("#st-add").addEventListener("click", () => openStartup(null));
$("#st-close").addEventListener("click", () => $("#st-dlg").close());
$("#st-save").addEventListener("click", () => saveStartup());
$("#st-hide").addEventListener("click", () => saveStartup(stEditing.hidden ? { hidden: false } : { hidden: true, hidden_reason: $("#st-hide-reason").value.trim() }));
$("#st-recheck").addEventListener("click", async e => {
  const b = e.target; b.disabled = true;
  try { b.textContent = (await api(`/api/admin/startups/${stEditing.id}/recheck`, { method: "POST" })).message; }
  catch (err) { b.textContent = err.message; b.disabled = false; }
});
async function download(path, fallback) {  // signed-in file download (the token isn't a cookie, so no plain link)
  const res = await fetch(path, { headers: { Authorization: `Bearer ${store.token}` } });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Download failed (${res.status})`);
  const name = (res.headers.get("content-disposition") || "").match(/filename="?([^"]+)"?/)?.[1] || fallback;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
$("#st-export").addEventListener("click", async () => {
  try { await download("/api/admin/startups.csv", "founderreach-startups.csv"); $("#st-msg").textContent = "CSV downloaded ✓"; }
  catch (err) { $("#st-msg").textContent = err.message; }
});
$("#st-import").addEventListener("change", async e => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  $("#st-msg").textContent = "Importing…";
  try {
    const r = await api("/api/admin/startups/import", { method: "POST", body: fd });
    $("#st-msg").textContent = `Imported: ${r.added} added, ${r.updated} updated${r.errors.length ? `, ${r.errors.length} skipped (${r.errors[0]}${r.errors.length > 1 ? "…" : ""})` : ""}`;
    loadStartups();
  } catch (err) { $("#st-msg").textContent = err.message; }
  e.target.value = "";
});
