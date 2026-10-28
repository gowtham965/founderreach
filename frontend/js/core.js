// Shared helpers: DOM lookup, the signed-in API call, element builder, the top banner, small formatters.
// All server text goes into the page via textContent, never as HTML.
export const $ = (s, root = document) => root.querySelector(s);
export const store = {
  get token() { try { return localStorage.getItem("fr_token"); } catch { return null; } },
  set token(v) { try { v ? localStorage.setItem("fr_token", v) : localStorage.removeItem("fr_token"); } catch {} },
};

// What the app does when the server says "signed out" (401) or "trial over" (402); set in main.js.
export const on = { signedOut: () => {}, paywall: () => {} };

export async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (store.token) headers.Authorization = `Bearer ${store.token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && store.token) { store.token = null; on.signedOut(); throw new Error("Signed out"); }
  const data = await res.json().catch(() => ({}));
  const msg = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg || `Request failed (${res.status})`;
  if (res.status === 402) on.paywall(msg);  // trial over: offer a pass right where they hit the wall
  if (!res.ok) throw new Error(msg);
  return data;
}

export const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
export const mk = el;
export const fmtDate = iso => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

/* ---------- the banner under the header (email confirmation, payments) ---------- */
export function banner(text, kind = "", resend = false) {
  $("#banner").hidden = !text;
  $("#banner").className = `banner ${kind}`.trim();
  $("#banner-text").textContent = text || "";
  $("#resend-btn").hidden = !resend;
}
