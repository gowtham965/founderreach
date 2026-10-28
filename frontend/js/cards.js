// The startup card ("letter"): what they do, why it fits, research, the draft and its buttons, proof of work.
// Used in the chat and in the Pipeline.
import { $, api } from "./core.js";

const CONF_LABEL = { verified: "verified", public: "published", likely: "likely", inbox: "shared inbox", guess: "guess", user: "you added" };
const CONF_HELP = {
  verified: "A verifier confirmed this mailbox exists.",
  public: "The company publishes this address on its site or GitHub.",
  likely: "Found by a lookup, or the domain accepts all mail so it can't be confirmed.",
  inbox: "A shared inbox the founders likely read.",
  guess: "Built from the founder's name. Unverified, may bounce.",
  user: "You set this address.",
};
export function letter(m) {
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
    const jobs = m.jobs || [];
    if (jobs.length) {  // open roles from their job board or careers page (refreshed nightly)
      const days = jobs[0].posted ? Math.max(0, Math.round((Date.now() - new Date(jobs[0].posted)) / 864e5)) : null;
      const when = days === null ? "" : days === 0 ? " · posted today" : ` · posted ${days}d ago`;
      const total = m.open_roles || jobs.length;
      tag(`Hiring: ${jobs[0].title}${total > 1 ? ` +${total - 1} more` : ""}${when}`, "t-hiring");
    }
    if (m.source === "startup_map" && m.source_url) {  // credit the directory this came from
      const a = document.createElement("a"); a.href = m.source_url; a.target = "_blank"; a.rel = "noopener";
      a.className = "via"; a.textContent = "via Bangalore Startup Map";
      tags.append(a);
    }
    $(".postmark b", el).textContent = Math.round(m.score);
    $(".postmark", el).style.setProperty("--p", Math.round(m.score));
    $(".why-text", el).textContent = m.reason || "";
    $(".why", el).hidden = !m.reason;
    renderFound(m);
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
    renderPow(m);
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
      if (!m.pow) btn("💡 Make something for them", "ghost", () => act("pow"));
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
      if (!m.pow) btn("💡 Make something for them", "ghost", () => act("pow"));
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
  const renderPow = (m) => {  // proof of work: 3 ideas → a starter → their link goes into the email
    const box = $(".pow", el), p = m.pow;
    box.textContent = "";
    box.hidden = !p || !(p.ideas || []).length;
    if (box.hidden) return;
    const make = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
    const busy = (b, label, fn) => b.addEventListener("click", async () => {
      const was = b.textContent; b.disabled = true; b.textContent = label;
      try { render(await fn()); } catch (err) { b.disabled = false; b.textContent = was; $(".state", el).textContent = err.message; }
    });
    box.append(make("h4", "", "💡 Make something for them"),
               make("p", "pow-tip", "Founders reply far more to “I made this for you” than to a CV. Pick one:"));
    p.ideas.forEach((idea, i) => {
      const card = make("div", `pow-idea${p.chosen === i ? " chosen" : ""}`);
      const head = make("p", "pow-title"); head.append(make("b", "", idea.title), make("span", "pow-effort", idea.effort || ""));
      card.append(head, make("p", "", idea.what));
      if (idea.why) card.append(make("p", "pow-why", idea.why));
      if (p.chosen === i) card.append(make("span", "pow-picked", "Your pick ✓"));
      else {
        const b = make("button", "btn ghost", p.chosen == null ? "Start this" : "Switch to this"); b.type = "button";
        busy(b, "Writing a starter…", () => api(`/api/matches/${m.match_id}/pow/${i}`, { method: "POST" }));
        card.append(b);
      }
      box.append(card);
    });
    if (!p.starter) return;
    const st = make("div", "pow-starter");
    st.append(make("p", "pow-label", "Your starter (fill in the [brackets] by using their product)"), make("div", "pow-text", p.starter));
    const copy = make("button", "btn ghost", "Copy starter"); copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(p.starter); copy.textContent = "Copied ✓"; } catch { copy.textContent = "Select the text and copy it"; }
    });
    st.append(copy);
    if (p.link) {
      const done = make("p", "pow-done", "Added to your email ✓ ");
      const a = make("a", "", p.link); a.href = p.link; a.target = "_blank"; a.rel = "noopener"; done.append(a);
      st.append(done);
    } else {
      const row = make("div", "pow-link");
      const inp = make("input"); inp.type = "url"; inp.placeholder = "When it's ready, paste its link (Google Doc, Notion, Loom, GitHub)";
      const add = make("button", "btn primary", "Add to my email"); add.type = "button";
      busy(add, "Rewriting your email…", () => api(`/api/matches/${m.match_id}/pow-link`, { method: "POST", json: { link: inp.value.trim() } }));
      row.append(inp, add); st.append(row);
    }
    box.append(st);
  };
  const renderFound = (m) => {  // the research agent's brief: facts the draft may lean on, each with its source
    const r = m.research, box = $(".found", el), body = $(".found-body", el);
    body.textContent = "";
    box.hidden = !r || !(r.what_they_do || (r.recent || []).length);
    if (box.hidden) return;
    if (r.what_they_do) { const p = document.createElement("p"); p.className = "found-what"; p.textContent = r.what_they_do; body.append(p); }
    const ul = document.createElement("ul");
    for (const f of r.recent || []) {
      const li = document.createElement("li"); li.append(f.fact, " ");
      const a = document.createElement("a"); a.href = f.source; a.target = "_blank"; a.rel = "noopener";
      try { a.textContent = new URL(f.source).hostname.replace(/^www\./, ""); } catch { a.textContent = "source"; }
      li.append(a); ul.appendChild(li);
    }
    if (ul.children.length) body.append(ul);
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
