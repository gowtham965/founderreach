// FounderReach web client: plain JS modules, no build step. This file wires them together and starts the app.
import { on, store } from "./core.js";
import { googleToken } from "./urlstate.js";
import { showAuth } from "./auth.js";
import { openPlans } from "./billing.js";
import { poll } from "./chat.js";
import { boot } from "./shell.js";
import "./landing.js";
import "./hero.js";
import "./pipeline.js";
import "./admin.js";
import "./admin-startups.js";

on.signedOut = showAuth;
on.paywall = openPlans;
if (googleToken) store.token = googleToken;  // back from Google: the server put the session token in the #fragment
setInterval(poll, 30000);
boot();
