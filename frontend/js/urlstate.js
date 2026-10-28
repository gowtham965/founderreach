// Things the address bar carries in, read once at startup before anything tidies the URL:
// the session token after Google sign-in, ?verified= from the confirmation link, ?paid= back from a payment.
const params = new URLSearchParams(location.search);
export const googleToken = new URLSearchParams(location.hash.slice(1)).get("token");
let justVerified = params.get("verified");  // "1" or "expired"
let paidOrder = params.get("paid");
if (googleToken || justVerified || paidOrder) history.replaceState({}, "", "/");

export const verified = () => justVerified;
export const clearVerified = () => { justVerified = null; };
export const takePaidOrder = () => { const o = paidOrder; paidOrder = null; return o; };
