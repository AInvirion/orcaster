// accept.js — passkey registration when accepting an invitation.
// External and with no inline data: token and placeholder pattern arrive
// through data-* of #accept-root, escaped by Jinja.
import { startRegistration } from "./webauthn.js";

const root = document.getElementById("accept-root");
const token = root.dataset.token;
const msg = document.getElementById("msg");
const placeholder = new RegExp(root.dataset.placeholderPattern, "i");
document.getElementById("register-btn").addEventListener("click", async () => {
  const rawName = document.getElementById("display-name").value.trim();
  if (!rawName || placeholder.test(rawName)) { msg.textContent = "Enter your name."; return; }
  const name = encodeURIComponent(rawName);
  try {
    const begin = await fetch(`/auth/accept/${token}/register/begin?display_name=${name}`, { method: "POST" });
    if (begin.status === 429) {
      const wait = begin.headers.get("Retry-After");
      msg.textContent = "Too many attempts. Try again" + (wait ? " in " + wait + " seconds." : " shortly.");
      return;
    }
    if (begin.status === 400) { msg.textContent = "Enter your name."; return; }
    const opts = await begin.json();
    const att = await startRegistration(opts);
    const res = await fetch(`/auth/accept/${token}/register/complete`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(att),
    });
    if (res.status === 429) {
      const wait = res.headers.get("Retry-After");
      msg.textContent = "Too many attempts. Try again" + (wait ? " in " + wait + " seconds." : " shortly.");
      return;
    }
    const data = await res.json();
    if (data.ok) { window.location = data.redirect || "/"; }
    else { msg.textContent = "Could not complete registration."; }
  } catch (e) { msg.textContent = "Registration cancelled or failed."; }
});
