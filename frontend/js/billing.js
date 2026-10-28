// Trial badge and passes: the plans panel, Razorpay's checkout popup (PhonePe: a redirect), and the payment check.
import { $, api, banner, el, fmtDate } from "./core.js";

let billing = null;
export function renderBilling(b) {
  billing = b;
  const pill = $("#plan-pill");
  pill.hidden = !b?.payments || b.admin;
  if (pill.hidden) return;
  pill.classList.toggle("locked", !b.access);
  pill.classList.toggle("paid", !!b.pass_until);
  if (b.pass_until) pill.textContent = `Pass · till ${fmtDate(b.pass_until)}`;
  else if (!b.access) pill.textContent = "Get a pass";
  else if (b.trial_ends) {
    const hrs = Math.max(0, (new Date(b.trial_ends) - Date.now()) / 36e5);
    const left = hrs >= 24 ? `${Math.ceil(hrs / 24)}d` : `${Math.ceil(hrs)}h`;
    pill.textContent = `Trial · ${left} · ${b.trial_drafts_left} drafts`;
  } else pill.textContent = `Free trial`;
}
export function openPlans(why) {
  if (!billing?.payments) return;
  $("#plans-why").textContent = why || "New matches every morning, founder emails, drafts in your voice and follow-up reminders.";
  $("#plans-error").textContent = "";
  $("#plans-via").textContent = billing.gateway ? `, through ${billing.gateway}` : "";
  const grid = $("#plan-grid"); grid.textContent = "";
  for (const p of billing.plans) {
    const best = p.days >= 30;
    const card = el("div", `plan${best ? " best" : ""}`);
    if (best) card.append(el("span", "plan-tag", "Best value"));
    const price = el("p", "plan-price"); price.append(`₹${p.price}`, el("small", "", ` / ${p.days} days`));
    const payLabel = `Pay ₹${p.price}`;
    const buy = el("button", `btn ${best ? "primary" : "dark"}`, payLabel); buy.type = "button";
    buy.addEventListener("click", async () => {
      grid.querySelectorAll("button").forEach(b => b.disabled = true);
      buy.textContent = `Opening ${billing.gateway}…`;
      const reset = msg => {
        $("#plans-error").textContent = msg || "";
        grid.querySelectorAll("button").forEach(b => b.disabled = false);
        buy.textContent = payLabel;
      };
      try {
        const r = await api("/api/pay", { method: "POST", json: { plan: p.id } });
        if (r.checkout) return await razorpayCheckout(r, reset);  // Razorpay: in-page popup
        if (!/^https:\/\/([^/]*\.)?phonepe\.com\//.test(r.redirect_url || "")) throw new Error("Unexpected payment page. Please try again.");
        location.assign(r.redirect_url);  // PhonePe: its own page, then back to /pay/return
      } catch (err) { reset(err.message); }
    });
    card.append(el("h3", "", p.label), price, el("p", "plan-per", `about ₹${Math.round(p.price / p.days)} a day`), buy);
    grid.appendChild(card);
  }
  const d = $("#plans");
  if (!d.open) d.showModal();
}
$("#plan-pill").addEventListener("click", () => openPlans(billing?.pass_until
  ? `Your pass runs till ${fmtDate(billing.pass_until)}. Buying another adds to it.` : ""));
$("#plans-close").addEventListener("click", () => $("#plans").close());
$("#plans").addEventListener("click", e => { if (e.target.id === "plans") $("#plans").close(); });  // tap outside

// Razorpay Standard Checkout: its script loads only when someone pays. The key id comes from the server
// (it's public); the secret never reaches the browser.
function loadRazorpay() {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((ok, fail) => {
    const sc = document.createElement("script");
    sc.src = "https://checkout.razorpay.com/v1/checkout.js";
    sc.onload = ok; sc.onerror = () => fail(new Error("Couldn't load the payment window. Check your connection and try again."));
    document.head.appendChild(sc);
  });
}
async function razorpayCheckout({ checkout, order }, reset) {
  await loadRazorpay();
  let settled = false;
  const rzp = new window.Razorpay({
    ...checkout,
    handler: async resp => {  // paid: the server checks the signature, then asks Razorpay
      settled = true;
      banner("Confirming your payment…");
      try {
        const v = await api("/api/pay/verify", { method: "POST", json: {
          razorpay_order_id: resp.razorpay_order_id, razorpay_payment_id: resp.razorpay_payment_id,
          razorpay_signature: resp.razorpay_signature } });
        renderBilling(v.billing);
        if (v.state === "COMPLETED" && v.billing.pass_until) banner(`Payment received ✓ Your pass is active till ${fmtDate(v.billing.pass_until)}. Happy hunting!`, "ok");
        else checkPayment(order);  // captured a moment later: keep checking
      } catch (err) { banner(err.message, "warn"); }
    },
    modal: { ondismiss: () => { if (!settled) { openPlans(); reset("Payment cancelled. You weren't charged."); } } },
  });
  rzp.on("payment.failed", r => {  // the popup stays open so they can retry with another method
    $("#plans-error").textContent = r?.error?.description || "The payment failed.";
  });
  $("#plans").close();  // our dialog sits on top of everything; Razorpay's popup must be visible
  rzp.open();
}


export async function checkPayment(order) {
  banner("Checking your payment…");
  for (let i = 0; i < 10; i++) {
    let r;
    try { r = await api(`/api/pay/${encodeURIComponent(order)}/check`, { method: "POST" }); }
    catch (err) { banner(err.message, "warn"); return; }
    renderBilling(r.billing);
    if (r.state === "COMPLETED" && r.billing.pass_until) {
      banner(`Payment received ✓ Your pass is active till ${fmtDate(r.billing.pass_until)}. Happy hunting!`, "ok"); return;
    }
    if (r.state === "FAILED") {
      banner("The payment didn't go through, so no pass was added. If money left your account, write to us from the Contact page.", "warn"); return;
    }
    await new Promise(res => setTimeout(res, 3000));
  }
  banner("The bank is still confirming your payment. Your pass turns on by itself once it does; check back in a few minutes.");
}
