"""E2E: passkey registration must request a discoverable credential.

Drives the real /auth UI in headless Chromium with a CDP virtual authenticator.

Requires:
  - server running at $E2E_BASE (default http://localhost:8765) with
    WEBAUTHN_RP_ID=localhost, WEBAUTHN_ORIGIN=$E2E_BASE and a migrated Postgres;
  - the same DATABASE_URL etc. in this process' env, because invites come from
    `orcaster-bootstrap-owner` (owner invites on the Default workspace, so that
    /admin/ is reachable after login), run with cwd=$SERVER_DIR (default: cwd);
  - optional E2E_CHROMIUM=<path to chrome / chrome-headless-shell> when the
    browser build pinned by the installed playwright is not present.
Run:  cd server && . <env.sh> && uv run --with playwright python <this file>
"""

import json
import os
import re
import subprocess
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE", "http://localhost:8765")
SERVER_DIR = os.environ.get("SERVER_DIR", os.getcwd())
results, console = [], []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def new_invite(label):
    out = subprocess.run(
        ["uv", "run", "orcaster-bootstrap-owner", "--label", label],
        cwd=SERVER_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return re.search(r"(http\S+/auth/accept/\S+)", out).group(1)


def creds(cdp, aid):
    return cdp.send("WebAuthn.getCredentials", {"authenticatorId": aid})["credentials"]


def summarize(cs):
    return [
        {
            "id": c["credentialId"][:10],
            "resident": c["isResidentCredential"],
            "signCount": c["signCount"],
            "hasUserHandle": bool(c.get("userHandle")),
        }
        for c in cs
    ]


def logout(page):
    # The /auth UI has no logout button: POST the classic logout form endpoint.
    page.evaluate("fetch('/auth/logout',{method:'POST'})")
    page.goto(f"{BASE}/auth/login")


def ui_login(page):
    page.goto(f"{BASE}/auth/login")
    page.click("#login-btn")
    # Poll from Python: Playwright's wait_for_function evals a string, which the
    # /auth CSP (no unsafe-eval) would flag as a violation of the harness itself.
    for _ in range(60):
        page.wait_for_timeout(250)
        try:
            if "/auth/login" not in page.url:
                return page.url, ""
            msg = page.inner_text("#msg")
            if msg:
                return page.url, msg
        except Exception:
            pass  # mid-navigation
    return page.url, page.inner_text("#msg")


def ui_register(page, invite, name, tamper):
    seen = {}

    def handler(route):
        resp = route.fetch()
        body = resp.json()
        seen["server_options"] = body.get("authenticatorSelection")
        if tamper:  # reproduces the pre-fix server options
            body["authenticatorSelection"] = {
                "residentKey": "discouraged",
                "requireResidentKey": False,
                "userVerification": "preferred",
            }
        seen["sent_to_browser"] = body.get("authenticatorSelection")
        route.fulfill(response=resp, json=body)

    page.route("**/register/begin*", handler)
    page.goto(invite)
    page.fill("#display-name", name)
    page.click("#register-btn")
    page.wait_for_url(lambda u: "/auth/accept/" not in u, timeout=15000)
    page.unroute("**/register/begin*")
    return seen


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True, executable_path=os.environ.get("E2E_CHROMIUM") or None
    )
    ctx = browser.new_context()
    ctx.add_init_script("""document.addEventListener('securitypolicyviolation', e =>
        console.error('CSPVIOLATION ' + e.violatedDirective + ' ' + e.blockedURI));""")
    page = ctx.new_page()
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    aid = cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )["authenticatorId"]

    # (1) BUG REPRO: non-resident credential (old options, via route rewrite)
    seen = ui_register(page, new_invite("old-nonresident"), "Old User", tamper=True)
    print("options the server sent (fixed server):", seen["server_options"])
    print("options forced to the browser (old behaviour):", seen["sent_to_browser"])
    c1 = creds(cdp, aid)
    print("credentials:", summarize(c1))
    check(
        "1a legacy registration yields NON-resident credential",
        len(c1) == 1 and c1[0]["isResidentCredential"] is False,
    )
    logout(page)
    url, msg = ui_login(page)
    print("legacy login ->", url, "| msg:", msg)
    check(
        "1b usernameless login FAILS with non-resident credential",
        "/auth/login" in url and "failed" in msg.lower(),
        f"(msg={msg!r})",
    )

    # (2) NEW registration with unmodified server options
    seen = ui_register(page, new_invite("new-resident"), "New User", tamper=False)
    print("server authenticatorSelection:", seen["server_options"])
    check(
        "2a server requests residentKey=required",
        seen["server_options"].get("residentKey") == "required"
        and seen["server_options"].get("requireResidentKey") is True,
    )
    c2 = creds(cdp, aid)
    print("credentials:", summarize(c2))
    old_ids = {x["credentialId"] for x in c1}
    new = [c for c in c2 if c["credentialId"] not in old_ids]
    check(
        "2b new credential isResidentCredential=true",
        len(new) == 1 and new[0]["isResidentCredential"] is True,
    )

    # (3) logout, login with no authenticator edits
    logout(page)
    before = {c["credentialId"]: c["signCount"] for c in creds(cdp, aid)}
    url, msg = ui_login(page)
    print("new login ->", url)
    after = {c["credentialId"]: c["signCount"] for c in creds(cdp, aid)}
    cookies = [c["name"] for c in ctx.cookies()]
    check(
        "3a usernameless login SUCCEEDS (lands on authenticated /admin/)",
        "/admin" in url and "/auth/login" not in url,
        f"(url={url})",
    )
    check("3b session cookie set", "session" in cookies, str(cookies))
    check(
        "3c resident credential was the one used (signCount incremented)",
        after[new[0]["credentialId"]] > before[new[0]["credentialId"]]
        and after[c1[0]["credentialId"]] == before[c1[0]["credentialId"]],
    )

    check(
        "4 zero CSP violations / console errors across /auth pages",
        not any("CSPVIOLATION" in m for m in console) and not console,
    )
    print("console messages:", json.dumps(console, indent=1))
    browser.close()

ok = all(r[1] for r in results)
print("RESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
