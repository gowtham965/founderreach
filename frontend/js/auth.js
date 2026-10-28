// Sign-up / sign-in form, and showing the home page (landing) instead of the app.
import { $, api, store } from "./core.js";
import { startDemo } from "./hero.js";
import { loadPublic, showWhatsApp } from "./landing.js";
import { boot } from "./shell.js";
import { verified } from "./urlstate.js";

let mode = "signup";
let emailSignupOpen = true;  // false while the server can't send confirmation emails
export function setMode(m) {
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

let landingStarted = false;
export function showAuth() {
  $("#app").hidden = true; $("#auth").hidden = false; setMode("signup");
  if (!landingStarted) {
    landingStarted = true;
    loadPublic();
    startDemo();
  }
  const jv = verified();
  if (jv) {
    setMode("login");
    $("#auth-error").textContent = jv === "1" ? "Email confirmed ✓ Sign in to continue." : "That confirmation link has expired. Sign in to get a new one.";
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
