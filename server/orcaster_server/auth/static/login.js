// login.js — passkey login flow (WebAuthn).
// External and with no inline data: the CSP of /auth does not allow inline <script>.
// The post-login destination arrives through data-next, already escaped by Jinja.
import { startAuthentication } from "./webauthn.js";

const nextUrl = document.getElementById("login-root").dataset.next || "";
const msg = document.getElementById("msg");
document.getElementById("login-btn").addEventListener("click", async () => {
  try {
    const begin = await fetch("/auth/login/begin", { method: "POST" });
    if (begin.status === 429) {
      const wait = begin.headers.get("Retry-After");
      msg.textContent = "Too many sign-in attempts. Try again" + (wait ? " in " + wait + " seconds." : " shortly.");
      return;
    }
    const opts = await begin.json();
    const assertion = await startAuthentication(opts);
    const res = await fetch("/auth/login/complete", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(assertion),
    });
    if (res.status === 429) {
      const wait = res.headers.get("Retry-After");
      msg.textContent = "Too many sign-in attempts. Try again" + (wait ? " in " + wait + " seconds." : " shortly.");
      return;
    }
    const data = await res.json();
    if (data.ok) { window.location = nextUrl || data.redirect || "/"; }
    else { msg.textContent = "Sign in failed."; }
  } catch (e) { msg.textContent = "Sign in cancelled or failed."; }
});
