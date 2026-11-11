// The home page around the hero: sticky nav, scroll reveal, "get started" buttons, the live ticker, WhatsApp buttons.
import { $, api, el, store } from "./core.js";
import { setMode } from "./auth.js";

document.documentElement.classList.add("js");
export function goToForm(m) {
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

/* ---------- ticker + startup count: real data or nothing ---------- */
export async function loadPublic() {
  try {
    const { startups } = await api("/api/public/stats");
    if (startups >= 10) $("#badge-text").textContent = `${Math.floor(startups / 10) * 10}+ seed-stage startups across Namma Bengaluru`;
  } catch {}
  try {
    const rows = await api("/api/public/fresh");
    if (rows.length < 3) return;
    const track = $("#ticker-track"); track.textContent = "";
    const lead = () => [el("span", "item live", "Fresh in Namma Bengaluru"), el("span", "item kn", "ನಮ್ಮ ಬೆಂಗಳೂರು")];
    const item = r => {
      const it = el("span", "item");
      const href = r.news_url || r.map_url;  // news story, or the startup's page on Bangalore Startup Map
      const name = href ? Object.assign(el("a", "", r.name), { href, target: "_blank", rel: "noopener" }) : el("b", "", r.name);
      it.append(name, el("span", "amt", r.stage), el("span", "", r.area), el("span", "sep", "◆"));
      return it;
    };
    for (let k = 0; k < 2; k++) { track.append(...lead()); rows.forEach(r => track.append(item(r))); }  // twice, for a seamless loop
    track.classList.add("go");
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
export function showWhatsApp(href) {
  if (document.querySelector(".btn.wa")) return;
  const hero = document.querySelector(".cta-row [data-goto=signup]");
  hero.before(waButton(href, "Chat on WhatsApp", "lg"));
  hero.className = "btn ghost lg"; hero.textContent = "Or sign up with email";
  document.querySelector(".nav-cta [data-goto=signup]").replaceWith(waButton(href, "Chat on WhatsApp", ""));
  $(".join-copy").append(waButton(href, "Start on WhatsApp", "lg join-wa"));
  $(".join-copy p").textContent = "Message us on WhatsApp, send your resume, and get your first Bangalore matches today. Prefer email? Sign up here.";
}
