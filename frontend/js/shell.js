// The signed-in app around the tabs: switching tabs, the account (badge, banner), and starting up.
import { $, api, banner, store } from "./core.js";
import { showAuth } from "./auth.js";
import { loadChat, quick, setMyName } from "./chat.js";
import { loadPipeline } from "./pipeline.js";
import { loadAdmin } from "./admin.js";
import { checkPayment, renderBilling } from "./billing.js";
import { clearVerified, takePaidOrder, verified } from "./urlstate.js";

document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(x => x.setAttribute("aria-selected", x === b));
  $("#chat").hidden = b.dataset.tab !== "chat";
  $("#pipeline").hidden = b.dataset.tab !== "pipeline";
  $("#admin").hidden = b.dataset.tab !== "admin";
  if (b.dataset.tab === "pipeline") loadPipeline();
  if (b.dataset.tab === "admin") loadAdmin();
}));


$("#resend-btn").addEventListener("click", async e => {
  const b = e.target; b.disabled = true;
  try { await api("/api/auth/resend-verification", { method: "POST" }); b.textContent = "Sent ✓"; }
  catch (err) { b.textContent = err.message; }
  setTimeout(() => { b.disabled = false; b.textContent = "Resend"; }, 60000);
});

export async function refreshMe() {
  const me = await api("/api/me");
  setMyName(me.name || "");
  $("#me-avatar").textContent = (me.name || me.email || "?").trim()[0].toUpperCase();
  quick(me.profile);
  renderBilling(me.billing);
  $("#admin-tab").hidden = !me.is_admin;  // the server checks too: these routes refuse everyone else
  const jv = verified();
  if (me.email_verified) banner(jv === "1" ? "Email confirmed ✓ Founder emails and drafts are unlocked." : "", "ok");
  else if (jv === "expired") banner("That confirmation link has expired. Send yourself a new one:", "warn", true);
  else if (!me.can_confirm) banner(`To see founder emails and drafts, sign out and continue with Google using ${me.email}.`, "");
  else banner(`Confirm your email to see founder emails and drafts. We sent a link to ${me.email}.`, "", true);
  clearVerified();
}


export async function boot() {
  if (!store.token) return showAuth();
  if (new URLSearchParams(location.search).has("home")) {  // the logo in the app: show the home page, stay signed in
    history.replaceState({}, "", "/#top");
    document.body.classList.add("signed-in");
    $(".nav-cta [data-goto=signup]").textContent = "Open my chat →";
    return showAuth();
  }
  $("#auth").hidden = true; $("#app").hidden = false;
  try { await refreshMe(); await loadChat(); } catch { return; }
  const paid = takePaidOrder();
  if (paid) checkPayment(paid);
}
