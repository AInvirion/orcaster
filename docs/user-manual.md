# Orcaster — User Manual

This manual describes what you can do with **Orcaster**:
from the server dashboard (dispatching tasks to a fleet of workers,
following their progress, stepping in when something fails and
reconstructing what happened) to installing, operating, network-isolating
and diagnosing a **worker** from its own host. All button, section and
state labels of the dashboard are quoted exactly as they appear in the
interface (in English); the worker's commands, variables, paths and log
messages are quoted as they appear, in `code`.

The manual is 100% usable without opening any **Technical Information**
block; those blocks only add detail for readers who want to go deeper.

To enqueue and follow tasks from a terminal or a script (the `orcaster`
CLI and the `/api/v1` HTTP API), see the [Operator manual](operator-manual.md).
For a task-oriented guide to daily operation from the browser, see the
[Operator manual (Dashboard)](operator-manual-ui.md).

## Table of Contents

- [Before You Begin](#before-you-begin)
- [Getting Started](#getting-started)
  - [Starting the Orchestrator Server](#starting-the-orchestrator-server)
  - [Connecting a Worker](#connecting-a-worker)
- [Isolating a Worker's Network (Egress)](#isolating-a-workers-network-egress)
- [Checking Fleet Status](#checking-fleet-status)
- [Dispatching a New Task](#dispatching-a-new-task)
- [Following a Task's Progress](#following-a-tasks-progress)
- [Reviewing a Run's Output](#reviewing-a-runs-output)
- [Intervening on a Task](#intervening-on-a-task)
- [Operating on Several Tasks in Bulk](#operating-on-several-tasks-in-bulk)
- [Monitoring and Managing a Worker](#monitoring-and-managing-a-worker)
- [Operating a Worker Day to Day](#operating-day-to-day)
- [Pausing, Stopping and Resuming a Worker](#pausing-stopping-and-resuming-a-worker)
- [Updating a Worker and Its Fleet](#updating-the-worker)
- [Diagnosing and Resolving Worker Problems](#troubleshooting)
- [Analyzing Completions and Failures (Outcomes)](#analyzing-completions-and-failures-outcomes)
- [Administering the Workspace](#administering-the-workspace)
- [Sharing Status with Third Parties (Share links)](#sharing-status-with-third-parties-share-links)
- [Auditing What Happened](#auditing-what-happened)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Operations](#operations)
- [Frequently Asked Questions](#frequently-asked-questions)
- [JSON API Reference](#json-api-reference)
- [Glossary](#glossary)

---

## Before You Begin

### Access

Orcaster opens in the browser. The main page is the dashboard,
available at the path `/admin/`. If you open the site root, the system
redirects you to the dashboard automatically.

### Dates and times

<a id="dates-and-times"></a>
All absolute dates and times that the dashboard **shows** are in your
**local time** (your browser's time zone). This includes the time columns of
the tables and the dates on the tokens, workspace and audit log pages.

Relative times (for example, `12s ago`) are different: they tell you how long
ago something happened relative to now, not a clock time, so they are not
expressed in any particular time zone.

To see the exact value in **UTC**, hover over any date or time (absolute or
relative): the tooltip shows it with the prefix **UTC ·**.

> The only exception is the **Run at** field of the new task form: there you
> **enter** a time in your local zone (see [Advanced
> options](#advanced-options)).

This convention (local time on screen, UTC in the tooltip) applies throughout
the manual. Wherever a column or field shows a time, it is understood to be
local unless stated otherwise.

### Signing in

Sign-in uses a **passkey** (fingerprint, face or device PIN); there are no
passwords.

1. Open the sign-in page. The system shows the title **Sign in**.
2. Click **Sign in with passkey**.
3. Confirm with the method your device asks for (fingerprint, face or PIN).
4. The system takes you to the dashboard.

If sign-in does not complete, the system shows **Sign in failed.** or
**Sign in cancelled or failed.** Try again from step 2.

If sign-in fails with a passkey registered earlier (for example, on a
physical security key), that passkey may not be recognized by the current
sign-in, which requires a credential stored on the device. In that case,
register the passkey again. The interface does not yet let you delete the
previous passkey.

If the system shows **Too many sign-in attempts. Try again in N seconds.**,
sign-in made too many attempts in a row from your connection (more than 10 in
a row or more than 30 per minute from one IP address, or more than 100 in a
row or 300 per minute from one network; configurable values). Several people
behind the same office connection can sign in at the same time, up to about
10. Wait the indicated seconds and press **Sign in with passkey** again. The
invitation page shows **Too many attempts. Try again in N seconds.** in the
same case.

### Accepting an invitation

An invitation link is single-use: it creates a new account or, if you are
already signed in, joins you with your current account. To create your
account:

1. Open the link. The system shows the title **Join**.
2. If the invitation is valid, the page states the workspace and the role it grants (for example, **Invitation for: Workspace “Acme” as member**). In **Your name**, enter your name; the field starts empty and, if you leave it empty, the system shows **Enter your name.**
3. Click **Create account with passkey** and confirm with your
   device.
4. The system creates the account and takes you to the dashboard.

The name the account is created with cannot be changed later: there is no
profile editing screen. Type it carefully when you register.

If you already have an account, do not create another. When you are not
signed in, the page reminds you with **Already have an account? Sign in, then
open this link again.** Sign in, open the link again and follow [Joining
another workspace with your current
account](#joining-another-workspace-with-your-current-account).

If the link does not exist, was revoked or was already used, the system shows
**This invite is no longer valid**. If the invitation expired, or its
workspace is disabled, the page still opens, with the same buttons, but it
names neither the workspace nor the role, so as not to reveal them to someone
holding an old link: instead it shows only **This invitation is no longer
valid. Ask for a new link.** With an expired invitation, the system rejects
the account or the join only when you click the button.
Ask a workspace owner for a new invitation (see [Administering the
Workspace](#administering-the-workspace)).

#### Joining another workspace with your current account

If you open the link while signed in, you join the invited workspace with
your current account and passkey:

1. Open the link. The system shows the title **Join**.
2. Check which workspace and with which role the invitation joins you (line
   **Your current account will be added to: ...**) and click the
   **Join as** button followed by your name (for example, **Join as Ana**).
3. The system adds you to the workspace with the invitation's role and takes
   you to the dashboard, with that workspace as the active one. From then on
   the **Workspace** selector in the top bar shows all your workspaces.

If you were already a member of that workspace, the invitation is consumed and
your role does not change. To create a separate account with another passkey
instead, use **Your name** and **Create account with passkey**, below **Or
create a separate account with a new passkey:** on the same page.

If your session expired before you clicked **Join as**, the system shows the
invitation page again without consuming it. Sign in and open the link again.

An expired link, or one for a disabled workspace, still shows the **Join as**
button, with the line **This invitation is no longer valid. Ask for a new
link.** instead of the workspace and the role, and without the line **Your
current account will be added to: ...**. If the link is expired, clicking
**Join as** makes the system answer **This invite is no longer valid** and it
does not consume the invitation.

### Signing out

1. In the top bar, click your name (with the initial in the circle, on the
   right).
2. Click **Sign out**.

<details>
<summary>Technical Information</summary>

- WebAuthn/passkey authentication. Endpoints: `POST /auth/login/begin`,
  `POST /auth/login/complete`, `POST /auth/logout`.
  The login challenge is single-use and is consumed even if the attempt
  fails; a malformed body gets the same `401` as a rejected login. If the
  login fails, press **Sign in with passkey** again to get a new challenge.
  Pending challenges are bounded (expired ones are purged, also in the
  background, and at most 1000 per type are kept). `POST /auth/login/begin`,
  `POST /auth/login/complete`, `POST /auth/accept/{token}/register/begin` and
  `.../register/complete` have a per-client (IP) limit and a per-network limit
  (IPv4 `/24`, IPv6 `/48`): past either one they answer `429` with the
  `Retry-After` header (seconds) and `{"ok": false, "reason": "rate_limited"}`.
  The `begin` and `complete` calls count in separate buckets with the same
  values, so an admitted `begin` can always be completed. The limit slows a
  flood down, but it does not prevent a `/24` (about 1000 `begin` calls in
  about 180 s with the default values) or many networks from filling the cap
  of pending challenges and forcing other users to retry. It is configured
  with `AUTH_BEGIN_RATE_PER_MINUTE` (30; `0` disables it) and
  `AUTH_BEGIN_RATE_BURST` (10) per client, and with
  `AUTH_BEGIN_GROUP_RATE_PER_MINUTE` (300; `0` disables it) and
  `AUTH_BEGIN_GROUP_BURST` (100) per network. The counter lives in the memory
  of each server process: with several uvicorn processes the effective cap is
  up to N times the configured one, and a restart resets it.
- Invitations: `POST /auth/accept/{token}/register/begin` (answers
  `400 {"ok": false, "reason": "invalid_display_name"}` if `display_name`,
  trimmed, is empty, exceeds 200 characters or has the form
  `workspace:<uuid>`; without the parameter, the name is `New user`) and
  `.../register/complete` (new account); `POST /auth/accept/{token}/join`
  (signed-in user: it creates no user, passkey or session; without a valid
  session it redirects with `303` to `/auth/accept/{token}` without consuming
  the invitation; a `POST` whose `Origin` is not the site's, or without
  `Origin` and with `Sec-Fetch-Site` other than `same-origin` or `none`,
  answers `403`).
  `GET /auth/accept/{token}` answers `410` with the invalid-invitation page
  if the token does not exist, is revoked or was already used; if the
  invitation is expired or its workspace is disabled it answers `200`, with
  the neutral line and without workspace, role or label. On submit (`POST
  .../join` and `.../register/complete`), a nonexistent, revoked, expired or
  already used token answers `410`; the same if the invitation's workspace is
  disabled (nothing is consumed or created).
- The dashboard requires an active session and membership in a workspace
  (`require_workspace`); without them, access to `/admin/` is rejected.

</details>

---

<a id="getting-started"></a>
## Getting Started

This section is for the operator in charge of installing and administering the
infrastructure: from starting the server to connecting an individual worker or
deploying a whole fleet. If you only use the dashboard to dispatch tasks, you
can skip it.

---

<a id="starting-the-orchestrator-server"></a>
## Starting the Orchestrator Server

### Prerequisites

| Component | Minimum version | Notes |
|-----------|-----------------|-------|
| Python | 3.13+ | Required on the host where the server runs. |
| uv | latest stable | Manages the virtual environment and the dependencies. |
| Podman | 4.4+ | Needed for the development database and for the workers. |
| PostgreSQL | 16+ | The only infrastructure dependency. In production it can be external. |
| GNU make | any | The `Makefile` uses GNU extensions (`$(word ...)`, `$(shell ...)`). On FreeBSD, use `gmake`. |

On macOS, start the Podman virtual machine before continuing:

```bash
podman machine init && podman machine start
```

### Development environment (quick start)

1. Start the local database:

   ```bash
   make db-up
   export DATABASE_URL='postgresql+asyncpg://orcaster:orcaster@127.0.0.1:5432/orcaster'
   ```

2. Apply the migrations:

   ```bash
   make migrate server
   ```

3. Generate the server's Ed25519 key (if it does not exist yet) and start the
   server with hot reload:

   ```bash
   make up server           # keygen + uvicorn at http://0.0.0.0:8000
   ```

   You can also use the shortcut that combines all the previous steps into a
   single command:

   ```bash
   make dev server          # db-up → migrations → keygen → uvicorn
   ```

4. Check that the server responds:

   ```bash
   curl http://localhost:8000/healthz
   ```

5. Create the first owner's account (see [Bootstrapping the first owner](#bootstrapping-the-first-owner)).

### Bootstrapping the first owner

The server does not expose a public registration route. The first owner must
be created by issuing an invitation from the host:

```bash
cd server && uv run orcaster-bootstrap-owner
```

The script prints a link of the form `/auth/accept/{token}` valid for 15
minutes. Open it in the browser and complete the registration with your
passkey. You become the **owner** of the `Default` workspace.

To create a workspace with its own name instead of `Default`:

```bash
cd server && uv run orcaster-bootstrap-owner --name "My Company"
```

> Re-running without `--name` is idempotent: it adopts the same `Default`
> workspace and only issues a new invitation. Re-running with `--name` creates
> a new workspace each time; it is not idempotent by design.

If whoever will own the new workspace already has an account in another
workspace, open the link while signed in and use **Join as** instead of
registering another passkey (see [Joining another workspace with your current
account](#joining-another-workspace-with-your-current-account)).

### Production deployment (Debian 13 VPS)

To install the server on a clean VPS with Debian 13 (Trixie):

1. Copy the repository to the VPS, into a root checkout (`/root/orcaster`),
   and leave it owned by root with no group or other write access:

   ```bash
   rsync -a --exclude '.git' --exclude '.venv' --exclude '*.env' --exclude 'server/.env' \
       ./ root@vps:/root/orcaster/
   ssh root@vps 'chown -R root:root /root/orcaster && chmod -R go-w /root/orcaster'
   ```

2. On the VPS, as root, configure the deployment file inside the checkout
   (owner root, mode `600`):

   ```bash
   cd /root/orcaster/setup-scripts/debian13
   install -m 600 -o root -g root setup.env.example setup.env
   $EDITOR setup.env          # set DOMAIN, ADMIN_EMAIL and, optionally, DB_PASSWORD
   ```

3. Run the provisioner **from the checkout**. It is idempotent and can be
   re-run:

   ```bash
   cd /root/orcaster
   sudo make setup-server debian13
   ```

   Or directly:

   ```bash
   sudo bash setup-scripts/debian13/run-all.sh
   ```

4. After provisioning, create the first owner (from the same directory):

   ```bash
   sudo make bootstrap debian13
   ```

5. Open the printed link, register your passkey and enter the dashboard at
   `https://<DOMAIN>`.

> **The installer runs from a root checkout, not from
> `/opt/orcaster`.** The installer runs as root, and
> `/opt/orcaster` belongs to the service user (`orcaster`): if the
> installer were run from there, a compromised service could modify a script
> and the next run would execute it as root. Step 3 copies the checkout to
> `/opt/orcaster`; the checkout never changes owner. `/root` is
> `0700`, so commands with `/root` paths are run as root (`sudo -i`, or direct
> SSH as root). If you launch the installer from a tree the service user can
> write to, it stops before doing anything with a `Source guard: …` error
> that names the path, the owner and the mode.

> The server's Ed25519 public key ends up at `/opt/orcaster/secrets/ed25519.pub`.
> Copy it to each worker host as `client/server-ed25519.pub` (development
> checkout) or `/etc/orcaster/server-ed25519.pub` (fleet host); without it the
> workers reject all tasks.

<details>
<summary>Technical Information</summary>

- The provisioner runs the scripts `00` to `08` in order (PostgreSQL 17, uv,
  Ed25519 key, `uv sync`, Alembic, systemd, Caddy with automatic HTTPS, ufw
  firewall). The script `09-bootstrap-owner.sh` is the first-owner step.
- The origin guard of `lib.sh` (only when running as root) aborts before any
  other effect if the scripts directory or any of its ancestors up to `/` is
  not owned by root, has group or other write access, or has an ACL; if
  `lib.sh`, `run-all.sh`, any `*.sh` of the suite, the `Makefile` or
  `setup.env` is not a regular file owned by root without group or other write
  access (symbolic links are rejected); if the script was invoked through a
  path with symbolic links; or if the checkout and `INSTALL_DIR` are the same
  directory or one contains the other. `03-app-install.sh` copies the checkout
  to `INSTALL_DIR` in two stages: root prepares it in a private directory
  under `/tmp` (mode `750`, group `orcaster`) and the service user syncs it
  with `rsync --delete` and the same exclusions as always; `secrets/`, the
  Ed25519 key and `server/.env` are also written by that user, not root.
- Server environment variables (`server/.env` file or process variable):

  | Variable | Default | Purpose |
  |----------|---------|---------|
  | `DATABASE_URL` | — | asyncpg string, e.g. `postgresql+asyncpg://user:pass@host/db`. Required. |
  | `BIND_HOST` | `0.0.0.0` | uvicorn's listening interface. |
  | `BIND_PORT` | `8000` | Listening port. |
  | `ED25519_PRIVATE_KEY_PATH` | — | Path to the Ed25519 private key (PEM). Generated by `make keygen server`. |
  | `WEBAUTHN_RP_ID` | `localhost` | Domain without scheme or port (e.g. `orcaster.example.com`). |
  | `WEBAUTHN_ORIGIN` | `http://localhost:8000` | Full origin of the dashboard (e.g. `https://orcaster.example.com`): `scheme://host[:port]`, without path or query. It is validated at startup; the server does not start if it is not a valid origin and says why. The scheme's default port (`:443`, `:80`), uppercase letters and the trailing slash are corrected automatically. |
  | `WEBAUTHN_RP_NAME` | `Orcaster` | Name shown in the passkey prompt. |
  | `SESSION_COOKIE_SECURE` | `false` | Must be `true` in production (requires HTTPS). |
  | `AUTH_BEGIN_RATE_PER_MINUTE` | `30` | Sustained rate per client (IP) of the `begin` calls and, in a separate bucket with the same value, of the anonymous passkey `complete` calls (`/auth/login/*`, `/auth/accept/{token}/register/*`); beyond it, `429` with `Retry-After`. `0` disables the per-client level. Per process. |
  | `AUTH_BEGIN_RATE_BURST` | `10` | Burst allowed per client before rejecting starts (integer >= 1). |
  | `AUTH_BEGIN_GROUP_RATE_PER_MINUTE` | `300` | Sustained rate shared per network (IPv4 `/24`, IPv6 `/48`), so that rotating addresses does not evade the per-client limit. `0` disables the per-network level. Per process. |
  | `AUTH_BEGIN_GROUP_BURST` | `100` | Burst allowed per network (integer >= 1). |
  | `API_DOCS_PUBLIC` | `false` | `true` opens `/openapi.json`, `/docs` and `/redoc` without auth; local development only. |
  | `MIGRATE_ON_STARTUP` | `true` | Applies Alembic migrations at startup. Disable it if you prefer an explicit `alembic upgrade head`. |
  | `WORKER_HEARTBEAT_TIMEOUT_SECONDS` | `120` | Time without a heartbeat before a worker is marked offline. |
  | `DEFAULT_CLAIM_TIMEOUT_SECONDS` | `3600` | Default claim timeout when the task does not specify one. |
  | `COMMANDS_RETENTION_DAYS` | `7` | Days that command records are retained. |

- WebAuthn requires HTTPS with a real domain. Without TLS, passkey login does
  not work. `WEBAUTHN_RP_ID` must match the browser's domain exactly.
- The Ed25519 key (`ED25519_PRIVATE_KEY_PATH`) signs all tasks before
  delivering them to the workers. Losing it invalidates signature
  verification on all workers; back up the private PEM
  (see [Backing up unrecoverable state](#backing-up-unrecoverable-state)).
- `make keygen server` generates `server/.dev-ed25519.pem` (private) and
  derives the public key at the resolved path of `WORKER_PUBKEY`:
  `client/server-ed25519.pub` in a development checkout,
  `/etc/orcaster/server-ed25519.pub` on a host with config (see the notice in
  [Connecting a Worker](#connecting-a-worker)). It is idempotent.

</details>

---

<a id="connecting-a-worker"></a>
## Connecting a Worker

Connecting a worker is an installation operation performed on the machine
that will act as the worker, outside the dashboard. Once connected, the
worker appears in the **Workers** panel when it sends its first heartbeat.

The full flow requires:

1. Generate an enrollment token in the dashboard.
2. Download the server's public key.
3. Prepare the `client/.env` file on the worker host.
4. Start the worker with `make up client`.

> **Steps 1-4 are the path for a single, hand-managed host. On a fleet host
> deployed by tarball, the host configuration does NOT go in the code
> checkout.** `client/.env` and `client/server-ed25519.pub` are the Makefile's
> fallback for a development machine (without config in `/etc/orcaster`); on a
> production host `ops/release/push-release.sh` replaces the whole of
> `/opt/orcaster` on every release and takes whatever is inside with
> it, and the release tarball does not ship those two files, so the worker
> would not start again. On a fleet host the environment goes in
> `/etc/orcaster/env` (or `/etc/orcaster/env.N` for several workers, mode
> `0640 root:orcaster`) and the public key in
> `/etc/orcaster/server-ed25519.pub`. `make up client` resolves those paths
> by itself: if the host has config (any `/etc/orcaster/env`, `env.*` or
> `server-ed25519.pub` exists) it uses `/etc/orcaster/env[.N]` and
> `/etc/orcaster/server-ed25519.pub`, and if one is missing it fails naming the
> `/etc/orcaster` path without falling back to `client/`; without host config
> (development checkout) it uses `client/.env[.N]` and
> `client/server-ed25519.pub` and prints a `WARNING` on stderr. `WORKER_ENV=`
> and `WORKER_PUBKEY=` still work as an optional override. That is the layout
> that [Provisioning a Debian 13 VPS](#provisioning-a-debian-13-vps) sets up;
> for more than one worker per host, see [Multiple workers on the same
> host](#multiple-workers-on-one-host).

<a id="what-the-worker-host-needs"></a>
### What the worker host needs

| Requirement | Minimum version | Notes |
|-------------|-----------------|-------|
| Rootless Podman | 4.4+ | The project is **Podman-only**: there is no Docker alternative. |
| `aardvark-dns` | **1.11.0** | The `Makefile` validates it before starting (see [Egress isolation precondition errors](#egress-precondition-errors)). |
| `nftables`, `netavark`, `passt` | any recent | Needed for the internal network of [egress isolation](#isolating-a-workers-network-egress). The `Makefile` does **not** validate their presence (unlike `aardvark-dns`); if they are missing, the failure only appears when the network is created. |
| GNU make | any | On FreeBSD use `gmake`. |
| macOS | Podman Machine | Start the VM before continuing: `podman machine init && podman machine start`. |

<details>
<summary>Technical Information</summary>

The worker container hardening is always active, even with egress isolation
disabled: `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`,
`--pids-limit 1024` and a 256 MiB `tmpfs` mounted at `/home/orcaster`. The
base image is `python:3.13-slim-bookworm`; the process runs as the `orcaster`
user (uid/gid `1000`).

</details>

### Step 1 — Get an enrollment token and the public key

Generate the token in the dashboard: open **Workspaces** → section **Worker
enrollment tokens** and follow the procedure documented in [Worker enrollment
tokens](#worker-enrollment-tokens). The window that appears when you create the
token includes:

- A block ready to paste into `client/.env` with `ORCH_BASE_URL`,
  `ORCH_ENROLLMENT_TOKEN` and `ORCH_WORKER_NAME` (empty).
- A link to download **server-ed25519.pub** (the server's public key).

Save the downloaded file as `client/server-ed25519.pub` in the same directory
where `client/.env` is or will be.

> The public key is also available at any time from
> **Workspaces** → **Download server public key**.

### Step 2 — Prepare the `client/.env` file

On the worker host, inside the repository root directory:

```bash
cp client/.env.example client/.env
$EDITOR client/.env
```

Essential variables:

| Variable | What to put |
|----------|-------------|
| `ORCH_BASE_URL` | URL of the orchestrator server (e.g. `https://orcaster.example.com`). |
| `ORCH_ENROLLMENT_TOKEN` | The token you copied from the dashboard. It is used only on the first start. |
| `ORCH_WORKER_NAME` | Name shown in the dashboard (optional, but recommended). |

You do not need to configure any API key: the worker registers itself
automatically on the first start (using `ORCH_ENROLLMENT_TOKEN`) and stores the
API key that the server issues to it in the persistent volume.

> **About `ORCH_BASE_URL` from a container:**
> If the server runs on the same host as the worker, do not use `localhost`.
> Use `http://host.containers.internal:8000` (macOS / Linux with Podman).

<a id="about-the-worker-identity-api_key"></a>
### About the worker identity (`api_key`)

The worker does **not** receive its access credential (`api_key`) through
manual configuration. The server issues it at registration time, and the
worker stores it together with its `worker_id` in the `state.json` file,
inside the persistent data volume (`orcaster-data`). There is no `ORCH_API_KEY`
variable that you must fill in or rotate by hand: if it exists in the `.env`,
the worker ignores it at runtime (some provisioning scripts stamp it there
only to preserve it across reruns, but the worker does not read it).

<a id="declaring-the-workers-capabilities"></a>
### Declaring the worker's capabilities

Some tasks ask for a worker with certain capabilities, for example a model
with enough context. The task declares this in its `eligibility`, and the
worker declares what it has in the `ORCH_CAPABILITIES` variable of its
environment file (`client/.env`, or `/etc/orcaster/env.N` on a production
host):

```bash
ORCH_CAPABILITIES={"model":true,"model_context":8192}
```

| Rule | Detail |
|------|--------|
| Format | A flat JSON object: each name is `true`/`false` or a number. |
| Names | Letters, digits, `_`, `.` and `-`; they cannot start with a digit. |
| No surrounding quotes | Write the JSON as is, without `'...'` or `"..."` wrapping it: `podman --env-file` does not strip them, and the worker would not start. |
| Variable absent or empty | The worker declares nothing and the server keeps what it already had stored. |
| `ORCH_CAPABILITIES={}` | Clears the stored capabilities. |

The worker sends this value when it registers and on every poll, so changing
it does **not require re-enrolling**: edit the variable and recreate the
container. The worker keeps its identity (the same UUID) and the server applies
the new value on the first poll, before choosing a task.

If the value is badly written (invalid JSON, text instead of a number, a name
with spaces), the worker **does not start** and the log says which entry
failed, with the `ORCH_CAPABILITIES` prefix. Fix it and recreate the
container.

> Tasks that do not ask for capabilities are still picked up by any worker,
> whether or not it declares `ORCH_CAPABILITIES`.

> **Production host with a fleet (`run-fleet.sh`):** running `run-fleet.sh`
> again rewrites every `/etc/orcaster/env.N` from `setup.env`, and a line added
> by hand is lost. For it to survive, declare it also in `EXTRA_ENV` of
> `setup.env`, with single quotes outside because that file is read by bash:
> `EXTRA_ENV='ORCH_CAPABILITIES={"model":true,"model_context":8192}'`.
> `install-release.sh` does not rewrite the `env.N` files.

<details>
<summary>Technical Information</summary>

The field travels as `capabilities` in `POST /v1/workers/register` and in
`POST /v1/workers/{id}/poll`. In the poll it is optional: if it does not
travel, the server does not touch `workers.capabilities`; if it travels, it
replaces it entirely. If an invalid value arrives, the server does not answer
422 (the worker would retry forever): it ignores it, keeps the stored value
and leaves a `WARNING` with the `worker_id`.

</details>

### Step 3 — Place the public key

The `client/server-ed25519.pub` file must be next to `client/.env` before
starting. The `Makefile` detects it automatically and mounts it at
`/var/lib/orcaster/ed25519.pub` inside the container. If you keep it at another
path, pass it with `WORKER_PUBKEY=/path/to/file.pub` (optional override).

> Without the server's public key loaded, the worker **does not start**: it is
> not left "connected but not working", it simply aborts on startup. See
> [The worker crash-loops on startup](#crash-loop-and-its-three-causes).

> On a fleet host the public key does **not** go in the checkout: it goes in
> `/etc/orcaster/server-ed25519.pub`, and `make up client` takes it from there
> without arguments (see the notice at the start of this section and
> [Provisioning a Debian 13
> VPS](#provisioning-a-debian-13-vps)).

### Step 4 — Start the worker

From the repository root:

```bash
make up client          # image build (~1-2 min for the first build) + start
make logs client        # check that the worker started and registered
```

One of these two lines must appear in the log confirming the enrollment:

- `Registered worker_id=<uuid>` (first successful enrollment).
- `Reusing existing worker_id=<uuid>` (the worker already had a stored
  identity and reused it, without needing the token).

Followed by the first heartbeats. The worker shows up in the dashboard's
**Workers** panel after a few seconds.

> **An enrollment token can be used more than once.** The server supports
> multi-use tokens (up to 1000 uses) with a configurable validity (1 hour, 1
> day, 7 days or 30 days). This lets you enroll a whole fleet with a single
> revocable secret, instead of generating one token per host.

Common operations after the first start:

| Operation | Command |
|-----------|---------|
| View live logs | `make logs client` |
| Restart (without rebuild) | `make restart client` |
| Rebuild after `git pull` | `make down client && make build client && make up client` |
| Stop the worker | `make down client` |
| Purge state (container + egress sidecar + volumes) | `make purge client` |

> **Purging the state** (`make purge client`) removes the `orcaster-data`
> volume, which erases the worker's identity (`state.json`). Use this option
> if the worker was left with an orphaned `state.json` after recreating the
> server's database.
>
> The purge takes away everything that is **specific to that worker**: its
> container, its egress sidecar (`orcaster-proxy`) and its three volumes
> (`orcaster-data`, `orcaster-cache` and the egress allowlist
> `orcaster-egress-vol`). The egress networks (`orcaster-egress`,
> `orcaster-egress-out`) are **not** deleted: they are shared by all workers on
> the host, so deleting them when purging one would cut off Internet access for
> those that keep running. `make up client` recreates them if they are missing.

Steps 1-4 serve a single, hand-managed host. If you would rather get a new VPS
ready with one command, or deploy several machines/several workers per
machine, follow one of the alternative installation paths below.

<a id="provisioning-a-debian-13-vps"></a>
### Provisioning a Debian 13 VPS (worker)

To get a clean Debian 13 (Trixie) VPS ready as a worker host (`orcaster` user,
rootless Podman, `/etc/orcaster/env`, public key and built image, **without
starting anything yet**), the VPS first needs the code installed. The code is
**not cloned**: it arrives pushed from the operator's machine with the tools in
`ops/release/`; the host does not need any GitHub credential.

1. From the operator's machine, push the code to the VPS. On a freshly created
   VPS, without the `orcaster` user yet, invoke `install-release.sh` directly
   with `--no-restart` (do not use `push-release.sh` for this first delivery:
   without the `orcaster` user already created, the container-recreation path
   of `install-release.sh` aborts):

   ```bash
   (
       set -euo pipefail
       tgz="$(ops/release/make-release.sh)"; base="$(basename "$tgz")"
       host=root@the-vps
       # Private directory (0700, of the ssh user) with a random name on the host:
       # never a fixed /tmp name, which another local user could create first.
       stage="$(ssh "$host" 'umask 077; mktemp -d "${TMPDIR:-/tmp}/bootstrap.XXXXXXXXXX"')"
       # It is deleted on exit, also if something fails.
       trap 'ssh "$host" "rm -rf -- $stage"' EXIT
       scp "$tgz" "$tgz.sha256" ops/release/install-release.sh "$host:$stage/"
       # sha256sum -c inside the directory, BEFORE `sudo bash`.
       ssh "$host" "cd '$stage' && sha256sum -c '$base.sha256' \
           && sudo bash '$stage/install-release.sh' --tarball '$stage/$base' --no-restart"
   )
   ```

   The block (in a subshell, paste it as is) creates a random private
   directory on the host, verifies the `sha256` there before `sudo bash` and
   deletes it on exit, also if it fails. It does not leave the installer under
   a fixed `/tmp` name.
   `install-release.sh` installs the code in `/opt/orcaster` (the
   `orcaster` user's tree, which the container mounts) and also leaves a root
   copy of the same release in `/root/orcaster`. The provisioning
   suite is run from that copy, never from `/opt/orcaster`.

   > **Limitation (not supported):** the worker is provisioned on a host of its
   > own, not on the one that runs the server. Both installers use the same
   > root copy and the same `INSTALL_DIR` by default. If the server's root copy
   > is a git checkout, `install-release.sh` aborts; if it is a copy uploaded
   > by rsync, it does not abort: it replaces it with the worker's and the
   > server's `setup.env` and its backups are lost. Run the server and workers
   > on separate hosts. A different `--source-dir` or `INSTALL_DIR` is untested.

2. Now on the VPS, with the code installed, create `/etc/orcaster/setup.env`
   from the example and enter the provisioning directory of the root copy (as
   root: `/root` is `0700`):

   ```bash
   install -d -m 0750 -o root -g orcaster /etc/orcaster 2>/dev/null \
       || install -d -m 0750 /etc/orcaster
   cd /root/orcaster/setup-scripts/debian13-worker
   install -m 0640 -o root -g orcaster setup.env.example /etc/orcaster/setup.env 2>/dev/null \
       || install -m 0640 setup.env.example /etc/orcaster/setup.env
   $EDITOR /etc/orcaster/setup.env      # ORCH_BASE_URL + pubkey
   ```

3. Run the provisioning orchestrator:

   ```bash
   sudo bash run-all-worker.sh
   ```

   This script runs steps `00` to `05` in order: package installation and
   creation of the `orcaster` user, writing and validation of the server's
   public key, generation of `/etc/orcaster/env`, building the image and a
   final smoke test. The code itself was already installed in step 1: none of
   these steps clones or downloads it.

4. If provisioning is interrupted (for example, halfway through the package
   installation), resume from the step where it stopped, without repeating
   what is already done:

   ```bash
   cd /root/orcaster/setup-scripts/debian13-worker
   sudo bash run-all-worker.sh --from 03
   ```

5. Start the worker and confirm the enrollment (see Step 4 above):

   ```bash
   # `cd /` first: sudo inherits the current directory, and from a root SSH
   # session (`/root`, not readable by other users) make does not even start.
   # make resolves /etc/orcaster/env and /etc/orcaster/server-ed25519.pub by
   # itself (there is host config); WORKER_ENV/WORKER_PUBKEY are not needed.
   cd /
   sudo -u orcaster make -C /opt/orcaster up client
   ```

6. Enable automatic start on host reboot (needed because
   `--restart unless-stopped` does **not** bring a rootless Podman container
   back up after a reboot). It requires the container already created by the
   previous step: the unit is derived from its run command.

   ```bash
   sudo bash /root/orcaster/setup-scripts/debian13-worker/06-autostart.sh
   ```

<details>
<summary>Technical Information</summary>

Each step of `run-all-worker.sh` is a script of its own, invocable in
isolation: `00-preflight.sh` (packages: `ca-certificates curl make openssl
podman uidmap slirp4netns passt fuse-overlayfs dbus-user-session aardvark-dns
nftables`, without `git`: the code is not cloned; `orcaster` user with
`--add-subuids/--add-subgids 100000-165535`; `podman system migrate`;
`loginctl enable-linger orcaster`), `02-server-pubkey.sh` (validates the pubkey
with `openssl pkey -pubin -noout`), `03-worker-env.sh` (generates
`/etc/orcaster/env`, preserving previous credentials if the script is re-run),
`04-build.sh` (`make build client` as the `orcaster` user) and
`05-smoke-test.sh` (checks rootless, image, `.env`, pubkey and
`curl ${ORCH_BASE_URL%/}/healthz`). The step numbers start at `00` and skip `01`;
this is intentional. `06-autostart.sh` is not part of `run-all-worker.sh`: it is run
separately because it does not start the container by itself; it generates the
`systemd --user` unit in adoption mode (`podman generate systemd`, without
`--new`).

</details>

<a id="deploying-a-fleet"></a>
### Deploying a Fleet

**Several workers on the same host.** Use `WORKER_COUNT` in `setup.env` (or
`make up client N=2` manually) to bring up more than one worker on the same
machine without the second killing the first; each one gets its own identity.
See the details in
[Multiple workers on the same host](#multiple-workers-on-one-host).

**Memory limit per worker.** Optionally, set `WORKER_MEMORY` in `setup.env`
(for example, `28g`) to limit the memory each worker of the fleet can use. With
the limit active, the worker reports that amount to the orchestrator (and not
the machine's total memory), so that it only receives tasks that fit in it.
Empty (the default) means no limit. The value must be a number greater than
zero with the suffix `b`, `k`, `m` or `g`; a badly written value (for example,
`28` without a suffix, or `0g`) stops the deployment with an error message
before touching the fleet.

When you recreate a worker by hand with `make up client N=…`, the command
reads the limit from `setup.env` on its own, so the worker keeps it. To bring
it up without a limit deliberately, add `WORKER_MEMORY=` (empty) to the
command; to use another value just this once, add `WORKER_MEMORY=…`.

> **Note:** if a worker ever started without a memory limit, it warns about it
> in its own log with the message `container running with NO memory cap`. To
> see it: `make logs client N=…` (see the section [Operating a Worker Day to
> Day](#operating-day-to-day)).

**Host directories for tasks.** Some tasks need a file that the operator leaves
on the machine, for example a package in `/opt/example-ro`. The worker does not
see the machine's directories on its own: declare them in `setup.env`,
separated by spaces, with `WORKER_RO_MOUNTS="/opt/example-ro"`. Each worker
receives that directory at the same path and read-only. Empty (the default)
means no extra directory. Each path must be absolute, without spaces, `:` or
`,`, it cannot be a system path or one the worker already uses (`/etc`, `/var`,
`/tmp`, `/usr`, `/home/orcaster`, among others) and it must exist. It must also
be readable by "other": the directory and its subdirectories with `o+rx`, the
files with `o+r` (for example `/opt/example-ro` at `0755` and the package at
`0644`). Inside the container the worker is not the machine's `orcaster` user
nor in its group, so a `0750` or a `0640` is not enough for it even if
`orcaster` can read them. `chmod -R o+rX /opt/example-ro` fixes it. If any of
that fails, the deployment stops with a message that names the path, before
touching the workers that are running.

The change is applied when the worker is created again (`make up client
N=…`, `run-fleet.sh` or a deployment). Restarting the worker does not apply it.
When you recreate it by hand, the command reads the list from `setup.env` just
like the memory limit; `WORKER_RO_MOUNTS=` (empty) on the command brings it up
without extra directories.

**Fleet without SSH (cloud-init).** `cloud-init-worker.sh` is exclusively the
[golden snapshot](#golden-snapshot) path: it requires the machine to boot
already with the code installed and the worker image built, and it does not
clone or download anything on its own. First **one** reference machine is baked
(see [Provisioning a Debian 13 VPS](#provisioning-a-debian-13-vps)) and a
snapshot is taken of it; only then is the rest of the fleet deployed by pasting
a script into the user-data of your VPS provider (DigitalOcean, Vultr, etc.)
when you create each instance **from that snapshot**:

1. Prepare the `CONFIG` block with, at a minimum, `ORCH_BASE_URL`,
   `SERVER_ED25519_PUBKEY` and `ORCH_ENROLLMENT_TOKEN`. Adjust `WORKER_COUNT`
   according to how many workers you want per machine.
2. Paste the `CONFIG` block followed by
   `exec bash /root/orcaster/setup-scripts/debian13-worker/cloud-init-worker.sh`
   as user-data when you create the instance. The path is that of the root copy
   of the code, which must be in the snapshot (see [Golden
   Snapshot](#golden-snapshot)).

When it finishes, cloud-init leaves the machine with the fleet **started and
enrolled**. The log is left in `/var/log/orcaster-cloud-init.log`; an abort of
the origin guard does not end up there but only in
`/var/log/cloud-init-output.log`. The process prints two phases:

```
=== 1/2 · Client deployment: N container(s) started ===
=== 2/2 · Fleet enrolled: N worker(s) visible on the server ===
```

If phase `2/2` does not arrive within `ENROLL_WAIT` seconds (120 by default),
the script aborts with
`Enrollment timeout (120s): worker(s) not enrolled: …` (see
[Fleet enrollment timeout](#fleet-enrollment-timeout)).

<details>
<summary>Technical Information</summary>

`cloud-init-worker.sh` is the user-data entry point, used only with a golden
snapshot: it requires the installed
code's `RELEASE` to be readable and the worker image to exist
(`should_bootstrap` in `lib.sh`); if either is missing, it aborts indicating
that a release must be pushed with `ops/release/push-release.sh` from the
operator's machine, without trying to clone or download anything. With both
present, it validates the `CONFIG`, writes `/etc/orcaster/setup.env`, runs a
`healthz` preflight against `ORCH_BASE_URL`, runs `run-all-worker.sh --from 02`
and then `run-fleet.sh`.

`run-fleet.sh` iterates `1..WORKER_COUNT`: for each worker `i` it generates
`/etc/orcaster/env.i` (`stamp_worker_env`, preserving credentials if they
already existed) and runs
`make up client N=$i AUTOSTART_SYNC=0 EGRESS_ISOLATION=$EGRESS_ISOLATION
WORKER_MEMORY=$WORKER_MEMORY CONFIG_DIR=$CONFIG_DIR ETC_SETUP_ENV_FILE=$CONFIG_DIR/setup.env
WORKER_RO_MOUNTS=$WORKER_RO_MOUNTS`, followed by `06-autostart.sh` for that
worker. It does not pass `WORKER_ENV` or `WORKER_PUBKEY`: the Makefile resolves
`$CONFIG_DIR/env.i` and `$CONFIG_DIR/server-ed25519.pub` itself (it only passes
them to an older Makefile that lacks this resolution, as on a rollback).
Before the loop it checks, as the worker user, that each directory of
`WORKER_RO_MOUNTS` exists, that this user can traverse it and that the
container's uid can read it through the "other" bits
(`check-ro-mounts.sh --host`):
the loop stops each worker's systemd unit before recreating it, so a missing
directory has to cut it short beforehand. With a non-empty `WORKER_MEMORY`,
`podman run` receives `--memory` and the worker reports its cgroup's
`memory.max` as `ram_mb` instead of the host's RAM; the format is validated
(`^0*[1-9][0-9]*[bkmgBKMG]$`) both in `cloud-init-worker.sh` (before the
bootstrap) and in `load_env` of `lib.sh`. The value is passed here explicitly,
but `up-client` also resolves it on its own from
`setup.env` when it comes neither through the environment nor through the
command line (`ifeq ($(origin WORKER_MEMORY),undefined)` in the `Makefile`), so
a manual rerun does not lose the cap. It prunes the leftover workers if
`WORKER_COUNT` went down compared with a previous run (see
[Reducing the size of a fleet](#reducing-the-size-of-a-fleet)). If
`ORCH_ENROLLMENT_TOKEN` is empty in `setup.env`, the `env.N` carries no
`ORCH_API_KEY` or previous token and the `orcaster-data-N` volume has no
`state.json` with `worker_id` and `api_key`, it aborts early with
`Worker N has no credentials: ORCH_ENROLLMENT_TOKEN is empty in setup.env…` instead
of leaving a container in a silent crash-loop. A `setup.env` without a token is
not enough to abort, because the rerun keeps the `env.N`'s previous token. An
already enrolled worker (with that `state.json`) starts without a token, so the
rerun goes ahead even if its `env.N` carries neither a token nor `ORCH_API_KEY`,
for example because it was deleted or regenerated and the volume survived.

Each worker's name comes from `WORKER_NAME_PREFIX` (or from the hostname, if it
is empty) plus the index: `PREFIX-N`. That applies only to a new slot, without
an `env.N` or with an empty `ORCH_WORKER_NAME`. A rerun keeps the
`ORCH_WORKER_NAME` that the `env.N` already carries, even if it does not follow
that pattern (a `worker1` worker does not become `worker1-1`) and even if
`WORKER_NAME_PREFIX` has changed. A name made only of spaces counts as empty. A
worker pruned when `WORKER_COUNT` goes down loses its `env.N` and, with it, the
name: if the fleet grows again, it reuses its identity but comes back as
`PREFIX-N`. To rename, see
[Renaming the worker](#renaming-the-worker).

Relevant `setup.env` variables: `WORKER_USER` (`orcaster`),
`WORKER_HOME` (`/home/orcaster`), `INSTALL_DIR`
(`/opt/orcaster`),
`WORKER_COUNT` (`1`), `WORKER_NAME_PREFIX`, `EXTRA_ENV`, `WORKER_MEMORY`
(empty = no limit), `WORKER_RO_MOUNTS` (empty = no extra mounts),
`ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION` and
`ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS` (the worker's disk gate,
empty = worker default; see
[Freeing disk when the worker fills up](#freeing-disk-when-the-worker-fills-up)),
`ENROLL_WAIT` (`120`), `ENROLL_POLL` (`5`),
`EGRESS_ISOLATION` (`1`), `RETRY_MAX` (`5`), `RETRY_DELAY` (`5`).

</details>

<a id="golden-snapshot"></a>
### Golden Snapshot

The cloud-init deployment (above) **requires** a pre-baked virtual machine image
("golden snapshot") with the code and the worker image already built:
`cloud-init-worker.sh` does not clone or download anything, so an instance
created from an unbaked base image has no way to bring the code on its own. The
reference image is produced by provisioning **one** VPS through the manual path
(code pushed with `ops/release/`, [Provisioning a Debian 13
VPS](#provisioning-a-debian-13-vps) completed up to the started worker) and
taking a snapshot of it. The snapshot must contain the root copy
`/root/orcaster`, which `install-release.sh` leaves: the
user-data runs the script from there.

> **Warning:** before taking the snapshot, purge the data volumes of every
> worker that started during the baking. If you do not, every new machine
> created from the snapshot **clones the same worker identity** (same
> `worker_id`), which confuses the server.

Purge as the `orcaster` user, not as `root`:

```bash
# `cd /` first: sudo inherits the current directory, and from a root SSH
# session (`/root`, not readable by other users) podman does not even start.
cd /
sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$(id -u orcaster) \
    podman volume rm orcaster-data-1 orcaster-data-2 …
```

> **`make purge client` run as `root` does not work for this**: it operates on
> the rootful storage (`root`'s), not on the rootless storage of the `orcaster`
> user, and falsely reports success without having deleted anything.

**Also purge `/etc/orcaster` on the reference VPS**, not only the volumes.
The host's live configuration lives there, outside the volumes: `setup.env` and every `env.N` carry the multi-use
`ORCH_ENROLLMENT_TOKEN` and whatever `EXTRA_ENV` declares (including
any credentials you put there). A snapshot that takes them along replicates
those credentials on **every** machine that boots from it, starts with the token
already consumed and, since `cloud-init-worker.sh` rewrites `setup.env` but
does not delete pre-existing `env.N` files (and `run-fleet.sh` preserves each
one's `ORCH_API_KEY` and `ORCH_WORKER_NAME`), it also clones the identity and
the name of the reference host, which is exactly what the volumes warning tries
to avoid:

```bash
sudo rm -rf /etc/orcaster
```

### Notes by operating system

| OS | Notes |
|----|-------|
| **macOS** | Requires Podman Machine (`podman machine init && podman machine start`). The default `make` is GNU make. |
| **Linux x86_64 / arm64** | `make` is usually GNU make. Rootless Podman. |
| **FreeBSD 15+** | Use `gmake` instead of `make`. Requires additional configuration of the linuxulator and of `pf` for NAT. See `client/README.md`, section "FreeBSD 15 setup". |
| **Windows** | There is no native support. Use WSL2 and follow the Linux instructions. |

<a id="multiple-workers-on-one-host"></a>
### Multiple workers on the same host

Each additional worker needs its own environment file, with its own identity
and its own enrollment token, and starts with the `N` parameter:

```bash
cp client/.env.example client/.env.2
$EDITOR client/.env.2
make up client N=2
```

> **On a host deployed by tarball (the fleet), the file does NOT go in the
> checkout.** `client/.env.2` is the Makefile's fallback for a development
> machine (without config in `/etc/orcaster`); on a production host the code in
> `/opt/orcaster` is replaced entirely on every release, so a
> `client/.env.2` inside it disappears on the next `push-release.sh` and worker
> 2 does not start again. On a fleet host the file goes in
> `/etc/orcaster/env.2` (without a leading dot; mode `0640 root:orcaster`),
> `make up client N=2` takes it from there without arguments, and
> `WORKER_COUNT` in `/etc/orcaster/setup.env` has to go up to 2: it is what
> `run-fleet.sh` iterates to stamp the `env.N` files and what
> `install-release.sh` uses to know how many containers to recreate. With
> `WORKER_COUNT=1`, the deployment recreates only worker 1 and worker 2 stays
> with the bind mount of the previous code.

`N=2` automatically derives the container name (`orcaster-worker-2`), its state
volumes (`orcaster-data-2`, `orcaster-cache-2`) and the environment file it
reads (`client/.env.2`). Use the same `N` in the rest of the operations on that
worker, for example `make logs client N=2` or `make purge client N=2`.

Do not reuse the same `N` (or the same `client/.env.N`) between different
instances: the identities in `state.json` would collide.

> Do **not** use the `N` suffix together with volume or container names set by
> hand (`WORKER_NAME=`, `WORKER_DATA=`, `WORKER_CACHE=`): the supported
> mechanism, and the one `06-autostart.sh` and `run-fleet.sh` use, is `N=`.
> Sharing the same data volume between two different instances makes their
> identities (`state.json`) collide.

<details>
<summary>Technical Information</summary>

- The worker runs `POST /v1/workers/register` only on the first start (when
  `state.json` does not exist). The server answers `{ worker_id, api_key }`;
  those credentials are persisted in the `orcaster-data` volume. From then on
  the worker reuses them on all polls.
- The enrollment token supports multiple uses (**Max uses**, up to 1000; see
  [Worker enrollment tokens](#worker-enrollment-tokens)): each successful
  registration adds one to its use counter until the quota is exhausted. If a
  worker's state volume is lost, it can register again with the same token as
  long as it has uses and validity left, or you can generate a new one in the
  dashboard.
- `make up client` essentially runs:

  ```
  podman run -d --name orcaster-worker --restart unless-stopped \
    --env-file client/.env \
    --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --mount type=tmpfs,dst=/home/orcaster,tmpfs-size=256m,tmpfs-mode=0700,U=true \
    --pids-limit 1024 \
    -v orcaster-data:/var/lib/orcaster -v orcaster-cache:/var/cache/orcaster \
    -v client/src/orcaster_worker:/app/src/orcaster_worker:ro \
    -v client/server-ed25519.pub:/var/lib/orcaster/ed25519.pub:ro,z \
    localhost/orcaster-worker:local
  ```

  With `WORKER_MEMORY` defined (e.g. `make up client WORKER_MEMORY=28g`, or
  declared in the fleet suite's `setup.env`, which the target itself reads when
  the variable does not come another way), it adds `--memory` to the
  `podman run`; if it is defined in neither, the container runs without a
  memory limit and the worker warns about it on startup (see
  [Deploying a Fleet](#deploying-a-fleet)).

  With `WORKER_RO_MOUNTS` defined (e.g. `make up client
  WORKER_RO_MOUNTS="/opt/example-ro"`, or declared in `setup.env`, which the
  target reads just like `WORKER_MEMORY`), it adds one `-v <dir>:<dir>:ro` per
  directory at the end of the mounts, without `z` or `Z`. Before the build and
  the `podman rm -f` (also with `make -j`) it runs
  `setup-scripts/debian13-worker/check-ro-mounts.sh --host` as the user who
  invokes make, which also requires the directory and all its contents to be
  readable by "other", which is how the container's uid reads them; if an
  entry does not pass, the target fails and the previous container keeps
  running. If make cannot read `setup.env`, it prints
  `WARNING: could not read WORKER_RO_MOUNTS from the host setup.env` with
  the reason.

  With `EGRESS_ISOLATION=1` (default), it also connects the worker to the
  `orcaster-egress` network (`--internal`) and exports `HTTPS_PROXY`/
  `HTTP_PROXY`/`NO_PROXY` pointing to the `orcaster-proxy` sidecar (see
  [Isolating a Worker's Network (Egress)](#isolating-a-workers-network-egress)).
  `make up client` depends on `make up-proxy`, which in turn validates Podman
  (`.podman-check`, error `ERROR: 'podman' not found in PATH.`) and the
  `aardvark-dns` version (`.aardvark-check`).
- `WORKER_NAME`, `WORKER_DATA`, `WORKER_CACHE` and `WORKER_ENV` can also be
  set by hand in `make up client` instead of being derived from `N`
  (`WORKER_ENV` is derived from `N` and from whether the host has config in
  `/etc/orcaster`; see the `Makefile`), but the usual way is to use `N` (see the
  warning above about not mixing both mechanisms).
- `N` accepts non-numeric values (for example, `N=staging`). With an empty `N`
  (the default), no suffix is added. `make down client` without `N` takes down
  only `orcaster-worker` and its `orcaster-proxy` sidecar; suffixed workers
  keep running. With an explicit `N`, it takes down only that instance and its
  sidecar. `make down client ALL=1` sweeps `orcaster-worker` and any existing
  `orcaster-worker-<numeric N>`, each with its egress sidecar (it uses the
  regular expression `^(orcaster-worker|orcaster-proxy)(-[0-9]+)?$`), lists the
  containers before deleting them and is not combined with `N`. The egress
  networks (`orcaster-egress`, `orcaster-egress-out`) are shared among all the
  instances on the host, without a suffix; the egress allowlist volume
  (`orcaster-egress-vol[-N]`) is specific to each instance. That is why neither
  `make down client` nor `make purge client` deletes those networks (they would
  leave the host's other workers without outbound access), whereas `make purge
  client` does delete the allowlist volume of the worker it purges.
- `make up client` mounts `client/src/orcaster_worker/` as a read-only volume
  in the container; code changes are visible after `make restart client`
  without a rebuild.
- Podman volumes created:
  - `orcaster-data` (`/var/lib/orcaster`): worker state (`state.json`,
    idempotency sqlite, logs).
  - `orcaster-cache` (`/var/cache/orcaster`): `uv`/`pip` cache and venvs
    reusable between tasks.

</details>

---

<a id="isolating-a-workers-network-egress"></a>
## Isolating a Worker's Network (Egress)

By default, each worker isolates the network egress of the tasks it runs:
tasks can only reach the domains that you explicitly authorize. This is
controlled at two levels: one at the **worker** level (always active unless you
expressly disable it, described in this section) and another at the **task**
level (what you declare when creating a task, see [Isolation (env /
egress)](#isolation-env--egress) in [Dispatching a New
Task](#dispatching-a-new-task)).

### Default egress (worker level)

With `EGRESS_ISOLATION=1` (the default value), `make up client` connects the
worker to an internal network (`--internal`, no direct access to the Internet)
and starts, next to it, a sidecar container (`orcaster-proxy`) that acts as the
only exit point. Every outgoing connection of a task goes through that proxy,
which only allows `CONNECT` traffic (HTTP/HTTPS) to the domains on an allowlist.

To disable this isolation entirely on a worker (no sidecar, no internal
network):

```bash
make up client EGRESS_ISOLATION=0
```

<a id="the-known-limit"></a>
<a id="the-two-known-limits"></a>
### The known limit

**Proxy variables are not inherited in strict `task_env` mode.** If you declare
`task_env` for a task, it starts with a clean environment: only the variables
you listed are forwarded, plus a fixed base (for example `HOME`, `LANG`, `TZ`,
`TMPDIR`, certificates). `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` are **not**
in that base. If your commands need to reach the Internet through the worker's
proxy, you must declare those three variables explicitly in **Environment
variables**; otherwise, any library that depends on those environment
variables to route its connections will not find the proxy.

This limit also covers the **installation of dependencies** of the task: the
`uv pip install` of the ephemeral venv runs with that same clean environment.
If the task declares `task_env` and its `requirements` need network access (for
example a package declared by URL), include `HTTPS_PROXY`, `HTTP_PROXY` and
`NO_PROXY` in `task_env`; without them the installation fails within seconds
with a DNS resolution error (for example `dns error` or `Name does not
resolve`), not with a connection timeout, even if the domain is allowed in the
`egress_allowlist`.

**A declared name that the worker does not have is not injected.** If
`task_env` lists a variable that the worker's environment does not have and that
no stored secret of the task delivers, the task starts without it and does not
fail because of that. The worker log records one warning per run, with the
task id (the same one that `orcaster task` shows, as `task_id=<id>`) and the
missing names (names only, never values). A `task_env` entry that is not a
valid variable name (for example `API_TOKEN=value`, written as in `podman -e`)
or that starts with the prefix of a known token (for example `hf_...`) is never
recorded: the warning only reports how many there were. The warning lists at
most 50 names and says how many more are missing; a name longer than 128
characters is not written, only counted. It is read by whoever operates the
worker, not by whoever creates the task: the task's output does not include it.
If a task fails because of a variable that it did declare, ask whoever operates
the worker to look for that warning by the task id.

**An optional-by-design name warns on every run, and that is expected.** The
warning does not tell a forgotten name apart from one that the task declares on
purpose even though the worker does not have it. If your `task_env` includes a
name that only some workers have —for example `MODEL_PROVIDER`, which a worker
without a model never sets—, every run on a worker that does not have it
records that `WARNING`, always with the same names, and the task runs anyway.
It is not a symptom: it is the warning doing what it says. To separate the
noise from a name that is truly missing, compare the names in the warning with
the ones you expect to be missing on that worker; if the warning only names
those, there is nothing to fix. There is currently no way to mark a name as
optional so that it warns at a lower level or not at all.

> The task's `egress_allowlist` is applied before the venv is created, so it
> also covers the installation of dependencies (`uv pip install`): a
> `requirement` declared by URL works when its domain is allowed in the task.

### Reference allowlist for tasks that download packages or datasets

For tasks that download packages, datasets and code from public hosts, this is
the usual list of domains (it saves you from debugging proxy `403` errors one
by one):

```
github.com
api.github.com
codeload.github.com
objects.githubusercontent.com
release-assets.githubusercontent.com
raw.githubusercontent.com
pypi.org
files.pythonhosted.org
```

<a id="enabling-strict-mode-orch_enforce_isolation"></a>
### Enabling strict mode (`ORCH_ENFORCE_ISOLATION`)

With `ORCH_ENFORCE_ISOLATION=true` in `client/.env`, a task that arrives
**without** `task_env`/`egress_allowlist`, or with those fields malformed, is
treated as if it had declared an empty list (clean environment, all network
egress denied), instead of inheriting the worker's full environment.

> Enable this mode **after** draining any queue of tasks created before
> explicit isolation was declared: otherwise, those older tasks will fail for
> lack of network or variables, instead of inheriting the environment as
> before.

**Default value.** The code default is `true`: a worker without the variable
runs strict. `push-release` and `install-release` do not stamp the line, so
keep it explicit in each worker's environment file. The worker installer writes `ORCH_ENFORCE_ISOLATION=true` in the
environment file of every new worker. If the environment file already exists
and does not have the line, the installer writes `ORCH_ENFORCE_ISOLATION=false`
explicitly, so the decision stays visible in the file. An explicit `false` keeps the legacy mode. While the flag is off, every
worker shows a `WARNING` on startup warning that a task without `task_env`
inherits the worker's full environment, including `ORCH_ENROLLMENT_TOKEN` and
`ORCH_API_KEY` (names only, never values).

**Check that the queue has no tasks without isolation.** Before enabling the
flag, list the workspace's pending tasks with their payload and look for the
ones that do not declare `task_env` (or `egress_allowlist`, which strict mode
also promotes to an empty list):

```bash
orcaster task list --status queued --status dispatching --status paused \
  --include-payload --limit 200 --json \
  | jq '[.tasks[] | select((.payload | has("task_env") | not)
                       or (.payload | has("egress_allowlist") | not)) | .id]'
```

If the response carries a `next_cursor` other than `null`, repeat with
`--cursor <value>` until it is exhausted. An empty list on all pages indicates
that the queue has no tasks that depend on the legacy mode. The ones that
appear are left to finish, canceled or recreated declaring `task_env`.

**Variables that require isolation (`ORCH_ISOLATION_REQUIRED_ENV`).** A worker
can have in its environment variables that must never reach a task without
strict mode, for example a credential for an external service. Their names are
declared in `ORCH_ISOLATION_REQUIRED_ENV`, separated by commas or spaces (on a
fleet host, in the `ISOLATION_REQUIRED_ENV` variable of `setup.env`, which the
installer copies to each `env.N`):

```bash
ISOLATION_REQUIRED_ENV="EXAMPLE_PROTECTED_VAR"
```

The worker's built-in list is empty: no variable is protected until the
operator names it. The list is additive (no source removes a name that another
added) and is compared by exact name, without patterns and without looking at
values. On startup, the worker records in an `INFO` the names it checks
(`startup: isolation-required variables checked: ...`, or `(none)`). A badly
written name in the list (with a hyphen, an `=` or a wildcard) stops the worker
with an `ERROR` before it registers, instead of ignoring the list. With one of
these variables present, the worker must run with
`ORCH_ENFORCE_ISOLATION=true`. Without it, a task that arrives without
`task_env` inherits the worker's full environment, variable included, and
whoever can create tasks in the workspace can print it in their output. With
it, only a task that names the variable in its `task_env` receives it. That
closes the inherited route and nothing more: a member can create a task that
names the variable and read it, and the value that comes from `EXTRA_ENV` is
not masked in the output. That is why such a credential must be dedicated and
narrowly scoped. Nor is it a sandbox: the flag closes only the inheritance of
the environment (the full copy of the legacy mode), not a task that reads the
worker's process or files, which runs with the same uid (see also the
limitation of the egress allowlist).

**The worker refuses to start.** If the worker starts with a variable from
`ORCH_ISOLATION_REQUIRED_ENV` in its environment (with a non-empty value) and
without `ORCH_ENFORCE_ISOLATION=true`, its log shows an `ERROR` that starts
with `startup:`, names the variable (never its value) and the process exits
with code 1 before registering, without claiming any task. The message repeats
the recipe from this section: move the value to the workspace's central secret
and remove it from the worker's environment, or else drain the queue of tasks
without `task_env`, declare the flag and recreate the container. If the value
moves to the central secret, the tasks that need it must declare the variable
in their `task_env`, because the server only delivers secrets to the declared
names; the tasks queued without that declaration are drained or recreated
before removing the variable from the worker's environment, or they will run
without it. The check looks only at the variable's name. Since the container
runs with `--restart unless-stopped`, a worker in this state enters a crash
loop: Podman relaunches it and it exits again with the same error. To break
it, apply the recipe and recreate the container; a `podman restart` does not
re-read the environment file. On a fleet host, the tarball deploy has its own
check before replacing the code (see
[Deploying a new version to the
fleet](#deploying-a-new-version-to-the-fleet)).

It is declared in the worker's environment file (`client/.env`, or
`/etc/orcaster/env.N` on a production host), just like `ORCH_CAPABILITIES`. On
a host with a fleet, `run-fleet.sh` rewrites each `env.N` from `setup.env`, so
declare it in `setup.env`, which has a variable of its own for the flag:

```bash
ORCH_ENFORCE_ISOLATION=true
```

It is also accepted inside `EXTRA_ENV`; if `setup.env` and `EXTRA_ENV` declare
it with different values, the installer aborts. Then run `run-fleet.sh` again
(a plain `make up client N=<n>` does not read `/etc/orcaster/env.N`); on a
standalone worker, add it to `client/.env` and run `make up client`. A
`restart` does not re-read the environment file.

<details>
<summary>Technical Information</summary>

The sidecar (`python -m orcaster_worker.egress_proxy`) listens on
`0.0.0.0:$ORCASTER_PROXY_PORT` (default `3128`) and only accepts `CONNECT` to
ports 80 and 443, re-reading `allow.txt` on every request. Messages in
`podman logs orcaster-proxy`: `ALLOW CONNECT <host>:<port>`,
`DENY CONNECT <host>:<port>`, `DENY CONNECT malformed host <host>`.
Responses to the worker: `403 Forbidden` (denied or malformed host),
`405 Method Not Allowed` (non-CONNECT), `502 Bad Gateway` (upstream
unreachable), `200 Connection Established`.

The task's allowlist (`allow.txt`) is signed by the worker with the
`ORCH_EGRESS_KEY` key, a per-worker Podman secret that `make up client` creates
(the first time) and delivers to the worker and its sidecar. On startup, the
sidecar records `per-task allowlist=signed` and the worker, `startup: per-task
egress allowlist is signed`. If the sidecar receives an `allow.txt` without a
valid signature, it records a `WARNING` for each version of the file:
`egress allowlist refused: <reason>; per-task entries ignored, base allowlist
only (file <path>)`. It means that it ignores what the task declared and
applies only the base allowlist: the task (or something running in its
container) wrote the file, or the worker and the sidecar do not share the same
key, and the `DENY` lines that follow this one are expected. If it happens on a
normal task, recreate the worker and its sidecar with `make up client` (the
secret is created if missing). Without a key (`ORCH_EGRESS_KEY` not defined),
the sidecar accepts the file without a signature and warns about it with a
`WARNING` on startup.

To rotate the key, [pause the worker](#pausing-and-resuming-a-worker) and wait
for it to finish what it has in progress. Then, as the rootless user, run
`podman secret rm orcaster-egress-key[-N]` and, immediately,
`make up client N=<n>`, which recreates the secret, the sidecar and the worker;
once it is back online, resume it. Do not use `make purge client` to rotate: it
also deletes the worker's data volume (`state.json`, its enrollment identity)
and forces it to register again. Podman resolves the secret every time a
container starts: one created with it does not start if the secret no longer
exists (`podman start` fails with `no secret with name or id ...: no such
secret`), nor does the `systemd --user` unit when the machine boots or on its
restarts. So do not leave the secret deleted while the containers exist. To go
back to a previous release, deploy it first (it recreates the containers
without the secret) and delete the secret afterwards.

The fixed passthrough base in strict `task_env` mode
(`task_env.BASE_ENV_PASSTHROUGH`) is: `HOME`, `LANG`, `LC_ALL`,
`LC_CTYPE`, `TZ`, `TMPDIR`, `SSL_CERT_FILE`, `SSL_CERT_DIR`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `PYTHONDONTWRITEBYTECODE`,
`PYTHONUNBUFFERED`, plus `PATH`. Any variable with the `ORCH_` prefix is
always discarded, even if declared explicitly (denylist).

`ORCH_ENFORCE_ISOLATION` is `true` by default in the code (`config.py`,
alias `ENFORCE_ISOLATION`); the installer stamps `true` on new workers
(`stamp_worker_env` in `setup-scripts/debian13-worker/lib.sh`).
`ORCH_ISOLATION_REQUIRED_ENV` (alias `ISOLATION_REQUIRED_ENV`) is merged with
`BUILTIN_ISOLATION_REQUIRED_ENV`, which is empty.

</details>

---

## Checking Fleet Status

The dashboard is the main screen. It has two zones that refresh on their own
every 3 seconds, with no need to reload:

- **Workers** (left panel): the fleet of connected machines.
- **Tasks** (right panel): the queued tasks and their progress.

In the top bar, next to the navigation links, two indicators appear that
refresh on their own:

- A **storage** indicator: it shows `used / quota` of the workspace's
  artifacts (for example, `120 / 500`). Hover over it to see the details of
  **Quota**, **Quota source** and **Disk free**. **Quota source** tells you
  where the workspace quota comes from: **Workspace override** (the host set
  a quota of its own for this workspace) or **Server default** (the common
  server quota applies). The quota cannot be changed from the interface (see
  [Artifact quota per
  workspace](#artifact-quota-per-workspace)). If storage fills up, the
  **⚠ storage full** warning appears and tasks that upload artifacts stop
  until space is freed.
- When the feature is enabled, an **artifacts** indicator with the counted
  artifacts: **Produced** and **Uploaded**. It is off unless the administrator
  configures it.

### Reading the Workers panel

When the whole fleet shares the same platform, the **Workers** heading carries
a subtitle with the common operating system and architecture (for example,
`· linux · aarch64`). If the fleet is heterogeneous, that subtitle does not
appear and each card shows its own platform.

Each worker is shown as a card with:

- The worker's **Hostname** (a link to its detail page).
- A status badge: **online**, **paused** or **offline**
  (see [Worker states](#worker-states)).
- The name assigned to the worker (if it has one) and its platform
  (operating system · architecture), when the fleet is mixed.
- An **N active** badge when the worker has runs in progress.
- The worker **version** (for example, `vX.Y.Z`), or **no version
  reported** if it has not reported one yet. Next to it appears the **No task
  secrets** mark when the worker reports no version or one older than the
  minimum that supports task secrets: the server does not hand it tasks that use [task
  secrets](#task-secrets).
- **RAM** and **Disk**: memory and disk used out of the total, with a usage
  bar. If the worker has not reported that data yet, it shows **unknown**.
- In the footer, how long ago the last heartbeat arrived (for example,
  `12s ago`). If it never reported, it shows **no heartbeats**.

If no workers are connected, the panel shows **No workers registered.**
with the instruction to start one (see [How do I connect a
worker?](#how-do-i-connect-a-worker)).

### Reading the Tasks panel

The **Tasks** title carries a subtitle that summarizes the queue. Depending on
the situation, that subtitle shows:

- The total number of tasks (for example, `153 total`).
- When you filter, how many match (for example, `4 of 153`).
- When all tasks finished successfully, `N completed`.
- In a mixed fleet, a breakdown by status ordered by urgency
  (for example, `2 failed · 12 queued · 3 dispatching`).

Between the filter bar and the table, a **progress bar** shows how many tasks
have already finished, for example `40 of 52 tasks processed (37
completed · 3 failed)`, with its percentage on the right. The bar paints
completed work green, failed work red and pending work gray, and it updates
on its own together with the table.

Below the bar, a note says what it is measuring:

- **The version chosen in the version selector.** By default, `Scope:
  current version of each tool.` (plus `plus unversioned tasks` if there are
  tasks with no detected version); if you choose another, the note says so
  (for example, `Scope: all versions.`).
- **Canceled tasks, if there are any.** They do not count toward the bar's
  total (a cancellation is not pending work), so the note says how many were
  left out; the subtitle does include them.
- **Active filters.** The status, outcome, search and artifact filters trim
  the table but not the bar; with any of them active, the note warns you.

If there are no tasks, the bar shows **No tasks yet**. When all the tasks of
the version have finished, **All tasks processed** appears.

The task table has these columns:

| Column | What it shows |
|--------|---------------|
| (checkbox) | Checkbox to select the task for bulk operations |
| **ID** | The task number (link to the detail page) and its status badge |
| **Payload** | Summary of the command the task runs |
| **Target** | The dispatch mode and the target number |
| **Workers** | How many workers completed the task out of those required |
| **Runtime** | When the last attempt started and finished, in your local zone (or `···` if it is still running, **not started** if it has not started yet) |

In the **ID** column, next to the status badge, a terminal task may also show
an **outcome** badge (for example, `no_result`): the nuance with which it
closed (see [Analyzing Completions and Failures
(Outcomes)](#analyzing-completions-and-failures-outcomes)). If the task comes
from a **Fork**, the **⑂** glyph appears with the **from #N** link to its
parent task.

In the **Workers** column, if the task targets a single worker, the name of
the assigned worker is shown (or **pending** if none has taken it yet).
If it targets several, the completed fraction, a progress bar and the first
workers are shown, with `+N more` if there are more.

When a task is in the **failed** state, a short failure hint appears under its
badge: the exit code (`exit N`) and the last line of the error, so you can
understand what broke without opening the detail page.

The **Target** and **Workers** columns are sortable: click the header to
sort; click again to reverse the order.

> To cancel an individual task use its detail page (the **Cancel
> task** button); the table has no per-row cancel button. To act on
> several at once, use the checkboxes and the bulk action bar (see
> [Operating on Several Tasks in Bulk](#operating-on-several-tasks-in-bulk)).

If there are no tasks, the table shows **No tasks yet.** with a
**+ New task** button.

### Filtering and searching tasks

Above the table, a bar lets you narrow what you see:

- **Status chips:** the **All**, **queued**, **dispatching**,
  **paused**, **completed**, **cancelled** and **failed** buttons. Activate one
  or several to see only those tasks. **All** shows them all. Each chip has a
  short tooltip on hover (for example, **queued** = *Waiting for a
  worker to claim it*).
- **Outcome chips:** besides the statuses, chips named after an **outcome**
  (for example, `no_result`) may appear. Activate them to see only the tasks
  that closed with that nuance (see [Analyzing Completions and Failures
  (Outcomes)](#analyzing-completions-and-failures-outcomes)). They are sorted
  from most to least frequent, and on hover they show how many tasks have that
  outcome within what the **Version** lens lets you see.
- **N more:** when the outcome chips do not fit in the two lines of the
  rail, the extra ones sit behind an **N more** button at the end of the
  second line; click it to see them all and **less** to fold them again. An
  outcome chip that is active is never hidden there: if needed, the rail takes
  an extra line to show it.
- **Version** (version lens): the dropdown labeled **Version**
  narrows the dashboard to the version of a tool. Its default value,
  **Current**, shows the most recent version of each tool; **All
  versions** does not filter; you can also choose a specific version or
  **Other** (tasks with no recognized version). If the server has a plugin
  that recognizes the tool, versions appear grouped by the tool that
  produced them, for example **example-tool** (with labels such as `1.2.0`, or
  `1.2.0 / opt` when the task runs with an optional stage); without a plugin,
  every task falls under **Other**. If several versions of a tool
  tie as the most recent (for example, `1.2.0 / opt` and `1.2.0`), all
  carry the **(current)** mark and all fall under **Current**. See [Version
  (variant)](#glossary).
- **artifacts:** the **artifacts** button expands filters on the files the
  task produced: **Has artifacts** (**Any** / **With artifacts** /
  **Without artifacts**), **Path glob**, **Content type** and **SHA-256**.
- **Search:** the field with the placeholder `payload, #id  /`. Type part of
  the command or a task number to filter. (The `/` shortcut moves focus to
  this field.)

When you filter, the active filters appear as removable tags above the
table; click the **×** of a tag to remove that filter. If no result
matches, the table shows **No tasks match these
filters.** with a **Clear filters** button.

At the bottom of the table, when there are many tasks, pagination appears: the
**Prev** and **Next** buttons, the **Page N of M** indicator and a
rows-per-page selector (**25 per page**, **50 per page**, **100 per page**).

<details>
<summary>Technical Information</summary>

- The dashboard is served at `GET /admin/`. The panels refresh with HTMX
  every 3 s: `GET /admin/workers` and `GET /admin/tasks`. With SSE enabled,
  the dashboard receives real-time updates via `GET /admin/events` and
  falls back to polling if SSE is disabled.
- The state of each worker (`online`/`paused`/`offline`) and its telemetry
  (RAM, disk) arrive with every worker heartbeat (idle 30 s / busy 5 s).
- Search does a partial match on `command` and `payload`; if the text is
  numeric, it also searches by `id` prefix.
- The **Version** lens corresponds to the `variant` parameter; its reserved
  tokens are `current` (default), `all`, `other`, `<kind>` and
  `<kind>~<label>`. A task's *variant* `(kind, label)` is derived
  automatically from the payload (`server/orcaster_server/task_variant.py`) with the
  extractors that the server's plugins register; the core ships none of its
  own. A plugin is a Python package installed in the server's environment that
  registers itself in the `orcaster.plugins` entry point group.
  The server loads all the installed ones at startup; `ORCASTER_PLUGINS`
  (comma-separated names) sets which ones take precedence and makes
  startup fail if any is not installed. A task that no extractor
  recognizes, and every task on a server without plugins, falls under
  **Other**; on that server, `GET /api/v1/tasks` rejects with `422` any
  explicit `<kind>` in `variant`.
- The artifact filter uses the parameters `has_artifacts`, `artifact_path`
  (glob), `artifact_type` (content-type) and `artifact_sha256` (prefix).
- The valid sort order is restricted to the columns `created_at`, `status`,
  `done` and `target_count` (whitelist on the server).

</details>

---

## Dispatching a New Task

A task is one or more shell commands that the fleet runs. To create
one:

1. Click **+ New task** (top right of the Tasks panel), or
   press the `n` key. The system opens the **New task** window.
2. In **Commands**, enter one command per line. Each line runs in
   order; if one fails, the rest do not run.
3. (Optional) In **Requirements**, enter one Python package per line.
   The worker installs them before running the commands.
4. Choose the **Target mode** (see [Dispatch modes](#dispatch-modes)).
   Depending on the mode, the form asks for:
   - **count**: enter **Target count** (how many copies to dispatch).
   - **worker(s)** or **all-subset**: select the workers in the
     **Workers** list.
5. (Optional) Expand the additional blocks to adjust more
   parameters: **Advanced options** (see below), [**Artifacts**](#collecting-artifacts)
   to keep produced files, and [**Isolation (env /
   egress)**](#isolation-env--egress) to control environment and network.
6. Click **Create task**.

The system confirms the creation and the task appears in the table. To
discard without creating, click **Cancel** or close the window.

### Dispatch modes

The **Target mode** defines how many workers, and which ones, the task reaches:

| Mode | What it does |
|------|--------------|
| **count** | Dispatches N copies to any available worker. The first N that claim it keep it, until **Target count** is reached. |
| **all-available** | One copy for every worker that is **online** and able to run it at the moment the task is created. If there is none at that moment, the task stays **waiting for an eligible worker** and is dispatched on its own when one joins (see [A task waiting for an eligible worker](#task-waiting-for-an-eligible-worker)). |
| **worker(s)** | Targets specific workers that you choose. A single worker receives the task directly; several compete within that subset. |
| **all-subset** | One copy for every worker of a subset that you choose. |

Below the modes, a one-line reminder of the selected mode appears.
Only **online** workers can be selected.

### Selecting workers (worker(s) and all-subset modes)

When the mode requires it, the **Workers** section appears:

1. Use the **Filter by hostname or id** field to narrow the list.
2. Check the workers you want. The counter (for example, `2 selected`)
   reflects the selection.
3. Helper buttons: **Select all online** checks all the available ones;
   **Clear** empties the selection.

### Advanced options

Inside **Advanced options**:

| Field | What it is for |
|-------|----------------|
| **Priority** | Task priority. Default `0`; a higher value runs first. |
| **Claim timeout (seconds)** | Maximum time an assigned worker has to claim the task. |
| **Execution timeout (seconds)** | Total time budget for all the commands together. Raise it for slow steps (for example, large downloads). |
| **Run at** | Earliest local date and time before which the task is not eligible. Applies only to **count** mode. |

The **View worker contract** block describes in detail how the worker
interprets the commands and requirements; it is optional.

### Collecting artifacts

Expand **Artifacts** if you want to keep the files that the task
writes:

1. Turn on **Collect output artifacts**. The task writes to the
   `$ORCASTER_ARTIFACTS_DIR` directory; with this option on, the worker
   collects and uploads those files when it finishes, and they become
   downloadable. If you leave it off, nothing is uploaded.
2. (Optional) In **Include**, list one pattern (POSIX glob) per line, relative
   to the artifacts directory (for example, `**/*.csv`). Empty means
   everything.
3. (Optional) In **Exclude**, list patterns to discard. **Exclude** takes
   priority over **Include**.
4. (Optional) Turn on **Required** so that the task **fails** if the artifact
   upload fails (for example, because of quota or disk). When off, upload
   failures are only logged and the task still completes.

### Isolation (env / egress)

Expand **Isolation (env / egress)** to control the environment and network of
the task:

1. Turn on **Enable isolation**. When off, the task runs in legacy mode (no
   isolation). When on, it runs with a clean environment and **all** network
   egress denied by default; only the variables and domains you list are
   forwarded.
2. In **Environment variables**, list one variable name per line that
   the task must receive (for example, `API_TOKEN`). The value comes from the
   workspace secret with that name, if it exists (see [Task secrets
   (Secrets)](#task-secrets)); otherwise from the worker's environment.
   Write only the name, never `NAME=value` or a token: a name uses
   letters without accents (A-Z, a-z), digits and underscore, and does not
   start with a digit. Names that start with `ORCH_` are rejected (they are
   worker secrets), and so are lines that start with the prefix of a
   known token (for example `hf_` or `ghp_`). The list allows up to 256 names,
   of up to 128 characters each.
   Empty = clean environment.
3. In **Egress allowlist**, list one allowed domain per line (for example,
   `files.example.com`). Empty = all network egress denied.

> Under strict isolation, if your commands need network access they must
> include the domain in **Egress allowlist**; otherwise the connection fails
> with a name resolution error.

**What happens if an Environment variables line is not a name.** When the task
is created (also when you **Fork**) the server checks each line and, if
any does not comply, it does not create the task and answers with a `422`
error that says, for example, `payload.task_env[1] is not a valid environment variable name; declare
the variable name only (letters, digits and underscores, not starting with a
digit), not NAME=value`. The number in brackets is the position of the line
in the list, counting from 0 (the form first discards empty lines
and the spaces at the start and end of each line): `[0]` is the first line
with text, `[1]` the second, and so on. The message never repeats what you
typed, because a badly written line may contain the value of a secret; the
server does not return it anywhere else in the response either. If the
line is a valid name that starts with `ORCH_`, the error says
`payload.task_env[N] may not declare ORCH_ vars`. If the line starts with the
prefix of a known token (`hf_`, `ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_`,
`github_pat_`, `glpat-`, `xoxb-`, `xoxp-` or `sk-`), the error says
`payload.task_env[N] looks like a credential (known token prefix), not a
variable name; declare the variable name only`, and it does not repeat the line
either. The comparison is case-sensitive and looks only at the start of the
line: `API_TOKEN` and `GITHUB_TOKEN` are valid names.

**What the `422` error returns and what it does not.** The error never repeats
the **Environment variables** line: its messages carry only the position in
brackets, and the server does not return the body you sent either (neither the
`input` field that normally accompanies it, nor the validator's internal
text). What **does** come back is, for each problem, the field that failed
(`loc`) and the reason (`msg`), and that is deliberate: without those two
things the error does not say what to fix. That is why a `422` may repeat text
that you put in **another** field of the body:

- a malformed `target` is named in the message, for example `target
  'worker:xyz' contains malformed UUID 'xyz'`;
- a key the server does not know appears in `loc` (the create and fork bodies
  do not accept extra keys).

It is your own text coming back and it is never the **Environment variables**
line, but keep it in mind if you store the body of a `422` in a log or in a
client: **do not put the value of a credential in any field of the body.** Values
go as secrets (see [Task secrets
(Secrets)](#task-secrets)).

**Maximum size of the list.** The server also rejects a list of more than
256 lines (`payload.task_env may have at most 256 entries`) and a line of
more than 128 characters (`payload.task_env[N] is longer than 128 characters;
declare the variable name only`, with `N` counted from 0 as above). Both
errors indicate only the position or the maximum, never what you typed. A line
of more than 128 characters gets this error even if it also starts with the
prefix of a token. Declare only the names the task needs.

> **This check is not a secret detector.** It rejects what does not have the
> shape of a name and what starts with the prefix of a known token. A
> credential in another format, such as an AWS key (`AKIA...`) or any string
> of letters and digits, has the shape of a name and is accepted. Do not paste
> a token or a value into **Environment variables**: store the values as
> secrets (see [Task secrets (Secrets)](#task-secrets)).

Via the CLI, this is declared with `orcaster task create --env GITHUB_TOKEN
--egress-allow files.example.com …`, so that the task sees only the
secrets and domains it needs. Each `--env` carries only the **name** of a
worker variable, never `NAME=value`: the value is supplied by the worker or by
a stored secret. `orcaster` checks each `--env` before sending anything to the
server, with the same shape rules as **Environment variables** (not the
maximum size of the list: only the server checks that, and it answers `422`),
and if one does not comply it does not create the task and exits with an
error, for example
`invalid --env #2: not a valid environment variable name (...); use --env NAME
to forward a variable from the worker, not NAME=value`. That final hint
accompanies the shape and token-prefix errors; the one for an `ORCH_` name
does not carry it. The number
after `#` is the position of the `--env` in the command, counting from 1
(`#1` is the first `--env`). Note that the server's `422` error
counts differently: `payload.task_env[0]` is the first entry, because it counts
from 0. Neither message repeats what you typed.

> This **per-task** isolation is independent of the network isolation that
> the worker applies by default at the level of the whole host. See
> [Isolating a Worker's Network (Egress)](#isolating-a-workers-network-egress)
> for the known limit (proxy variables are not inherited in strict
> `task_env` mode) and for how to turn it off.

<details>
<summary>Technical Information</summary>

- The form submits to `POST /api/v1/tasks` (via the dashboard). The `target`
  field is built according to the mode: `"N"`, `"all-available"`,
  `"worker:<id>"`, `"workers:<ids>"` or `"all:<ids>"`.
- The worker creates an ephemeral venv with `payload.requirements` (list of
  pip specifications) and runs `payload.commands` in order via
  `/bin/sh -c`. `commands` must be a non-empty list of strings.
- Every task is signed with Ed25519; the worker verifies the signature before
  running.

</details>

---

## Following a Task's Progress

To see the detail of a task, click its **ID** (`#N`) in the table.
The detail page refreshes on its own every 3 seconds.

In the header you will see:

- The task number (**Task #N**) and its status badge.
- The command summary, the target mode and number, the priority and the
  creation date.
- A breakdown of attempts by status (for example, `3 done · 1 failed`). If
  the task finished **completed** but there were failed attempts before, a
  **⚠** sign appears that warns you without alarming.
- If the task comes from a **Fork**, a **Forked from #N** line indicates the
  origin and whether the parameters were modified or are identical.
- If the task was retried in place, a **Retried N time(s) (in
  place)** line.

The **Executions** section lists each execution attempt:

| Column | What it shows |
|--------|---------------|
| **Attempt** | Attempt number (with the **latest** label on the most recent) |
| **Worker** | The worker that ran it (link to its detail page) |
| **Status** | The status of the attempt (see [Run states](#run-states)) |
| **Started** | When it started (local time; UTC in the tooltip) |
| **Finished** | When it finished (local time; UTC in the tooltip) |
| **Duration** | How long it lasted |
| **Exit** | The process exit code |
| (output) | The **output →** link to the full output |

If there are no attempts yet, the section shows **No executions yet**.

<details>
<summary>Technical Information</summary>

- The **Attempt** number is counted per (task, worker) pair, not globally:
  two attempts on different workers may both show `#1`.
- The detail page is served at `GET /admin/tasks/{id}`; the executions block
  refreshes by polling with `hx-select`.

</details>

---

<a id="reviewing-a-runs-output"></a>
## Reviewing a Run's Output

From the **Executions** section (on a task or worker detail page),
click **output →** on the attempt you are interested in. The **Execution
output** page shows:

- A status line with the task, the worker, and the attempt's **status** and
  **exit_code**.
- The full output log, with stdout and stderr **interleaved** in the
  order in which they were produced.

**Live view.** While the task is running, the page refreshes
automatically every 2 seconds and shows the **● live** indicator. There is no
need to reload the page: the output appears as the worker produces it.
As soon as the task finishes (with success, error or cancellation), the
automatic refresh stops.

**Downloading the log.** The **Download log** and **JSONL** links download
the full output: **Download log** as plain text; **JSONL** with one event
per line (useful for processing it). Both are available even while the
execution is still alive (they download what was captured up to that moment).

To go back, use the **← Task #N** link.

### Downloading the artifacts

If the task collected artifacts, the task detail page includes an
**Artifacts** section with the files produced by the last attempt. Each file
shows its path and size. As long as the file is still on disk, it can be
downloaded individually; when there are several such files, the **Download all
(zip)** link downloads them all in a single compressed file, preserving their
folder structure. If there were no artifacts, the section shows **No
artifacts produced by this task.**

**Collected artifacts.** An external retention process (not part of
Orcaster) may back up the content of an artifact to external storage and
mark it as collected, which frees the space it occupied on disk. That file keeps appearing in the list, as evidence
that the task produced it, but it no longer has a download link: instead it
shows the **Collected** badge together with the backup date. When you hover
over the badge, the tooltip explains *Bytes purged after being
backed up to external storage*. The **Download all (zip)** link only
appears if at least one artifact is still downloadable, and the
compressed file it generates excludes the collected artifacts.

<details>
<summary>Technical Information</summary>

- Individual download: `GET
  /admin/executions/{execution_id}/artifacts/{artifact_id}/download`. If the
  artifact was collected, it answers `410 Gone` with the backup detail.
- Bulk download: `GET /admin/executions/{execution_id}/artifacts.zip`.
  It excludes the collected artifacts; if all of them are collected, it also
  answers `410 Gone` instead of serving an empty zip.
- The JSON of `GET /admin/executions/{execution_id}/artifacts` includes the
  `collected_at` field (null while the artifact is still on disk).

</details>

---

## Intervening on a Task

### Canceling a task

Cancel a task that has not finished yet (it is not **completed**, **cancelled**
or **failed**):

- **From the detail page:** open the task and click **Cancel task** in the
  header.
- **In bulk:** select several tasks with the checkboxes and use **Cancel
  selected** (see [Operating on Several Tasks in
  Bulk](#operating-on-several-tasks-in-bulk)).

The system asks for confirmation with the title **Cancel task #N?** and lets
you record an optional reason (**Cancellation reason (optional)**) that is
kept in the [Audit log](#auditing-what-happened). Runs in progress receive
the cancellation on their next heartbeat.

### Retrying a task (Retry)

**Retry** runs the *same* task again: it keeps the same ID, the cache
and the attempt history. It is only available for terminal tasks
(**completed**, **failed** or **cancelled**).

1. Open the task detail page.
2. (Optional) Check **Retry on the same worker** if you want to pin the retry
   to the worker that ran the last attempt (it reuses its local cache, useful
   for debugging). By default it is unchecked: the retry goes to any eligible
   worker. If you pin it, the task waits if that worker is down, busy or
   retired.
3. Click **Retry**.

The **Retry** button is disabled while the task is not terminal. On an
**all-available** task, the **Retry on the same worker** option does not
apply: the retry takes a fresh snapshot of all the online workers.

**From the CLI and the API the default is the same.** `orcaster task retry <id>`
retries on any eligible worker, just like the unchecked box in the
dashboard and `POST /admin/tasks/{id}/retry` with no body. To pin the
retry to the worker of the last attempt you have to ask for it:

```bash
orcaster task retry 123                       # any eligible worker (default)
orcaster task retry 123 --force-same-worker   # pinned to the worker of the last attempt
```

> **Note.** The CLI does not pin the retry to the same worker unless you pass
> `--force-same-worker`, the same as the dashboard and the API.

### Forking a task (Fork)

**Fork** creates a *new* task from the parameters of the current one,
linked to it as its origin (parent). Use it when you want to relaunch with
adjustments.

1. Open the task detail page.
2. Click **Fork…**. The system opens the **New task** window with the
   parameters preloaded.
3. Adjust what you need and click **Create task**.

> **Retry vs. Fork:** **Retry** reuses the same task in place (same ID,
> keeps cache and history). **Fork** queues a new task from
> these parameters (editable before creating), linked to the original as
> its parent.

### Deleting a task

On the detail page of a terminal task (**completed**, **cancelled** or
**failed**), the button changes to **Delete task**. When you confirm, the
system deletes the task record and all its executions. This action cannot be
undone.

---

## Operating on Several Tasks in Bulk

To cancel, retry or delete several tasks at once:

1. In the table, check the tasks' checkboxes. (The header checkbox,
   **Select all**, checks them all.)
2. The bulk action bar appears, showing **Selected: N**.
3. Choose an action:
   - **Cancel selected**: cancels the selected tasks.
   - **Retry selected**: retries the selected tasks that are in the
     **failed** or **cancelled** state; tasks already completed are skipped.
   - **Delete selected**: deletes the selected tasks.
4. Confirm when the system asks.

---

## Monitoring and Managing a Worker

To see the detail of a worker, click its **hostname** in the Workers
panel. The page refreshes on its own every 3 seconds.

In the header you will see:

- The **hostname** and, if it has one, its name.
- An activity dot (liveness) that shows how recent the last heartbeat is, and
  the status badge (**online**, **paused** or **offline**).
  The dot and the badge may diverge: for example, an **online** status with a
  red dot means the server believes it is active but the heartbeat is not
  arriving.
- The worker identifier (UUID), with a button to copy it.
- Three groups of metadata:
  - **Hardware**: platform, **RAM** and **Disk** (used out of total).
  - **Runtime**: the worker version.
  - **Lifecycle**: how long ago it registered and how long ago its last
    heartbeat was (the tooltip shows the exact value in UTC, see [Dates and
    times](#dates-and-times)).

The **Executions** section shows two live indicators:

- **active**: executions currently assigned, claimed or running on
  this worker.
- **recent (60s)**: executions that started or finished in the last
  60 seconds.

Below, the table lists the worker's executions with the columns
**Task**, **Status**, **Started**, **Duration**, **Exit** and the
**output →** link. (Times are shown in your local zone; see [Dates and
times](#dates-and-times).)

<a id="worker-telemetry"></a>
### Worker telemetry

The worker card and detail page show RAM, CPU and disk. Two
clarifications about how the worker measures what it reports:

- **RAM and CPU** correspond to the container (cgroup), not to the whole host.
- **Disk** is measured on the task cache volume
  (`ORCH_ORCASTER_CACHE_ROOT`, by default `/var/cache/orcaster`), not on the
  container's root disk.

If a freshly updated worker shows `ram=/…` or `disk=/…` instead of a
number, there is nothing to purge: the first full heartbeat fills in those
totals.

<a id="clearing-the-worker-cache-clear-cache"></a>
### Clearing the worker cache (Clear cache)

On the worker detail page, the cache block shows when it was last cleaned
(**Last cleaned: …**) or **Cache not cleaned yet**. Click
**Clear cache** to request cleaning of the worker's disk (task scratch
and `uv` cache). The system asks for confirmation.

The cleaning is **deferred**: the worker runs it the next time it reports
being idle. In the meantime, the block shows **Cleaning cache…**; the
freed space is then reflected in the worker's disk usage. If more than
10 minutes pass without it completing, it shows **Cache flush timed out**.

### Pausing and resuming a worker

Controlling a worker (pause, resume, clear cache, delete) is reserved for the
workspace **owner** (see [Roles](#roles)); members do not see these buttons.

Pausing a worker tells the system to stop giving it new tasks, without
shutting it down and without interrupting what it is already doing. It is
useful for draining a machine before working on it.

The control is in the worker detail header and also in the
**⋮** menu of its card in the Workers panel, and it changes depending on the
status:

- If the worker is **online**, the button is **Pause**.
- If it is **paused**, the button is **Resume**.
- If it is **offline**, neither is available: on the detail page,
  **Pause** appears disabled and explains the reason when you focus it or
  hover over it; on the card in the Workers panel, the option simply does not
  appear in the menu. In both cases the reason is the same: only a worker
  that is responding can be paused.

When you click, the system asks for confirmation and offers a **Reason (optional)** field
to note why. The reason is optional and is recorded in the [audit
log](#auditing-what-happened) together with the action and its user; it is not
shown on the worker card.

The pause is a **soft drain**: the worker stops claiming new tasks,
but the executions it already had in progress continue until they finish. The
confirmation dialog tells you how many there are at that moment. Resuming makes
it eligible again on its next query to the server, which happens within
seconds.

A pause is your decision and the system respects it: it is not lost if the
worker stops and starts again (for example, by stopping the
container with `make down client` and starting it again with
`make up client`; see [Worker states](#worker-states)). The
pause is an intention that lives on the server, not a state that the worker
process reads or controls: stopping the container does **not** revert it, only
the **Resume** button does. That is why, when a paused worker is also
shut down, its card in the Workers panel shows **paused (stale)**: it is still
paused, but it has not reported heartbeats for more than two minutes, so do not
count on it until the machine is back. The worker detail page does not repeat
that suffix next to the badge: there the connection is read from the activity
dot, as described above.

<details>
<summary>Technical Information</summary>

- `POST /admin/workers/{id}/pause` and `POST /admin/workers/{id}/resume`. The
  reason travels in the body (`{"reason": "..."}`) or in the `HX-Prompt` header;
  the dashboard sends that header percent-encoded in UTF-8 and the server
  decodes it on receipt.
- Each action writes a row in `admin_actions` (`pause_worker` /
  `resume_worker`) with the previous state in the payload, the reason in `reason` and
  the session user in `actor`.
- Pausing requires the `online` state and resuming requires `paused`; any other
  state answers `409`, which the dashboard shows as a temporary
  notification in the corner of the screen.
- After a successful action, the worker detail page reloads the full page;
  the card in the Workers panel only refreshes its panel, without reloading
  the whole page.
- The **(stale)** suffix appears when the age of the last heartbeat exceeds
  `WORKER_HEARTBEAT_TIMEOUT_SECONDS` (120 seconds by default).

</details>

<a id="renaming-the-worker"></a>
### Renaming the worker

The name visible in the dashboard (`ORCH_WORKER_NAME`) is independent of
the worker's internal identity (`worker_id`), which never changes because of
this.

1. Edit `ORCH_WORKER_NAME` in `client/.env`.
2. Apply the change with:

   ```bash
   make up client
   ```

> **Careful:** `make restart client` does **not** re-read the `.env` file
> (it only restarts the process inside the same container). For a
> change in `.env` to take effect, always use `make up client`, which
> recreates the container with the updated file. See also
> [Updating a Worker and Its Fleet](#updating-the-worker).

On a fleet host (`run-fleet.sh`) the file is `/etc/orcaster/env.N` and not
`client/.env`: edit `ORCH_WORKER_NAME` there and run
`run-fleet.sh` again, which keeps the edited name. Changing
`WORKER_NAME_PREFIX` in `setup.env` does not rename workers that already have
a name; for a worker to receive `PREFIX-N` again, leave
`ORCH_WORKER_NAME=` empty in its `env.N` before the rerun.

### Deleting a worker

In the detail page header (or from the **⋮** menu of the card in the
Workers panel):

- If the worker is **offline**, the button is **Delete worker**.
- If it is **online** or **paused** with active executions, the button is
  **Delete (force)**: when you delete it, its active executions are canceled.

In both cases the system asks for confirmation and the action cannot be undone.
The worker's execution history is preserved even if it is deleted.

### Worker states

| State | Meaning |
|-------|---------|
| **online** | The worker is connected and reporting heartbeats. |
| **paused** | The worker is held and takes no new tasks (see [Pausing and resuming a worker](#pausing-and-resuming-a-worker)). |
| **offline** | The worker stopped reporting heartbeats. |

**online** and **offline** describe whether the worker is responding. **paused**
describes something different: a decision of yours. That is why a pause is not
lost on its own. If the worker stops and starts again, it stays **paused** and
does not resume work until you resume it.

As a consequence, a paused worker that is also shut down stays
**paused** instead of moving to **offline**. To know whether it is still
connected, look at the time of its last heartbeat instead of the status badge.
And if you want to delete it, you have two paths: use **Delete (force)**, or
resume it and wait for the system to move it to **offline** for lack of
heartbeats (resuming returns it to **online**, not to **offline**).

<details>
<summary>Technical Information</summary>

- Worker detail: `GET /admin/workers/{id}`. Deletion:
  `DELETE /admin/workers/{id}` (or `?force=true`).
- Deletion is a soft delete: the row is preserved with `deleted_at`
  so as not to lose the execution history. On the detail page of a deleted
  worker the **deleted** badge appears.
- The activity dot classifies by the age of the last heartbeat: ok (<30 s),
  warn (30–120 s), danger (>120 s).

</details>

---

<a id="operating-day-to-day"></a>
## Operating a Worker Day to Day

This section describes what an operator who manages the worker process from
their own host (not from the dashboard) sees and can do.

### Following the logs

```bash
make logs client            # worker without suffix
make logs client N=2        # suffixed worker (see Multiple workers on one host)
```

There you will see, in order, startup, enrollment, each claimed task
(`Claimed task_id=<id> command=<cmd>`) and its completion
(`Completed task_id=<id> status=<status> exit_code=<code>`), plus any
cancellation or network error.

### Polling cadence (poll)

The worker queries the server at a certain interval (**poll**), asking for
work and reporting its status. That interval is suggested by the server itself
in each response and the worker respects it within a range of **1 to 30
seconds**. Do not expect a latency below 1 second between a task being created
and a worker claiming it, nor an instant cancellation: both are subject to the
next query.

The poll also works as the worker's heartbeat: each
successful query renews its presence with the server and the lease of the
execution in progress, if there is one. It is not a separate call (see
[Worker endpoints](#worker-endpoints) in the JSON API Reference).

### One slot per worker

Each worker runs **only one task at a time**. While that task runs,
the worker reports itself **busy** on each poll; if the server assigns it
another task at that moment, the worker ignores it
(`received task_id=<id> while busy with task_id=<id>; ignoring (server
will reclaim)`) and the server reassigns it to another available worker. Do not
expect concurrency within a single worker; for parallelism, add
more workers.

### Where a task writes

Inside the task, two environment variables tell it where to persist each
kind of result:

| Variable | What to use it for |
|----------|--------------------|
| `ORCASTER_ARTIFACTS_DIR` | Files you want to keep and download later (requires the task to have **Collect output artifacts** enabled; see [Collecting artifacts](#collecting-artifacts)). |
| `ORCASTER_CACHE_DIR` | Downloads reusable across executions of the same job (datasets, large packages), so they are not downloaded again. |

The worker also sets `TASKORCH_ARTIFACTS_DIR` and `TASKORCH_CACHE_DIR` to the
same two directories. They are **deprecated** aliases, kept only for tasks
written before the product was renamed; use the `ORCASTER_*` names in new
tasks.

The worker always sets these four variables itself. A value from
**Environment variables**, from the worker's environment or from a task secret
never replaces them, and none of the four can be stored as a secret (see
[Setting a secret](#setting-a-secret-owner-only)). If the worker cannot create
the cache directory, the task runs without any of the four.

The working directory (`cwd`) where each command starts is **ephemeral and
is always deleted** when the task finishes, whether it succeeds or not. Do not
write anything there that you need to keep.

For the RAM, CPU and disk telemetry that the worker reports, see
[Worker telemetry](#worker-telemetry) in Monitoring and Managing a
Worker.

### Opening a shell inside the worker

To inspect volumes and paths directly:

```bash
make shell client
```

### What a task cannot do

By design, a task cannot: modify the host, open ports below
1024, map a GPU (unless the worker was started with
`--device`) or listen on ports unless the worker was published with
`-p`. Do not design tasks that depend on these capabilities.

<details>
<summary>Technical Information</summary>

Each poll sends
`{status, lease_token, current_task_id, current_execution_progress,
lease_seconds, name, ram_mb, ram_used_mb, disk_mb, disk_used_mb, cpu_pct,
cache_flush_completed}` to `POST /v1/workers/{id}/poll`. The requested lease
TTL is `ORCH_LEASE_SECONDS` (default **120** seconds). The status the
worker reports is `"idle"` or `"busy"` (also `busy` while a cache
flush is running). The actual interval of the next query is
decided by the server via `next_poll_after_seconds`, clamped locally to
`[ORCH_POLL_MIN_SECONDS, ORCH_POLL_MAX_SECONDS]` (`1.0`–`30.0` by
default).

</details>

---

<a id="pausing-stopping-and-resuming-a-worker"></a>
## Pausing, Stopping and Resuming a Worker

Pausing (draining) a worker, stopping its process on the host and resuming it
are three different actions, with different owners: the pause is decided by an
operator from the dashboard; stopping and resuming the process are decided by
whoever manages the host.

<a id="pause-and-resume"></a>
### Pause and resume

Pausing and resuming a worker are **dashboard** actions, not host actions: they
are done with the **Pause** and **Resume** buttons. See the complete
procedure, the **Reason (optional)** field and the **paused (stale)** state in
[Pausing and resuming a worker](#pausing-and-resuming-a-worker), inside
[Monitoring and Managing a Worker](#monitoring-and-managing-a-worker).

### Stopping the worker without losing its identity

To stop the worker without shutting down the host or losing its identity:

```bash
make down client
```

And to start it again, keeping the same identity:

```bash
make up client
```

Only `make purge client` deletes the identity (see
[Updating a Worker and Its Fleet](#updating-the-worker) and
[The worker gets 401 / unauthorized](#401-unauthorized)).

### Cooperative shutdown

When it receives `SIGTERM` or `SIGINT` (for example, via `podman stop` or
`make down client`), the worker does not shut down abruptly: it tries to close
the task in progress cleanly, if there is one.

1. `Signal received, shutting down...`
2. The worker leaves the polling loop.
3. If a task is running, it signals it to abandon and waits up to **10
   seconds** of grace for it to finish; if that is exceeded, it cancels it by
   force.
4. It reports the task as **cancelled** to the server.
5. It sends a farewell notice (`goodbye`) to the server, with a limit of 2
   seconds: if it does not arrive in time, the server detects the
   absence anyway through the lack of heartbeats.

> If the worker was **paused** at the time of shutdown, it stays
> **paused** after restarting: the farewell notice only leaves it
> **offline** if it was **online**. See [Pause and resume](#pause-and-resume)
> above.

### systemd --user vs make

If you enabled automatic startup (`06-autostart.sh`), the worker is
managed by a `systemd --user` unit
(`container-orcaster-worker[-N].service`) that restarts it if it goes down. To
stop it without systemd bringing it back up, do not use `podman stop`
directly: stop it through systemd.

```bash
systemctl --user stop container-orcaster-worker.service
# suffixed worker:
systemctl --user stop container-orcaster-worker-2.service
```

If instead you run `make up client` while the unit is still active,
it may collide with `container name is already in use`; that is why the
`up client` target itself stops the active unit before recreating the
container, and regenerates it when it finishes.

To do this, `make up client` needs to talk to the `systemd --user` manager
of `orcaster`. Run it as that user, not as `root`:

```bash
sudo -iu orcaster
cd /opt/orcaster    # the host's installation directory
make up client
```

(On a host deployed by tarball, `make up client` takes the same
arguments as in the step that starts the worker in
[Provisioning a Debian 13 VPS](#provisioning-a-debian-13-vps).) The `orcaster`
user must have **linger** enabled: it is what
keeps its `systemd --user` manager alive without an open session, and the host
provisioning already enables it (`sudo loginctl enable-linger orcaster`). Without
linger, the command stops with an error that names `loginctl enable-linger`.

If the manager does not respond, `make up client` stops with
`ERROR: could not reach the systemd --user manager…` and exits with an error
**without recreating anything** (neither the container nor, with egress
isolation, its sidecar): the worker keeps running as it was. What to do in that
case: [`make up client` fails with `could not reach the systemd --user
manager`](#could-not-reach-the-systemd-user-manager).

<details>
<summary>Technical Information</summary>

`/goodbye` leaves the worker `offline` **only if it was `online`**; if it was
`paused`, it remains `paused`. The worker never reads
the `storage_paused` field of the poll response: pausing is a state
that lives exclusively on the server.

</details>

---

<a id="updating-the-worker"></a>
## Updating a Worker and Its Fleet

> This section covers the local development checkout (with `git pull`). A
> production host provisioned with `setup-scripts/debian13-worker/` has no
> `.git`: to update its code use
> [Deploying a new version to the fleet](#deploying-a-new-version-to-the-fleet),
> further below.

### Applying a code or configuration change

After a `git pull` that touched dependencies or the `Dockerfile`:

```bash
make down client
make build client
make up client
```

The worker's identity survives because it lives in the data volume
(`orcaster-data`), not in the container.

The worker's source code (`client/src/orcaster_worker`) is mounted as a
read-only volume inside the container. To apply code changes
that did not touch dependencies, without a rebuild:

```bash
make restart client
```

> Remember that `make restart client` does **not** re-read `client/.env` (see
> [Renaming the worker](#renaming-the-worker)). Use it
> only for code changes; for configuration changes, use
> `make up client`.

### Automatic updates (`self_update`)

The worker can receive a reserved self-update command, but
**it does not run it on its own**: it only confirms (ACK) that it received it.
The actual update of the code and the image is run by you, on the host, with
the steps above.

### Updating a fleet

Running `run-fleet.sh` or `cloud-init-worker.sh` again reconciles
**configuration** (`setup.env` variables, number of workers, memory
cap, disk gate), but it does **not update the code**: neither of the two
scripts clones or downloads it. To bring new code to the fleet, see
[Deploying a new version to the fleet](#deploying-a-new-version-to-the-fleet)
below.

```bash
cd /root/orcaster/setup-scripts/debian13-worker
sudo bash run-fleet.sh
```

If your fleet runs from a [golden snapshot](#golden-snapshot), the new code
does not arrive when you re-run `cloud-init-worker.sh` (that script requires
the code to be already installed; it does not bring it). To update it: push the
release to each live host with `ops/release/push-release.sh` (same path as
any hand-provisioned host), or re-bake the reference snapshot
with the new code and create the next fleet from that snapshot.

<a id="deploying-a-new-version-to-the-fleet"></a>
### Deploying a new version to the fleet

Each production host updates its code with
[`ops/release/push-release.sh`](../ops/release/README.md). From the
operator's machine, with the repository checked out:

```bash
ops/release/push-release.sh --ref vX.Y.Z
```

`push-release.sh` builds the package with `make-release.sh` (a `tar.gz` of
`git archive`, without `.git`, with a `RELEASE` file that declares version and
commit), copies it by `scp` to each host listed in `deploy.local.env` (or to
those you pass with `--hosts`) together with its `sha256` and the installer, and runs
`install-release.sh` on the host via `sudo bash` over `ssh`.

> **The code bind mount is live while the container runs.** The
> container reads the code from the host through a live-mounted volume, so
> replacing the code directory while the container is still up leaves its
> mount pointing to a directory that no longer exists under that name. That is
> why `install-release.sh` always swaps the code **first** and only
> afterwards recreates the worker containers (never `podman restart`, which
> reuses the old mount). The script itself handles this: no separate manual
> step is needed, but if you ever recreate a worker by hand after replacing
> the code by hand, the order is the same: swap first, then
> `make up client`.

`install-release.sh` never deletes the installed code before having the new
release complete and with its `sha256` verified; if the swap fails
midway, it tries to restore the previous code and leaves a message that
says exactly where each thing ended up.

Before the swap, `install-release.sh` validates the host state (the
`setup.env`, each `/etc/orcaster/env.N`, the server's public key, the
`orcaster` user, the `systemd --user` manager of `orcaster` if the host has
autostart units, `EGRESS_ISOLATION`, `WORKER_MEMORY`, `WORKER_RO_MOUNTS`) and
any failure aborts with "The installed code was not touched." If the host has
autostart units (`container-orcaster-worker*.service` or
`container-orcaster-proxy*.service` in `~/.config/systemd/user/` of `orcaster`),
confirm before deploying that `orcaster` has linger:
`loginctl show-user orcaster -p Linger` must print `Linger=yes`; if not, the
deploy stops before installing anything (see [`make up client` fails with `could
not reach the systemd --user manager`](#could-not-reach-the-systemd-user-manager)). One of those
validations is the protected variable without isolation: if any
`env.N`, or the `EXTRA_ENV` of `setup.env`, has a value for a variable in the
list of variables that require isolation and does not have `ORCH_ENFORCE_ISOLATION=true`,
the deploy aborts naming the file and the variable (never the value), because the
new worker would refuse to start and would end up in a crash loop. The list for each
`env.N` is its last `ORCH_ISOLATION_REQUIRED_ENV` line; the one for `EXTRA_ENV` is
`ISOLATION_REQUIRED_ENV` of `setup.env`, and `EXTRA_ENV` is evaluated followed by
`setup.env`'s own `ORCH_ENFORCE_ISOLATION`, which is what `run-fleet.sh`
will stamp. An empty or missing list protects nothing; a malformed one aborts
the deploy before the swap. Apply the recipe in
[Enabling strict mode](#enabling-strict-mode-orch_enforce_isolation) and retry the deploy.

<a id="deployment-order-capabilities"></a>
### Rolling upgrade

Upgrade the server first, then the workers one at a time:

1. **Server first.** Deploy the server and verify that the workers
   keep polling and taking tasks. Restarting the server does not cut off tasks in progress: the worker keeps
   running and retries the poll, and during the first 150 s after
   startup (`STARTUP_GRACE_SECONDS`) the server does not expire leases. What
   can be lost is the closing of a task that finishes right while the server is
   down: the worker retries the report for about 8 s and then gives up, and the
   task returns to the queue when its lease expires.
2. **Then each worker, one at a time.** For each worker:
   1. Pause it from the dashboard (**Pause**) and wait until it has **no task
      in progress**. `install-release.sh` recreates the container, and on
      receiving `SIGTERM` the worker gives the task in progress only 10 seconds
      of grace before canceling it (see [Cooperative shutdown](#cooperative-shutdown)).
      Before pausing it, check the **Secrets** page (or the server log)
      for a rejected delivery for that worker: while it is paused the
      server does not offer it tasks, so its rejection mark is not renewed
      and a pre-assigned task that has been waiting longer than its
      `claim_timeout_seconds` expires during the pause. Fix or delete the
      secret, or cancel and retry the task, before pausing.
   2. If you add or change `ORCH_CAPABILITIES`, edit its `/etc/orcaster/env.N` (and
      `EXTRA_ENV` of `setup.env`, see
      [Declaring the worker's capabilities](#declaring-the-workers-capabilities)).
   3. Deploy the release on that host with
      `push-release.sh --ref <tag> --hosts <host>`. `install-release.sh`
      recreates **all** the workers on the host (`WORKER_COUNT`), so if the
      host runs more than one, pause them and wait until all are idle before
      this step.
   4. Confirm the worker is running: `podman ps` must show it
      `Up` for more than a few seconds, and not restarting. If
      `ORCH_CAPABILITIES` is misspelled, the worker does not start and, with
      `--restart unless-stopped`, goes into a crash loop even though
      `install-release.sh` finished fine. The log says which entry
      failed (see
      [The worker enters a crash loop at startup](#crash-loop-and-its-three-causes)).
   5. Resume it (**Resume**) and move on to the next.

The worker stays paused after being recreated: the pause lives on the server,
not in the container. Never recreate two workers at once if that leaves the
fleet with nobody to take from the queue.

<a id="moving-the-extra_env-tokens-to-the-workspace-secrets"></a>
### Using task secrets instead of `EXTRA_ENV`

The tokens that tasks need can live on the server as
[task secrets](#task-secrets) instead of in the `EXTRA_ENV`
of each host. A task *uses stored secrets* when any of the names it
declares in **Environment variables** has a secret stored in the
workspace. The server delivers those tasks only to workers that report task
secret support (a card without the **No task secrets** mark in the Workers
panel); until one is available, the task stays queued. To move a token:

1. **Configure the server's keys** (see [Configuring the task secret
   keys](#configuring-the-task-secret-keys)).
2. **Set the secrets** in **Secrets** or with `orcaster secret set`, with the
   same names the tasks declare.
3. **Then** remove those values from `EXTRA_ENV` in `setup.env` and
   recreate the containers (a `restart` does not re-read the environment file).

A worker built from an unreleased commit between two releases reports the
version of the previous release, and the server treats it as not supporting
task secrets until the release is cut. To test secrets with such a worker
before the release, start it with `ORCH_VERSION` set to a version that supports
task secrets. If a task with stored secrets stays queued, see [A task with
stored secrets stays queued](#a-task-with-stored-secrets-stays-queued).

To check where a task got each variable from, look in the worker's log for the
per-task line:

```text
task_id=<id> task_env sources: server=[API_TOKEN] worker_env=[HTTPS_PROXY,HTTP_PROXY,NO_PROXY] missing=[] denied=[] ignored_server_secrets=[] refused_server=[]
```

`server` are the values that came from the server, `worker_env` those that
came from `EXTRA_ENV` and `missing` the declared names that exist
in neither. If any of `server` is a secret scoped to that
worker, the line ends with `server_worker_scoped=[GITHUB_TOKENS]`. The line only names variables, never values. A
declared entry that is not a valid variable name (for example
`NAME=value`) or
that starts with the prefix of a known token (for example `ghp_...`) is not
written: the line only counts it, as `invalid_missing=1` next to `missing` or
`invalid_denied=1` next to `denied`, and those counters do not appear when they
are zero. `missing` lists at most 50 names: the rest are counted as
`missing_not_listed=N`, and a name of more than 128 characters is not written and is
counted as `missing_too_long=N`; they do not appear when they are zero either. The exception is a name that the worker or the server does have: it appears in
`worker_env` or `server` like any other, because in that case it is delivered to
the task just as before.

<a id="reducing-the-size-of-a-fleet"></a>
### Reducing the size of a fleet

Lowering `WORKER_COUNT` in `setup.env` and running `run-fleet.sh` again prunes
the surplus workers (`systemd` unit, container and `/etc/orcaster/env.N`) together
with their egress sidecar: `run-fleet.sh` disables
`container-orcaster-proxy-N.service` before the `make down client N=n`, which removes
both containers. The pruning **keeps the volumes** of each worker
(`orcaster-data-N`, `orcaster-cache-N` and `orcaster-egress-vol-N`), because
`make down client` does not delete any volume. This lets you grow the
fleet again later reusing the same worker identity, instead of
each new worker registering as if it were a different one.

### Retiring the sidecar of a worker without isolation

Setting `EGRESS_ISOLATION=0` when recreating a worker does **not** dismantle the
sidecar that already existed. Remove it separately:

```bash
systemctl --user disable --now container-orcaster-proxy.service
podman rm -f orcaster-proxy
```

### Rotating an expired or exhausted enrollment token across the fleet

Generate a new token in the dashboard, update
`ORCH_ENROLLMENT_TOKEN` in `setup.env` and run `run-fleet.sh` again: the
script re-stamps that variable in each `/etc/orcaster/env.N` without touching the rest
of the configuration already stored.

<details>
<summary>Technical Information</summary>

The `AUTOSTART_SYNC=1` hook (default of `make up client`) stops the
`systemd --user` unit before recreating the worker and regenerates it when it
finishes, so that autostart keeps pointing to the right container after a later
`make up client`. `run-fleet.sh` disables it (`AUTOSTART_SYNC=0`) because it calls
`06-autostart.sh` separately, once per worker. `install-release.sh` also
calls it separately, but passes `AUTOSTART_RESYNC=0`, which turns off only the
final re-sync and keeps the previous stop of the unit. The unit is stopped
unless it is at rest (`inactive`, `failed` or `unknown`); in `active`, `activating`
(which includes the automatic restart of a worker in a crash loop), `deactivating` and
`reloading` it is stopped.

`make up client` must be run as the `orcaster` user, not as `root`. If
the shell does not carry `XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS` (the case with
`sudo -iu orcaster`, which does not open a logind session;
[pam_systemd(8)](https://www.freedesktop.org/software/systemd/man/latest/pam_systemd.html)),
the target derives them: `XDG_RUNTIME_DIR=/run/user/<uid>` if that variable is not
defined and that directory exists, and
`DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus` if that one is not
either. A variable that is already defined is left alone. Then it probes the manager with
`systemctl --user show-environment`, as a prerequisite that runs before
recreating the egress sidecar, for the worker's unit and, with egress
isolation, also for the sidecar's. The manager is only queried if the unit
file exists (`container-<name>.service`), `AUTOSTART_SYNC` is not `0`
and `systemctl` is present; on a development or CI machine without autostart nothing
changes.

These exit with a non-zero code, and `make up client` with them: the manager
not responding (before recreating anything, so neither the container nor the
sidecar is touched), a failing `systemctl --user stop` (if the sidecar had
already been recreated in the same command, the error says so) and a failed
re-sync of `06-autostart.sh` (in that case the container was recreated; see [The
`systemd --user` unit is out of sync](#systemd-unit-out-of-sync)). A
failure to connect to the bus is never interpreted as "unit inactive".

The `AUTOSTART_SYNC_FATAL` variable (default `1`) governs the last case: with
`AUTOSTART_SYNC_FATAL=0` a failed re-sync only prints the warning and
`make up client` exits with 0. `install-release.sh` passes `AUTOSTART_RESYNC=0`,
which turns off only the Makefile's final re-sync, because on a host
installed by tarball the Makefile tree is `INSTALL_DIR` and the origin guard
would reject the hook; and it also passes `AUTOSTART_SYNC_FATAL=0`, so that the
Makefile of an earlier release (a rollback), which does not know
`AUTOSTART_RESYNC`, only warns. After each successful `make up client`, if the
worker's unit file exists, it runs `06-autostart.sh` as root from the
root copy (`/root/orcaster` by default), with the worker's user
and its home. If it fails, it warns and continues with the next worker (the loop
must not be cut off halfway); at the end it exits with an error if any re-sync
failed or if any unit that was running did not come back. The manager's
prior check does not depend on that variable.

The `orcaster` manager exists without an open session only if the user has
linger ([loginctl(1)](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)):
`00-preflight.sh` enables it with `loginctl enable-linger orcaster`. Without linger
`/run/user/<uid>` does not exist and the command stops with an error that names
`loginctl enable-linger`.

`make up-proxy` applies the same manager check before recreating the
sidecar, but it does not regenerate the sidecar's unit by itself; for that use
`make up client EGRESS_ISOLATION=1` or run `06-autostart.sh` afterwards.

</details>

---

<a id="troubleshooting"></a>
## Diagnosing and Resolving Worker Problems

<a id="crash-loop-and-its-three-causes"></a>
### The worker crash-loops on startup

With `--restart unless-stopped`, a worker that fails on startup restarts over
and over. Check `make logs client` and compare it against these three causes
of early (fail-fast) failure:

| Cause | What the log shows | What to do |
|-------|--------------------|------------|
| Public key cannot be loaded | `Failed to load Ed25519 pubkey from <path>: <error>` or `ED25519_PUBLIC_KEY_PATH not configured (settings value is None).` | Check that `client/server-ed25519.pub` exists, is mounted correctly and is the server's correct public key. |
| Enrollment token missing | `No persisted credentials and the enrollment token needed to register the worker is missing.` | Set `ORCH_ENROLLMENT_TOKEN` in `client/.env` to a valid token (see [Connecting a Worker](#connecting-a-worker)) and recreate with `make up client`. |
| Token rejected by the server | `The server rejected the registration with HTTP 401/422: the enrollment token is invalid, expired or has no uses left.` | Generate a new token in the dashboard. |

<a id="401-unauthorized"></a>
### The worker gets 401 / unauthorized

This usually means the stored `api_key` is no longer valid (for example,
because the worker was deleted from the server and was left with an orphaned
`state.json`). Purge the local identity and enroll again with a valid token:

```bash
make purge client
make up client   # with a valid ORCH_ENROLLMENT_TOKEN in client/.env
```

### The worker connects but runs no task

If the log shows
`signature verification failed; rejecting silently. Server will reclaim
lease on expiry.`, the worker is receiving tasks but silently rejecting them
(it does not call `complete`) because the signature does not verify against
the loaded public key. This almost always means that
`client/server-ed25519.pub` is the wrong key, from another server or
outdated after a rotation. Replace it with the server's current public key
and recreate the worker.

### Proxy `DENY` (403 when downloading something)

In `podman logs orcaster-proxy`, a line `DENY CONNECT <host>:<port>` means
the task tried to reach a domain that is not allowed. Add the domain to the
task's **Egress allowlist**, or to the worker's base allowlist
(`ORCASTER_PROXY_BASE_ALLOW`) if every task needs it. See
[Isolating a Worker's Network (Egress)](#isolating-a-workers-network-egress).

### `egress allowlist refused` in the proxy

In `podman logs orcaster-proxy`, a line `WARNING egress allowlist refused:
<reason>; per-task entries ignored, base allowlist only` means the sidecar
discarded the task's allowlist because it did not carry a valid signature.
While this lasts, the task only reaches the base allowlist and will see `DENY`
for its own domains. The cause is usually an `allow.txt` modified from inside
the worker container, or a worker and a sidecar created with different keys;
in the second case, `make up client` recreates them together with the same
key. See the details in
[Isolating a Worker's Network (Egress)](#isolating-a-workers-network-egress).

### `venv setup failed`

| Message | Typical cause | What to do |
|---------|---------------|------------|
| `uv venv failed (exit=N): …` | Failure creating the virtual environment (for example, full disk). | Check the space in `/var/cache/orcaster` (see [Freeing disk when the worker fills up](#freeing-disk-when-the-worker-fills-up)). |
| `uv pip install failed (exit=N): …` | Almost always a proxy `DENY` during package installation, or a DNS resolution error within seconds (`dns error`, `Name does not resolve`) if the task declares a strict `task_env` without the proxy variables. | Check `podman logs orcaster-proxy`. If there is a `DENY`: the **task's** allowlist already applies in this phase, so add the missing domain to the task's **Egress allowlist** (or the package index to the worker's base allowlist, if every task needs it). If there is no `DENY` and the error is a DNS one (`dns error`, `Name does not resolve`): add `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` to the task's `task_env` (see [The known limit](#the-known-limit)). |

### The task ended `failed` with `exit_code=2`

There are two possible causes: the task payload did not include `requirements`
(a list) or `commands` (a non-empty list), with the message `rejected: task
payload must include 'requirements' (list) and 'commands' (non-empty list);
received keys=[…]`, or the internal process that runs the task failed
unexpectedly (`executor_task crashed for task_id=<id>: <repr>`). In the first
case, check how the task was created; in the second, check the full log around
that `task_id`.

A third cause, with the message `secrets malformed: …`, means the `secrets`
field of the server's response arrived with an invalid shape. The worker does
not run the task so as not to run it without its secrets. Check that the
worker and the server are on compatible versions, and check the server log
around that time.

<a id="task-failed-because-a-secret-cannot-be-decrypted"></a>
<a id="task-queued-because-a-secret-cannot-be-decrypted"></a>
### A task stays queued because the server cannot decrypt a secret

If a task that declares in **Environment variables** a name with a stored
secret stays in **queued** even though the available workers support stored
task secrets, the server probably cannot deliver that secret. There are two
causes:

- The server has no encryption keys configured (`TASK_SECRETS_KEYS_PATH`),
  for example because it was reinstalled without the file.
- The server has keys, but none of them decrypts the stored value (for
  example, the key that encrypted it was removed from the file).

In both cases the server does not deliver the task and does not mark it
**failed** either: it stays queued, without running and without consuming an
attempt. When the cause is fixed, free workers receive it on their next polls,
without **Retry**, also for `all-available` tasks (see their limits below).
While this lasts, a worker's polling keeps its normal cadence and tasks that
do not use stored secrets keep running. Tasks that use other, healthy stored
secrets also keep running: on each poll the server sets aside only the tasks
that declare the names that cannot be decrypted. There is one limit: if there
are more than three distinct undecryptable names at the front of the queue at
once, tasks that use healthy secrets wait for that worker until the blocked
ones leave the front of the queue or you fix or delete the secrets; tasks
without stored secrets keep running all the same. With no keys loaded, no
stored secret can be delivered and the server sets aside every task that uses
them.

The task itself only shows **queued**, with no message of its own: the reason
is visible on the **Secrets** page and in the server log (below).

To confirm it:

1. On **Secrets**, a notice says so. With no keys configured: *Task secrets
   are disabled on this server*, followed by the fact that tasks declaring a
   stored name stay queued, how many there are (*N queued task(s) declare a
   stored secret name.*) and that an owner can delete a secret so those tasks
   take the value from the worker's environment. With keys that do not
   decrypt: *The server could not decrypt stored secret(s) <NAMES> in the
   last 15 minutes*, with the name of each secret. This second notice lives
   in the memory of the server process: a restart clears it and, with several
   processes, each one shows only what it rejected itself.
2. In the server log, look for `task_secrets.keys_missing` (no keys) or
   `task_secrets.undecryptable` (cannot be decrypted). Each line carries the
   worker id, the task id, the secret names (never the values) and
   `queued_tasks`, and ends with the action to take. `queued_tasks` counts
   all the queued tasks of the workspace that use stored secrets and that
   the worker could receive, not only those that declare the quoted names.
   It appears at most once every 10 minutes per worker and per type in each
   server process, and only when a poll discovers the refusal: while the
   server remembers a recent refusal for that worker (30 s) it does not log
   it again. If the keys are missing, the server startup already left an
   `ERROR` `lifespan: N task secret(s) are stored but TASK_SECRETS_KEYS_PATH
   is not configured`.

To fix it, choose according to the cause:

- **No keys:** configure the file and restart the server (see [Configuring the
  keys for task secrets](#configuring-the-task-secret-keys)). The server
  reads the path only at startup.
- **Cannot be decrypted:** restore in the file the key that encrypted the
  secret, or set the secret again in **Secrets**. There is no need to restart
  the server. If the key file is a regular file, the server rereads it by
  itself, once, when it detects that it changed, on the poll that is about to
  refuse a delivery because it cannot be decrypted or on the polls of a worker
  with that refusal freshly remembered; with a systemd credential the service
  only sees the change when it restarts.
- **No possible or necessary fix:** delete the secret in **Secrets** (the
  owner can do it even when there are no keys): tasks that declare that name
  take the value from the worker's environment, if it has one.

After fixing or deleting a secret, the task can take up to 30 s longer than
the normal polling interval to be delivered, whether you set the secret again
or delete it: the server remembers for 30 s, per process, what it could not
deliver to each worker so as not to repeat the attempt on every poll, and
only when that period expires does it try again. With several server
processes, each one has its own memory.

An `all-available` task that was already assigned to a worker when the
delivery was refused stays assigned to that worker, and while the refusal
lasts the server does not let that assignment expire through
`claim_timeout_seconds`: the worker keeps asking for the task and it is the
server that cannot deliver it. When the cause is fixed, that worker's next
free poll receives it, without **Retry**. There are limits:

- While the refusal lasts, the task stays in **dispatching** on the board,
  and it stays so until the cause is fixed or you cancel it with **Cancel
  task**.
- A worker busy with another task does not pick assigned tasks or tasks from
  the open distribution: nothing is refused to it and its assignment is not
  protected. It expires after the task's `claim_timeout_seconds` (3600 s by
  default), like that of any busy worker.
- An assignment that has already expired, or that expires through the
  previous case, requires cancelling the
  task with **Cancel task** and retrying it with **Retry**.

Because of the refusal the server sets no deadline on a queued task; a task
that has its own expiry (`expires_at`) stops being picked after that instant,
as always.

<details>
<summary>Technical Information</summary>

- The delivery attempt runs in a `SAVEPOINT` of the poll transaction and, on
  refusal, `ROLLBACK TO SAVEPOINT` is executed
  ([SAVEPOINT](https://www.postgresql.org/docs/current/sql-savepoint.html),
  [ROLLBACK TO SAVEPOINT](https://www.postgresql.org/docs/current/sql-rollback-to.html)):
  no run row is left and no attempt is consumed. The worker's heartbeat, the
  lease renewal and the cancellation signals are saved all the same. Then the
  same poll picks again without the tasks that declare the refused names (up
  to 3 times per poll, accumulating names) and, with no keys or once those
  picks are exhausted, without any task that uses stored secrets. If the key
  file is missing, that last pick chooses, poll after poll, among the queued
  tasks of the workspace until it finds one that does not use stored secrets;
  with a long queue made up only of blocked tasks this is a bounded index scan
  per poll.
- Short refusal memory (`REFUSAL_MEMORY_SECONDS`, 30 s, per server process):
  after refusing a delivery, the process remembers what it could not deliver
  to that worker (all the tasks with stored secrets if the keys are missing,
  or those that declare the undecryptable names) and, while it lasts, the poll
  goes straight to the pick that excludes them, without repeating the claim,
  the refusal and the `ROLLBACK TO SAVEPOINT`. When it expires, the next poll
  tries the normal pick. A valid change of the key file discards it at once (a
  `stat` per poll with per-name memory), and the "keys missing" one is only
  valid while no keys are loaded. That is why the log `ERROR` and the
  **Secrets** notice appear at most once per memory period per worker and per
  process.
- When a delivery is refused, the poll marks the worker
  (`workers.task_secrets_refused_at`, migration `0060`). The mark is rewritten
  at most once every 60 s per worker (half the window), also while the memory
  applies, so as not to rewrite its row on every poll; the server does not
  compare its clock with the database's. For 120 s the other workers do not
  yield to it the tasks that use stored secrets: without that mark the refused
  worker would remain the "least recently assigned" and fair distribution
  would leave it tasks it cannot receive. The mark is in the database because
  the server can run with several processes, and it expires by itself when the
  refusal ceases.
- The same mark criterion (less than 120 s), together with the task declaring
  some name in `task_env`, makes housekeeping not expire the assignment of
  that task to that worker through `claim_timeout_seconds`. If it expired, the
  closing rule for `all-available` tasks counts the expired run as terminal
  and another worker's `/complete` would close the task as **failed**, and
  besides the worker would not receive it when the keys return. The condition
  does not check whether stored secrets remain: deleting the last one, which
  is the way to unblock the task, does not remove the protection while the
  mark is still current. When the worker stops polling or the refusal ceases,
  the mark ages and the expiry runs as usual.
- `next_poll_after_seconds` remains 30 (free worker) or 5 (busy); only a
  delivered task lowers it to 1.
- The full log line is `task_secrets.keys_missing worker_id=… task_id=…
  names=… queued_tasks=N: the task was left queued and the delivery was
  refused; …` (or `task_secrets.undecryptable …`). `queued_tasks` is an upper
  bound, as in `task_secrets.worker_unsupported`: it counts all the queued
  tasks of the workspace that use stored secrets and that the worker could
  receive (open claim and tasks directed to it).
- The server detects that the file changed by its inode, `mtime`, `ctime` or
  size. If it changed but is not valid, it keeps the keys already loaded and
  logs an `ERROR` `task_secrets.keys_reload_failed`; if it is valid, it logs
  `task_secrets.keys_reloaded` with the key ids. The check happens when a poll
  is about to refuse a delivery because it cannot be decrypted and on the
  polls of a worker with that refusal remembered; see
  [Rotate the key](#rotate-the-key) for its scope.

</details>

<a id="a-task-with-stored-secrets-stays-queued"></a>
### A task with stored secrets stays queued

If a task that declares in **Environment variables** a name with a secret
stored in the workspace stays in **queued** while other tasks do run, the most
likely cause is that the available workers are older versions that do not
support stored task secrets (or have not reported a version): the server does
not deliver to them the tasks that use stored secrets. To confirm it:

1. On the Workers board, each worker's card shows its version. The **No task
   secrets** flag next to it means that worker does not receive those tasks.
2. In the server log, look for `task_secrets.worker_unsupported`: it carries
   the worker id, its version, the minimum version and how many queued
   tasks, already ready to run, use stored secrets. It is an upper bound: it
   may count tasks that the worker would not take either for other reasons,
   such as the pool or the capabilities. At most one line appears every 10
   minutes per worker and per server process, and only when the worker polls
   free, not paused and with no lack of space.

See [Using task secrets instead of
`EXTRA_ENV`](#moving-the-extra_env-tokens-to-the-workspace-secrets).
A task directed to a specific worker waits for that worker to be updated or,
if it has an expiry (`expires_at`), until it expires; without an expiry it
waits indefinitely. If you do not want to use the stored secret for that name,
delete it in **Secrets**: tasks that do not declare another name with a stored
secret stop using stored secrets, and the older workers take them again with
their `EXTRA_ENV`. If the workers already support stored task secrets and the
task stays queued, see [A task stays queued because the server cannot decrypt
a secret](#a-task-stays-queued-because-the-server-cannot-decrypt-a-secret).

### The worker aborted a task before running it

The message `could not clear the cache of a previous job: <dirs>`
means the worker could not delete a cache directory left by a
previous job before starting the new one, and preferred to abort instead of
starting with potentially mixed data. Check permissions or space in
`/var/cache/orcaster`.

### Incomplete output

If a run's record looks cut off, look in the worker log for messages such as
`output drain deadline (X.Xs) reached task_id=…; dropping N pending events` or
`output backlog over hard RAM cap and no spill available; dropped N events so
far`. They indicate that the worker could not deliver the whole volume of
output to the server in time (for example, because of a sustained network
problem) and dropped events so as not to accumulate memory without limit. The
output that did reach the server is reliable; what is missing are the dropped
events.

<a id="a-worker-shows-as-offline-in-the-dashboard"></a>
### A worker shows as `offline` in the dashboard

1. Check that the container is still running:

   ```bash
   podman ps -a --filter name=orcaster-worker
   ```

2. Check the process logs:

   ```bash
   make logs client
   ```

3. If you manage the worker with autostart, check the unit's state:

   ```bash
   systemctl --user status container-orcaster-worker.service
   ```

### `make up client` fails with `ERROR: 'podman' not found in PATH.`

The project is Podman-only, with no Docker alternative. Install Podman on the
host (see [What the worker host needs](#what-the-worker-host-needs)).

<a id="could-not-reach-the-systemd-user-manager"></a>
### `make up client` fails with `could not reach the systemd --user manager`

The full message starts with `ERROR: could not reach the systemd --user manager
and the unit container-orcaster-worker.service exists`, and continues with the
cause and the remedy. It means `make up client` could not talk to the
`orcaster` user's `systemd --user` manager and stopped **before recreating
anything** (neither the container nor, with egress isolation, its sidecar): the
worker keeps running as it was, not recreated. There is nothing to undo.

A release deploy (`push-release.sh`, which runs `install-release.sh` on the
host) stops with the same initial message, `ERROR: could not reach the systemd
--user manager…`, **before installing anything**, when the host has autostart
units and the `orcaster` manager does not respond: the installed code and the
workers stay as they were. The same remedies as in steps 2 and 3 below apply;
then repeat the deploy.

The message says which of three causes it is, and each one corresponds to one
of steps 2 to 4. Commands with `sudo` are run from an administrator account,
not from `orcaster`, which normally has no `sudo`.

1. Confirm that you run the command as `orcaster` and not as `root`:

   ```bash
   sudo -iu orcaster
   ```

2. If the message says `/run/user/<uid>` does not exist, the user has no
   *linger*. From an account with `sudo`, enable it and repeat `make up client`:

   ```bash
   sudo loginctl enable-linger orcaster
   ```

3. If the message says the variables were derived from `/run/user/<uid>` but
   the manager does not respond, the `orcaster` manager is down or starting.
   From an account with `sudo`, check it and start it (`<uid>` is the result of
   `id -u orcaster`), and repeat `make up client`:

   ```bash
   sudo systemctl status user@<uid>.service
   sudo systemctl start user@<uid>.service
   ```

4. If the message says the variables came from the caller's environment and
   do not work, they point somewhere else (or the manager is down). Repeat
   `make up client` from a `sudo -iu orcaster` shell without those variables:
   the target derives them from `/run/user/<uid>`.

5. If this host no longer uses automatic start at boot, the unit file
   (`~/.config/systemd/user/container-orcaster-worker.service`, or that of the
   suffixed worker) is a leftover. Remove it as `orcaster` and `make up client`
   stops querying the manager.

<details>
<summary>Technical Information</summary>

`sudo -iu` does not open a logind session and so the shell does not bring
`XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS`
([pam_systemd(8)](https://www.freedesktop.org/software/systemd/man/latest/pam_systemd.html)).
`make up client` and `make up proxy` derive them when they are missing, but
only if `/run/user/<uid>` exists, and that directory is created by the user
manager, which without an open session only runs with linger
([loginctl(1)](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)).
With the environment resolved, `make up client` probes the manager with
`systemctl --user show-environment` as a prerequisite that runs before
recreating the egress sidecar (for the worker's unit and, with egress
isolation, the sidecar's); if it does not respond, it exits with an error
without recreating anything. A failed `systemctl --user stop` also aborts
before touching the container (if the sidecar had already been recreated in
the same command, the error says so). `install-release.sh` makes the same
query before replacing the code, only if some worker or sidecar unit file
exists, and if the manager does not respond it aborts without installing
anything.
The same probe also fails in `06-autostart.sh`, which ends with an error
without regenerating any unit (`ERROR: could not reach the systemd --user
manager of the worker; no unit was regenerated.`).

</details>

<a id="systemd-unit-out-of-sync"></a>
### The `systemd --user` unit is out of sync (`Could not re-sync the autostart`)

If `make up client` ends with `ERROR: Could not re-sync the autostart (<unit>).
Tried with CONFIG_DIR=<dir>.` (or `WARNING: the autostart re-sync of worker N
failed`, in a release deploy), the container was recreated and is running, but the `systemd --user`
unit is not in sync with it. While it stays that way, **do not run `systemctl
--user daemon-reload`** on that host: it can stop the worker. The same
situation can be left by an earlier recreation done from a shell without access
to the `orcaster` manager. In a release deploy, `install-release.sh` re-syncs
on its own: if that fails it prints `WARNING: the autostart re-sync of worker N
failed` and goes on with the next worker; the deploy detects the stopped unit and ends with an
error at the end. To check it and repair it:

1. **Check** whether the unit and the container point to the same process. The
   two numbers must match (`-<n>` is the worker index; for the worker without a
   suffix, omit it):

   ```bash
   cd /
   uid=$(id -u orcaster)
   sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$uid \
       DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus \
       systemctl --user show -p MainPID --value container-orcaster-worker-<n>.service
   sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$uid \
       podman inspect -f '{{.State.ConmonPid}}' orcaster-worker-<n>
   ```

   If they match, there is nothing to repair. If the host runs with egress
   isolation, repeat the comparison with `container-orcaster-proxy-<n>.service`
   and `orcaster-proxy-<n>`.

2. **If they differ, do not run `systemctl --user daemon-reload` on its own.**

3. **Pause the worker** from the dashboard (**Pause** button; see [Pausing and
   resuming a worker](#pausing-and-resuming-a-worker)) or wait until it has no
   task running. The re-sync can restart the worker and its proxy, which is why
   it should be idle.

4. **Re-sync** the unit, giving the host's real configuration directory (the one
   that contains its `setup.env`):

   ```bash
   sudo env CONFIG_DIR=<host-config-dir> N=<n> \
       bash /root/orcaster/setup-scripts/debian13-worker/06-autostart.sh
   ```

   For the worker without a suffix, also remove the `N=<n>` word from the
   command: the rule of omitting `-<n>` in step 1 covers the unit names, not
   this variable.

5. **Verify** again that the two numbers from step 1 match and that the
   container is running:

   ```bash
   sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$(id -u orcaster) podman ps
   ```

   If the unit was left stopped, start it as `orcaster`, with the same
   variables as in step 1: `systemctl --user start
   container-orcaster-worker-<n>.service`. If you had paused the worker, resume
   it with **Resume**.

<details>
<summary>Technical Information</summary>

The unit is generated in adoption mode (`podman generate systemd` without
`--new`) and embeds a `PIDFile` with the container ID: its `MainPID` is that
container's monitoring process (conmon), which is what `podman inspect -f
'{{.State.ConmonPid}}'` shows. When the container is recreated, the ID and the
PID change. If the manager did not find out (the previous stop or the re-sync's
`daemon-reload` did not reach its bus), it keeps the old unit loaded with a
`MainPID` that no longer exists; the next `daemon-reload` that does reach the
bus reloads the already regenerated file and stops the container.
`06-autostart.sh` generates the unit file again, reloads the manager and
enables it with `enable --now`, so disk and manager are consistent again.

</details>

<a id="egress-precondition-errors"></a>
### Egress isolation precondition errors

| Message | Cause | What to do |
|---------|-------|------------|
| `ERROR: aardvark-dns not detected…` | `aardvark-dns` is not installed. | Install the `aardvark-dns` package. |
| `ERROR: aardvark-dns X < 1.11.0.` | The installed version is older than the minimum validated one. | Update `aardvark-dns` to 1.11.0 or later. |
| `netavark: nftables error: unable to execute nft` | `nftables` is missing on the host. This precondition is **not** validated beforehand by the `Makefile`: the failure only appears when the egress network is created. | Install the `nftables` package. |

<a id="fleet-enrollment-timeout"></a>
### Fleet enrollment timeout

`Enrollment timeout (120s): worker(s) not enrolled: …` in
`run-fleet.sh`/`cloud-init-worker.sh` means one or more workers started (step
`1/2`) but did not manage to register within the time limit (step `2/2`,
`ENROLL_WAIT`, 120 seconds by default). Check whether the enrollment token
expired or used up its `max_uses`, and the logs of each affected worker:

```bash
podman logs orcaster-worker-<N>
```

### Early abort of `run-fleet.sh`

`Worker N has no credentials: ORCH_ENROLLMENT_TOKEN is empty in setup.env…` means
worker N has nothing to start with: `setup.env` had no token configured, its
`env.N` carried no `ORCH_API_KEY` or earlier token, and its `orcaster-data-N`
volume has no `state.json` with `worker_id` and `api_key` (or it could not be
read as the worker's user). The script stops at that worker, without starting
it, instead of leaving it in a silent crash-loop. Set `ORCH_ENROLLMENT_TOKEN`
and run it again.

A worker that has already enrolled does not need a token: on startup it loads
its identity from that `state.json`. That is why, in an enrolled fleet, the
expired token can be removed from `setup.env` and from each `env.N`, and
`run-fleet.sh` goes ahead with the notice `state.json in orcaster-data-N
already holds worker_id and api_key: reusing that identity`. The script only checks that the
two keys exist with a value; it never prints their content.

<a id="freeing-disk-when-the-worker-fills-up"></a>
### Freeing disk when the worker fills up

The worker frees space in `/var/cache/orcaster` in three ways, in the order in
which they act:

1. **Deletion on success**: when a task ends with state **completed**, its
   working cache is deleted automatically (unless you disable it with
   `ORCH_ORCASTER_CACHE_DELETE_ON_SUCCESS=false`). On **failed** or
   **cancelled**, the cache is preserved so it can be inspected.
2. **Periodic garbage collection (GC)**: by age (deletes anything older than 3
   days) and by pressure (if less than 20 % of space remains free, it deletes
   the least recently used until 80 % is recovered).
3. **Clear cache on demand**: a manual action from the dashboard. See
   [Clearing the worker cache (Clear cache)](#clearing-the-worker-cache-clear-cache).

If the disk still stays under 20 % free, the worker **stops asking for new
tasks** until space is recovered. It does this so as not to start downloads
doomed to die with "No space left on device", which waste one attempt of each
task without producing anything.

What you see while that happens:

- **The dashboard does not give it away.** The worker keeps appearing
  connected (**online**) and with no task in progress, just like a healthy one
  waiting for work. What you will see is that it stops taking tasks while the
  other machines keep advancing, and that its **Disk** usage is at the top (the
  ring on the card in the dashboard, the bar in the worker detail).
- The explicit signal is in the **worker log**: a warning that names the free
  space measured, the floor and the volume path.
- **Clear cache** does not reach it while it is in that state: the system only
  delivers that signal to an idle worker. To unblock it, free space on the
  machine over SSH, or disable the brake with
  `ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION=0` in its `.env`.
- When braking, the worker triggers **an immediate cache cleanup** instead of
  waiting for the next scheduled pass, to shorten the time it is stopped.
- If the space does not come back, the worker **gives up after half an hour**
  and takes tasks again anyway. From then on they fail again because of a full
  disk, which is visible in the dashboard, instead of staying silent and idle
  forever. That half hour counts only the time it is **braked**: if in the
  meantime it runs a directed task, the clock stops while it lasts and resumes
  afterwards, so a long task does not consume the allowance. The allowance
  also does not survive a worker restart: when the container is recreated it
  starts again from zero. It is adjusted with
  `ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS` (`0` disables it and leaves the
  worker braked until someone intervenes).

**To set these values across the whole fleet (not just on one worker over
SSH):** declare them in the deploy suite's `setup.env`
(`setup-scripts/debian13-worker/setup.env.example`), and do not edit them by
hand in the worker's `.env`. That file is regenerated on every run of the
suite (`03-worker-env.sh` in the manual flow, `run-fleet.sh` in fleet/cloud-init),
which only preserves the credentials (`ORCH_API_KEY` and
`ORCH_ENROLLMENT_TOKEN`): a value set by hand there **keeps disappearing** on
the next rerun, without a warning. That stays as it is, and that is why a
change over SSH is only useful as an immediate patch for one worker. From
`setup.env`, in contrast, the value survives any rerun because the suite reads
it again and stamps it on every run.
See the deployment section [Deploying a Fleet](#deploying-a-fleet).

If the same key is set both in `EXTRA_ENV` (the generic `setup.env` channel
for runtime secrets of the tasks) and in the dedicated channel above, **the
dedicated channel wins**: the suite stamps it after the `EXTRA_ENV` block in the
worker's `.env`, and the container startup resolves a repeated key with the
last occurrence in the file. The suite warns on the console when it detects the
same key in both channels, so you can clean up the leftover in `EXTRA_ENV` and
not have two different values living together unnoticed.

When to touch each one?

- **Lower the floor** (`ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION` below
  the 20 % inherited from the GC): when that 20 % is too conservative for the
  size of the worker's disk — on a large volume, 20 % free is a lot of idle GB
  — and you prefer to keep accepting tasks with less margin.
- **Disable the gate** (`=0`): when the volume has permanent content outside
  the task cache (a worker-wide `uv` cache, large downloaded blobs) that the GC
  cannot free, so the gate would stay closed with no way out; or when you
  prefer tasks to fail visibly with "no space" rather than leave the worker
  braked silently.
- **Lower or disable the fail-open allowance**
  (`ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS`): if the gate brakes many
  workers at once and they must be unblocked right now, while the real cause
  of the full disk is investigated.

A task **directed** to that particular worker does reach it and does run: the
brake decides whether the worker *asks for* work, not whether it runs what it
has already received. The task in progress is never aborted for lack of disk
either.

**Measure the whole fleet before raising the floor or upgrading workers.** The
dashboard's **Disk** indicator does not reproduce this criterion near the
threshold (it is derived from `disk_mb`/`disk_used_mb`, which is not
`free/total`). On a fleet that may be tight on disk,
`setup-scripts/debian13-worker/fleet-disk-check.sh` goes through the hosts
over SSH and measures, worker by worker, the same `free/total` that the gate
uses, marking those that are already (or would be) under the floor — see the
README in that folder for usage. It is read-only and does not replace the live
signal: it is the manual gate before a rollout, not a replacement for
continuous monitoring.

---

<a id="analyzing-completions-and-failures-outcomes"></a>
## Analyzing Completions and Failures (Outcomes)

The **Outcomes** page groups the workspace's terminal tasks by their error
signature, with breakdowns by exit code, worker and outcome. It is the triage
tool when something fails in volume. To open it, click **Outcomes** in the top
bar.

Under the title you will see the legend *Terminal tasks in this workspace,
grouped by error signature, with breakdowns by exit code, worker and outcome.*

### Narrowing what is analyzed

A row of controls filters the analysis:

- **Time window:** the time window. By default it shows the current day; you
  can widen it (last 24 hours, 7 days, 30 days or the whole history).
- **Limit:** how many error signatures to show (25, 50, 100 or 200).
- **Terminal status:** the terminal state to analyze. **failed** by default;
  also **completed**, **cancelled** or **all**.
- **Outcome:** the nuance of the completion. **All** includes every one, or
  choose a specific outcome (for example, `no_result`).
- **Version:** the same version lens as the dashboard (see [Filtering and
  searching tasks](#filtering-and-searching-tasks)); it narrows the analysis to
  the version of a tool. The list offers **all** the workspace's versions,
  including archived ones, so you can jump to one even if it has no recent
  activity. The number next to each version counts all its finished tasks
  (completed, failed and cancelled, without trimming by the time window or by
  the status chosen above), so the dropdown always says the same thing while you
  move the other filters. A `· 0` means that version has no finished task yet:
  you can still choose it, but the table will be empty. The **(current)** mark
  points to the most recent version with finished tasks (or all those tied as
  the most recent).

Changes apply instantly. While the system applies a filter change, the controls
dim and stay disabled until the result arrives; if you change another filter
before that load ends, the new selection replaces the previous one (they are
not queued). The **⟳ Refresh** button reloads the block manually and behaves
the same while one is in progress.

### Reading the results

The summary says how many terminal tasks enter the analysis (**N terminal
tasks**) and the cutoff time (**as of …**), followed by the count per
combination of state and outcome.

Tasks are grouped by **Signature** (the error's signature), with its **Count**
(how many share that signature) and the **Exit codes** involved. Each group
brings an example with its command, the worker, the exit code, the duration and
excerpts of **stderr**, **stdout** and **payload**. The **download full log**
link downloads the example's complete record, and under **tasks:** the affected
tasks (`#N`) are listed as links to their detail.

On one side, two panels summarize the distribution: **By exit code** and **By
worker**.

If there are no terminal tasks in the chosen window, the page says so (for
example, **No failed tasks in the last 24 hours.**).

### Exit codes

To interpret the `exit N` you see in a group or in a run's detail:

| Code | Meaning | What to do |
|------|---------|------------|
| `0` | Success. | No action. |
| `75` (`EX_TEMPFAIL`) | Transient failure. Use it in your own scripts to distinguish throttling from a genuine failure. It is the default value of `retryable_exit_codes`, but it is **not retried on its own**: the task must declare `auto_retry` with `max_attempts` greater than 0. Without that, `75` closes the task as `failed` just like any other code. | If the task has `auto_retry.max_attempts > 0`, the server retries it until that budget is used up. If not, retry with **Retry** or create the task with the policy (see [`POST /admin/tasks`](#post-admintasks--create-a-task)). |
| Any other `> 0` | Failure. Terminal unless the task declares `auto_retry` with `max_attempts` greater than 0 and that code in `retryable_exit_codes`. | Check the `exit N` and the error excerpt in the run's detail. |

<details>
<summary>Technical Information</summary>

- Page: `GET /admin/outcomes`; the block is refreshed with
  `GET /admin/outcomes/partial`. The old `/admin/failures` route redirects
  (308) to `/admin/outcomes`.
- An **outcome** is the pair `(name, closes_as)` that the task declares in its
  payload and that the server assigns according to the exit code. `closes_as`
  can only be `completed` or `failed`. A tool's default outcome map is declared
  by the server plugin that recognizes that tool. Without a plugin that
  recognizes it, the server only classifies the exit codes that the task
  declares in the `outcomes` block of its payload, and any other non-zero code
  closes as `failed` without an outcome. For example, a plugin can declare that
  its tool's exit code 3 closes as `completed` with the outcome `no_match`, and
  that 4 closes as `failed` with `unsupported_source`. A `completed` **with**
  an outcome still counts as success; the nuance is shown separately.
- Public API (with a PAT): `GET /api/v1/insights/outcomes` (params `status`,
  `outcome`, `variant`, `since`, `until`, `limit`) and
  `GET /api/v1/insights/failures`. The default window is the current day; for
  the full history pass an old `since`.
- If the client does not send `until`, the query has no upper bound; the
  response reports the resolved instant in `filters.until` for information
  only. An explicit `until` bounds the results.

</details>

---

## Administering the Workspace

A **workspace** isolates a team's tasks, workers and records. To open its
administration, click **Workspaces** in the top bar.

### Switching workspace

If you belong to more than one workspace, a **Workspace** selector with the
name of the active one appears in the top bar. Click to open the list and
choose another; the active one is marked with **✓**.

### Roles

Your role in the workspace appears as a badge: **owner** or **member**.
Some actions (renaming the workspace, inviting members, managing [share
links](#sharing-status-with-third-parties-share-links) and setting or deleting
[task secrets](#task-secrets), and controlling workers: pause, resume, clear
cache, delete and worker commands) are exclusive to **owners**. Members see
workers and their status, but not the control buttons; a direct request
answers `403`. A member who opens an owner-only page sees an **Access denied**
page instead of being sent back to sign-in. Worker enrollment tokens and
personal access tokens are self-service: each member manages their own and
the owner sees them all.

### Renaming the workspace (owner only)

In the **Details** section, under **Rename workspace**, edit the name and click
**Rename**. If you leave the name empty, the system shows **Workspace name
cannot be empty.**

### Inviting a member (owner only)

In the **Members** section:

1. Under **Invite a member**, choose the role (**member** or **owner**).
2. Click **Generate invitation link**.
3. The system opens a blocking **Invitation link created** window with the
   legend *Copy it now. For security it is shown once and cannot be retrieved
   later.* and the link. Click **Copy link** to copy it; the **Done** button
   stays disabled until you copy it. Share the link with the person: it is
   used only once to join.

If the person already has an account (for example, they are a member of
another workspace), they must open the link while signed in and choose **Join
as** with their name: that way they join with their current account instead of
creating another one (see [Joining another workspace with your current
account](#joining-another-workspace-with-your-current-account)).

The **Members** section also lists the current members with their **Name**,
**Role** and the date they joined (**Joined**, in your local zone; see [Dates
and times](#dates-and-times)).

### Worker enrollment tokens

An enrollment token lets one or several workers register against this
workspace. Any member can generate them. In the **Worker enrollment tokens**
section:

1. Under **Generate enrollment token**, optionally enter a label in
   **Label**.
2. In **Max uses**, indicate how many workers can register with this same
   token, from 1 to 1000 (default 1, that is, a single use).
3. In **Expires in**, choose its validity: **1 hour**, **1 day**, **7 days**
   (default option) or **30 days**.
4. Click **Generate token**.
5. The system opens a blocking **Worker enrollment token created** window
   with the legend *Copy it now. For security it is shown once and cannot be
   retrieved later.* and a block ready to paste into the worker's
   `client/.env` file:

   ```
   ORCH_BASE_URL=<the URL of this server>
   ORCH_ENROLLMENT_TOKEN=<the token just generated>
   ORCH_WORKER_NAME=
   ```

   Click **Copy .env block** to copy it; the **Done** button stays disabled
   until you copy it. The same window includes a link to download
   **server-ed25519.pub**, which you must save as `client/server-ed25519.pub`
   next to the `.env` (it is a public key, not a secret). Paste the block into
   `client/.env` and start the worker with `make up client`.

   If you set **Max uses** above 1, the same token serves to register more than
   one worker: repeat this block (with a different `ORCH_WORKER_NAME`) in each
   one's `client/.env`, as long as the token has not used up its uses or
   expired.

### Downloading the server's public key

In the same section, the **Download server public key** link downloads the
`server-ed25519.pub` file. Each worker needs it to verify the signature of
tasks; it is public, not a secret. (The same link appears inside the window
that opens when you create a token.)

### Managing existing tokens

The table lists the tokens with their **Label**, **Status**, how many uses they
have consumed (**Uses**, as `used / maximum`), expiration date (**Expires**, in
your local zone) and how long ago they were last used (**Used at**). The
tooltip of the date columns shows the exact value in UTC (see [Dates and
times](#dates-and-times)). Depending on its state, each token offers a
different action:

- An **active** token shows **Revoke**: it cancels it so that no more workers
  can register with it, even if it still has uses or validity left. The system
  asks for confirmation (**Revoke token?**).
- A **revoked**, **expired** or **exhausted** token shows **Delete**: it
  removes it from the list permanently. The system asks for confirmation
  (**Delete token?**).
- If the token has already registered at least one worker, its row adds the
  **Workers enrolled** button, which expands the list of workers registered
  with it and how long ago each one did so.

> An **owner** sees all the workspace's tokens; a **member** sees only those
> they created themselves.

### Token states

| State | Meaning |
|-------|---------|
| **active** | Valid, with at least one use and some time available. |
| **exhausted** | The **Max uses** quota was used up: it has already registered all the workers it allowed. |
| **expired** | It expired before using up its uses. |
| **revoked** | Cancelled manually. |

### Personal access tokens

A personal access token (PAT) is a credential you generate to operate your
workspace from the `orcaster` CLI or from the `/api/v1` JSON API, without going
through the browser. It is the personal equivalent of a worker's API key: you
issue it, revoke it when you no longer need it, and can have several at once
(for example, one per machine).

To manage your personal access tokens, click **Tokens** in the top bar. The page
shows the issuing form and the table of existing tokens.

#### Issuing a personal access token

In the **Generate a personal access token** section:

1. (Optional) In **Optional label (e.g. my-laptop)**, enter a label that helps
   you identify the token later.
2. In the **Expires** dropdown, choose the validity:
   - **No expiration**: the token does not expire.
   - **30 days**, **60 days** or **90 days**: the token expires after those
     days from its issue.
3. Click **Generate token**.
4. The system opens a blocking **Personal access token created** window with
   the legend *Copy it now. For security it is shown once and cannot be
   retrieved later.* Click **Copy token** to copy it; the **Done** button
   stays disabled until you copy it.

Store the token in a safe place: **you will not be able to see it again**. If
you lose it, generate a new one.

#### Listing and understanding the token table

The table lists your tokens (or, if you are an owner, all those of the
workspace) with the following columns:

| Column | What it shows |
|--------|---------------|
| **Label** | The label you assigned when issuing (it may be empty). |
| **Status** | The token's current state (see below). |
| **Expires** | The expiration date, or **Never** if it does not expire. |
| **Last used** | The last time the token authenticated a request. |
| (actions) | The action available depending on the state. |

**Expires** is shown in your local zone; **Last used** says how long ago the
last time was. The tooltip of both columns shows the exact value in UTC (see
[Dates and times](#dates-and-times)).

> An **owner** sees all the workspace's tokens; a **member** sees only those
> they created themselves.

#### Personal access token states

| State | Meaning |
|-------|---------|
| **active** | Valid; it can be used to authenticate requests. |
| **expired** | It expired by reaching its expiration date. |
| **revoked** | Cancelled manually; it can no longer be used. |

If your user is disabled, your tokens stop working (the API responds `401`)
even if they remain **active**; the block applies to every request that
arrives after the disabling, and a request already in progress may finish. They
are not revoked: if the user is re-enabled, the same tokens work again, except
those that are **expired** or **revoked** (they remain unusable) and those of a
user who is no longer a member of the workspace (the API responds `403`).
Whoever operates the server sees the rejection in the log as a warning
(WARNING) per token at most every 5 minutes (per server process), with the
count of rejected requests; the warning names the user and the token by its id
and never includes the secret.

#### Revoking a token

An **active** token can be revoked: click **Revoke** on its row. The system
asks for confirmation. After the revocation, any CLI client or API call that
used that token stops working immediately.

An **active** token cannot be deleted directly; revoke it first.

#### Deleting a token

A **revoked** or **expired** token can be deleted from the list permanently:
click **Delete** on its row. The system asks for confirmation. This action
cannot be undone.

<details>
<summary>Technical Information</summary>

- Endpoints: `GET /admin/pats` (page), `POST /admin/pats` (issue; form fields
  `label` and `expires_in_days` ∈ `{"", "30", "60", "90"}`),
  `POST /admin/pats/{token_id}/revoke` and `POST /admin/pats/{token_id}/delete`
  (only tokens in state **revoked** or **expired**).
- The PAT travels in `Authorization: Bearer orcaster_pat_…`. The scheme name is
  case-insensitive and allows more than one space before the token
  (RFC 7235 §2.1), so a client or gateway that sends `bearer` or `BEARER`
  authenticates the same; the token itself is case-sensitive.
- The secret has the format `orcaster_pat_<base64url(32 bytes)>`. Only
  `sha256(token)` is persisted in the database; the plain text is revealed only
  once with the same PRG + single-use flash cookie pattern as enrollment
  tokens, so reloading the page does not issue additional tokens.
- `expires_at NULL` in the database is equivalent to "no expiration". Unlike
  enrollment tokens, PATs have no `max_uses` and no **exhausted** state: they
  can be used indefinitely until they expire or are revoked.
- PATs are scoped to the workspace that was active at the time of issue; the
  user's role is resolved dynamically on each request (it is not frozen in the
  token).

</details>

<a id="task-secrets"></a>
### Task secrets

A task secret is a value (a GitHub token, an API token or similar) that the
server stores encrypted for the whole workspace and delivers to each task that
declares its name in **Environment variables** (`task_env`). That way a token is
changed in a single place and applies from the next task, without touching the
workers' hosts.

Values are **write-only**: once stored, nobody can see them, neither in the
dashboard, nor in the CLI, nor in the API. Only a **fingerprint** is shown,
which lets you see whether a value changed or whether two secrets match.

To manage them, click **Secrets** in the top bar. The page shows the secrets
table and, if you are an owner, the form to set them.

> If the page shows the notice *Task secrets are disabled on this server*, the
> server has no encryption keys configured: secrets cannot be set (they can be
> deleted) and tasks that declare a name with a stored secret stay queued until
> they are configured. Whoever operates the server resolves it; see [Configuring
> the task secret keys](#configuring-the-task-secret-keys). If the
> notice is *The server could not decrypt stored secret(s) …*, no server key
> decrypts those secrets and the tasks that declare them stay queued; see [A
> task stays queued because the server cannot decrypt a
> secret](#a-task-stays-queued-because-the-server-cannot-decrypt-a-secret).

#### Setting a secret (owner only)

In **Set secret**:

1. In the **Name (e.g. API_TOKEN)** field, enter the name of the environment
   variable as tasks declare it.
2. In the **Value** field, paste the value. **The field shows it in clear text
   while you type**, to preserve its line breaks: a PEM key or a JSON of
   credentials is pasted as is. Make sure nobody sees the screen.
3. In the scope selector, leave **All workers (workspace-wide)** so the secret
   reaches all the workspace's workers, or choose a worker so that only that
   worker receives it.
4. Click **Save secret**.

If the name already exists with the same scope, the value is replaced. The
system confirms with *Secret API_TOKEN saved.* (or *Secret API_TOKEN saved for
worker worker-a.*) and the row shows the new fingerprint.

**A different value per worker.** The same name can have a secret for the whole
workspace and, in addition, one of its own for any worker. That worker receives
its own; the others receive the workspace one, if it exists, or look for the
name in their own environment. A worker never receives another worker's secret.
It is useful, for example, so that each worker uses its own GitHub token. Keep
in mind that GitHub limits requests per user and not per token: two tokens of
the same account share the same limit.

**Name rules.** Letters, digits and underscore, not starting with a digit, up
to 128 characters. The **Environment variables** lines of a task follow the
same shape rule, the same 128-character limit and the same rejection of token
prefixes, without the list of reserved names. The following are not accepted:

- names that start with a known token prefix (`hf_`, `ghp_`, `gho_`, `ghs_`,
  `ghu_`, `ghr_`, `github_pat_`, `glpat-`, `xoxb-`, `xoxp-`, `sk-`),
  case-sensitive: it is almost always the token pasted instead of the name.
  `API_TOKEN` and `GITHUB_TOKEN` remain valid. The error message does not
  repeat the name;

- names that start with `ORCH_`, in upper or lower case (they belong to the
  worker itself);
- names that control the task's execution environment (the shell, the loader,
  the interpreters, the build tools, the installers, git, the certificates, the
  proxy or helper programs), regardless of upper or lower case:
  - exact: `PATH`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR`,
    `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`,
    `CURL_CA_BUNDLE`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`,
    `BASH_ENV`, `ENV`, `IFS`, `CDPATH`, `SHELLOPTS`, `BASHOPTS`, `PS4`,
    `PROMPT_COMMAND`, `GCONV_PATH`, `LOCPATH`, `GLIBC_TUNABLES`,
    `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, `JDK_JAVA_OPTIONS`, `PERLLIB`,
    `CLASSPATH`, `PHPRC`,
    `CC`, `CXX`, `CPP`, `LDSHARED`, `CFLAGS`, `LDFLAGS`, `MAKEFLAGS`,
    `GOFLAGS`, `RUSTC_WRAPPER`, `LESSOPEN`, `PAGER`, `EDITOR`, `VISUAL`,
    `SHELL`, `CURL_HOME`, `WGETRC`, `HGRCPATH`, `HF_ENDPOINT`,
    `SSLKEYLOGFILE`, `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`,
    and the task directories `ORCASTER_CACHE_DIR`, `ORCASTER_ARTIFACTS_DIR`,
    `TASKORCH_CACHE_DIR` and `TASKORCH_ARTIFACTS_DIR`;
  - those that start with `LD_`, `DYLD_`, `PYTHON`, `UV_`, `PIP_`,
    `SSL_CERT_`, `GIT_`, `PERL5`, `PERL_`, `RUBY`, `GEM_`, `BUNDLE_`,
    `NPM_CONFIG_`, `JAVA_`, `DOTNET_`, `CORECLR_`, `NODE_`, `CARGO_`,
    `OPENSSL_` or `XDG_`;
  - those that end in `_CA_BUNDLE`.

  If a task needs one of them, its value still comes from the worker's
  environment; the four task directories are always set by the worker itself
  (see [Where a task writes](#where-a-task-writes)). `GITHUB_TOKEN` is
  allowed. This list prevents whoever manages the secrets from changing how a
  task's tools run; it does not isolate the task, and it is closed except for
  one specific case.

**Value rules.** Non-empty, without NUL characters and up to 64 KiB. A value
that looks like a multiline value that lost its line breaks is rejected: a PEM
on a single line (or only its `-----BEGIN ...` line) or a lone `{` or `[`, the
first line of a JSON. Line breaks that the browser sends as CRLF are stored as
LF.

#### Reading the secrets table

| Column | What it shows |
|--------|---------------|
| **Name** | The variable's name. |
| **Scope** | **Workspace** if all workers receive it, or **Worker** and the name of the worker that receives it. |
| **Fingerprint** | The value's fingerprint: `hmac-sha256:<12 hex>`. It contains no character of the value. |
| **Updated** | When the current value was set. |
| **Updated by** | Who set it, or **deleted user** if that account no longer exists. |

The fingerprint is computed by the server with its own key, so it cannot be
reproduced outside it: whoever obtains a copy of the database cannot test
values against it. It is useful to see whether a secret changed (the
fingerprint changes) or whether two secrets of the workspace have the same
value (the fingerprint matches). It also changes when the operator rotates the
server's keys, even if the value is the same.

#### Deleting a secret (owner only)

Click **Delete** on its row and confirm with **Delete secret**. Only that row
is deleted: deleting a worker's secret does not touch the workspace one, and
vice versa. From the next task, that worker takes the workspace secret again,
if it exists; if none remains, tasks that declare that name take the value from
the worker's environment, if it has one. This action cannot be undone. Deleting
does not require the server to have its encryption keys configured: it is the
owner's way out when a stored secret leaves tasks queued and it cannot be
decrypted or the keys are missing.

A secret saved under a name that later became reserved (for example
`TASKORCH_CACHE_DIR`, saved before the task directories were reserved) still
shows in the table and can be deleted as usual. A worker of this version or
later never passes its value to a task: it applies its own value instead. An
older worker may still pass it, so delete it.

#### From the `orcaster` CLI

```bash
orcaster secret set API_TOKEN                       # in a terminal it asks for the value without echo
pass show api/token | orcaster secret set API_TOKEN # from a pipe
orcaster secret set GCP_CREDENTIALS < sa.json       # a multiline value (PEM, JSON)
orcaster secret list                                # name, fingerprint, date and author
orcaster secret list --json
orcaster secret delete API_TOKEN                    # asks for confirmation; --yes skips it

# A value of its own for a worker (by id or by name):
pass show gh/worker-a | orcaster secret set GITHUB_TOKENS --worker worker-a
orcaster secret delete GITHUB_TOKENS --worker worker-a --yes
```

`orcaster secret list` shows the `SCOPE` column: `workspace` or
`worker:<name>`. Without `--worker`, `set` and `delete` act on the workspace
secret. If the worker name matches more than one worker, use its id; also if
the name has spaces at the start or end, because `--worker` is not trimmed and
such a value is rejected.

`orcaster secret set` reads the value **only from standard input**, never from
an argument, so that it does not end up in the shell history or in the process
list. From a pipe it strips exactly one trailing line break. Setting and
deleting require a personal access token of an owner.

The terminal prompt reads **a single line**. A multiline value, such as a PEM
key or a JSON of credentials, is passed through a pipe or with
`orcaster secret set NAME < file`. If it is pasted into the prompt, `orcaster`
rejects it without saving anything and discards the rest of what was pasted so
the shell does not run it.

If the server has no encryption keys configured, `orcaster secret list` still
shows the stored secrets and warns on the error output (*Note: task secrets are
disabled on this server ...*) that they cannot be set or delivered to tasks;
`orcaster secret set` fails with a message that explains it, and `orcaster
secret delete` works as usual, because deleting does not need the keys.

#### What a task receives

- A task that declares `API_TOKEN` in **Environment variables** receives the
  workspace's `API_TOKEN` secret. If the workspace does not have it, it receives
  the value from the worker's environment (`EXTRA_ENV`); if it does not exist
  there either, the variable is not defined. With the same name on both sides,
  the workspace one wins. The exception are the reserved names above: for them
  the worker's environment value always applies, and the four task directories
  always take the worker's own directories.
- A task that uses stored secrets only runs on workers that support stored task
  secrets; until one is available it stays queued (see [Using task secrets
  instead of `EXTRA_ENV`](#moving-the-extra_env-tokens-to-the-workspace-secrets)).
- A task without **Enable isolation** (legacy mode) does not receive workspace
  secrets: it runs with the worker's full environment.
- The output the task sends to the dashboard shows `***` instead of values of 8
  or more characters. It is a best-effort protection: a value that the task
  itself transforms (for example, into base64) is not hidden, and the artifacts
  the task writes are not checked.
- Any member who can create tasks can declare a name and read the value from
  inside the task. Share the workspace only with whoever should use its
  secrets.

<details>
<summary>Technical Information</summary>

- Dashboard: `GET /admin/secrets` (page), `POST /admin/secrets` (form fields
  `name` and `value`; owner) and `POST /admin/secrets/{name}/delete`
  (owner). A member receives `403`.
- API: `GET /api/v1/secrets` (any member) returns
  `{"items": [{"name", "fingerprint", "created_at", "updated_at",
  "updated_by"}], "keys_configured": true}`; `PUT /api/v1/secrets/{name}` with `{"value": "..."}`
  (owner) returns the metadata, never the value; `DELETE
  /api/v1/secrets/{name}` (owner) returns `204`, or `404` if it does not exist.
  With no keys configured on the server, `PUT` responds `503` with `error`
  equal to `task_secrets_unconfigured`, `GET` responds `200` with
  `keys_configured` as `false` and `DELETE` keeps working (with or without
  `?worker=`), because deleting does not decrypt. A `422` does not repeat the
  received value.
- The secret's name travels in the URL of `PUT` and `DELETE
  /api/v1/secrets/{name}`. The server validates it once the request has
  already arrived: a proxy that logs paths has already stored the name, even
  if the `422` does not repeat it. If you paste a token where the name goes,
  the token ends up in that log. `orcaster secret set` rejects a name with a
  known token prefix before sending the request; `orcaster secret delete`
  validates only the shape, so that a row stored before that check can be
  deleted. A direct API client (curl, a script) has no prior check: validate
  the name before sending it. No proxy in front of the server should log the
  paths of `/api/v1/secrets/`.
- The server stores the value encrypted (`MultiFernet`) in the `task_secrets`
  table. On each poll it delivers to the task, in the response's `secrets`
  field (outside `payload` and the Ed25519 signature), only the secrets whose
  name `payload.task_env` declares; it does not store them in the task or in
  the run.
- Audit (**Audit log**): `set_task_secret` and `delete_task_secret` when
  setting or deleting (with the name, never the value) and
  `deliver_task_secrets` on each delivery to a worker, with the task id, the
  worker and the names.
- Version floor: the server reads the leading `MAJOR.MINOR.PATCH` of the
  version the worker reports (`ORCH_VERSION`, by default the package's) and
  ignores suffixes such as `+patched`; a missing, empty or unreadable version
  counts as below the floor. For a worker below the floor, the server does not
  decrypt, send or audit secrets. If that worker polls free (not paused and
  with no storage or disk gate) and receives no task, the server log records,
  at most once every 10 minutes per worker and per server process, a `WARNING`
  `task_secrets.worker_unsupported` with the worker id, its version, the
  minimum version and `queued_tasks`: the `queued` tasks that use stored
  secrets, with `run_at` reached and not expired (workspace claims and pushes
  directed to that worker), without filtering by pool, capabilities or budget.
  `GET /admin/workers.json` adds to each worker the `task_secrets_supported`
  field (boolean).
- In an `all-available` (or `all:<ids>`) task that uses stored secrets, the
  creation snapshot and the **Retry** one leave out the workers below the
  floor; if that leaves an `all:<ids>` with no workers, the task closes
  **failed** (`no_eligible_workers`). If the secret is stored after the task is
  created, the assignment of the older worker stays unclaimed until it expires
  through `claim_timeout_seconds` (residual case).
- A task sent directly to a worker (`delivery_mode` `push`) that uses stored
  secrets is not delivered in a poll in which the worker reports being busy: it
  stays queued until a poll in which it is free. In a busy poll the server
  never decrypts or audits secrets; if a secret is stored right between the
  choice of the push and its assembly, the push goes out without secrets (the
  worker discards it) and the log records a `WARNING`
  `task_secrets.busy_push` with the names.
- If a stored secret cannot be decrypted with any server key, or there are no
  keys configured, the server does not deliver the task and it stays in
  **queued**, without a run or a consumed attempt; see [A task stays queued
  because the server cannot decrypt a
  secret](#a-task-stays-queued-because-the-server-cannot-decrypt-a-secret).

</details>

<details>
<summary>Technical Information</summary>

- Workspace switch: `POST /admin/workspace/switch`. Rename:
  `POST /admin/workspaces/rename` (owner). Invite:
  `POST /admin/workspaces/invite` (owner). Accept an invitation with the
  current account: `POST /auth/accept/{token}/join` (requires a session; it
  consumes the invitation and creates the membership in one transaction,
  without creating a user).
- Tokens: `POST /admin/workspaces/enrollment-tokens` (issue, with `max_uses`
  from 1 to 1000 and `ttl`), `.../{token_id}/revoke` (cancel) and
  `.../{token_id}/delete` (delete an already **revoked**, **expired** or
  **exhausted** token; **active** ones cannot be deleted). The worker passes
  the token as `enrollment_token` to `POST /v1/workers/register`, which
  increments its use counter on each successful registration.
- Public key: `GET /admin/workspaces/pubkey` downloads `server-ed25519.pub`
  (PEM `SubjectPublicKeyInfo`); accessible to any member.
- Issuing a token or an invitation uses POST-Redirect-GET: the secret travels in
  a single-use flash cookie and is revealed only once in the modal, so reloading
  the page does not generate new secrets.

</details>

---

<a id="sharing-status-with-third-parties-share-links"></a>
## Sharing Status with Third Parties (Share links)

A **share link** gives a third party a public, read-only status page of the
workspace, **without needing to sign in**. It is useful so that a client can
follow progress without being given access to the dashboard. Only an **owner**
can manage them. To open the page, click **Share links** in the top bar.

Under the title you will see the legend *A share link gives a client a
read-only public status page for this workspace, with no login. Revoke a link
to cut access immediately.*

### Creating a share link

1. (Optional) In **Label (e.g. Acme Corp)**, enter a label to identify the
   recipient.
2. Click **Create share link**.
3. The system shows the link with the legend *Link created.* and a button to
   copy it. It has the form `https://<server>/status/<token>`.
   - If the server has encryption keys configured (`TASK_SECRETS_KEYS_PATH`),
     the legend continues with *Its URL also stays available in the table
     below:* and you will be able to copy the URL again later from the table.
   - If it does not, the legend continues with *Copy it now; it is shown only
     once:* and the page shows the notice *This server has no encryption keys
     configured (TASK_SECRETS_KEYS_PATH), so a link's URL is shown only once,
     right after creating it, and cannot be recovered later.* Copy it at that
     moment.

### Managing links

The table lists the links with their **Label**, their **URL**, creation date
(**Created**), the last time someone opened it (**Last seen**, or **never**)
and their **Status** (**active** or **revoked**). Dates are shown in your local
zone (UTC in the tooltip; see [Dates and times](#dates-and-times)).

In the **URL** column, an active link shows its address with a button to copy
it. A revoked link never shows the URL. In the other cases you will see:

| Text | Meaning |
|---|---|
| *URL not recoverable; mint a new link* | The link was created without encryption keys. It still works, but its URL cannot be shown again: create a new one if you need it. |
| *URL not recoverable: encryption keys are not configured on this server* | The link was stored encrypted, but the server no longer has `TASK_SECRETS_KEYS_PATH` configured. |
| *URL not decryptable with the configured keys; mint a new link* | No key in the file decrypts the stored URL (for example, the key it was created with was removed). The link still works. |

To cut off access, click **Revoke** on the link's row and confirm (**Revoke
share link?**). The client loses access immediately and the stored URL is
deleted. This action cannot be undone.

### What the person opening the link sees

The public page is read-only and does not expose sensitive data. It shows:

- The overall progress (**N of M tasks processed**, with the breakdown
  **X completed · Z failed** and the percentage). The bar stacks a green
  segment (completed), a red one (failed) and the rest neutral (pending).
- Counters for **Workers**, **Active now**, **Queued** and **Failed**.
- The **Tasks by status** panel (with the outcome breakdown under
  **completed**).
- The **Worker fleet** panel, with each worker anonymized as **Worker 1**,
  **Worker 2**, etc. (without hostname, identifier or resources).

It supports the version lens and refreshes itself every 10 seconds. A revoked
link (or an invalid token) shows a generic not-found page.

<details>
<summary>Technical Information</summary>

- Management page (**owner-only**): `GET /admin/share-links`; create:
  `POST /admin/share-links`; revoke: `POST /admin/share-links/{id}/revoke`.
- Public page (no auth): `GET /status/{token}`; the summary is refreshed with
  `GET /status/{token}/summary` (10 s polling). Both routes respond
  `Cache-Control: public, max-age=10`; the rest of the `/status/*` plane is
  `no-store` by default.
- Behind those two routes there is also a server cache with a configurable TTL
  (`STATUS_SNAPSHOT_CACHE_TTL_S`, 10 s by default): a real change in the data
  can take up to ~20 s to show on the page. The link resolution is outside that
  cache, so revoking a link still cuts access immediately.
- The token is opaque; only its hash lives in the database. The aggregation is
  anonymous: it exposes no payloads or per-task data beyond the count per
  state.

</details>

---

<a id="auditing-what-happened"></a>
## Auditing What Happened

The **Audit log** records the workspace's administrative actions. To open it,
click **Audit log** in the top bar. The table refreshes every 3 seconds.

Columns:

| Column | What it shows |
|--------|---------------|
| **When** | When the action happened |
| **Action** | What was done (see below) |
| **Target** | Which task, worker or share-token it was applied to |
| **Reason** | The reason that was recorded (if one was entered) |
| **Actor** | Who performed it |
| **Payload** | Additional details of the action |

The **When** column shows the time in your local zone (UTC in the tooltip; see
[Dates and times](#dates-and-times)).

You can narrow the list with the **Action** and **Target** selectors and the
number in **Limit** (how many rows to show, up to 500).

Recorded actions, grouped by category:

- **Worker actions:** **delete_worker**, **clear_worker_cache**,
  **pause_worker**, **resume_worker**.
- **Task actions:** **cancel_task**, **delete_task**,
  **bulk_cancel_task**, **bulk_delete_task**, **bulk_retry_task**,
  **retry_task**, **auto_retry_task**, **fork_task**.
- **Credential actions:** **mint_pat**, **revoke_pat**,
  **delete_pat**, **mint_enrollment_token**, **revoke_enrollment_token**,
  **delete_enrollment_token**, **consume_enrollment_token**.
- **Share-token actions:** **mint_share_token**,
  **revoke_share_token**.

Every issuing, revocation and deletion of credentials (both enrollment tokens
and personal access tokens) is recorded in the Audit log.
**consume_enrollment_token** is the exception: it is recorded by the worker
itself when enrolling with the token, not by an operator from the dashboard, so
that row has no Actor.

Minting and revoking a status page share link is also recorded, with the
affected token as Target: these are the **mint_share_token** and
**revoke_share_token** actions.

If there are no records, the table shows **No admin actions recorded yet.**

---

## Keyboard Shortcuts

The dashboard responds to these shortcuts. Press `?` to open the **Shortcuts**
panel at any time (or use the **?** button at the bottom right).

| Key | Action |
|-----|--------|
| `/` | Move focus to the search (**Focus search**) |
| `n` | Open the new task window (**New task**) |
| `g` `w` | Go to workers (**Go to workers**) |
| `g` `t` | Go to tasks (**Go to tasks**) |
| `?` | Show or hide the shortcuts panel (**Toggle this panel**) |
| `Esc` | Close the panel or the window (**Close panel / modal**) |

---

<a id="operacion"></a>
<a id="operations"></a>
## Operations

This section is for operators running an Orcaster server once it is
installed: how to update it, keep the service
running, back up what cannot be regenerated, free space when it fills up
and detect problems. The initial installation is already documented in
[Getting Started](#getting-started) (server) and in
[Connecting a Worker](#connecting-a-worker) (workers); here it is assumed
that the server is already deployed and running.

On a VPS provisioned with `setup-scripts/debian13`, this is what lives where:

| What it is | Path on the VPS |
|--------|-----------------|
| Server code and virtual environment | `/opt/orcaster` |
| Root checkout, source of the installer (owned by root, no group or other write access) | `/root/orcaster` |
| Server configuration (`DATABASE_URL`, `WEBAUTHN_*`, etc.) | `server/.env` (inside `/opt/orcaster`) |
| Private task-signing key | `secrets/ed25519.pem` (inside `/opt/orcaster`) |
| Deployment configuration (domain, database credentials) | `setup-scripts/debian13/setup.env` and its backups `setup.env.<suffix>` (inside the root checkout, owned by root, mode `600`; never in `/opt/orcaster`) |
| Artifacts uploaded by the workers | `/var/lib/orcaster-artifacts` |
| Service unit | `/etc/systemd/system/orcaster.service` |
| Reverse proxy and TLS | `/etc/caddy/Caddyfile` |

<a id="updating-the-server-re-deploy"></a>
### Updating the server (re-deploy)

To bring a new version of the code to the VPS (without provisioning again
from scratch):

1. Copy the updated code to the root checkout on the VPS with `rsync`,
   keeping exactly these exclusions (if the checkout is a git clone, you can
   update it with `git pull` as root instead), and then leave the tree owned
   by root with no group or other write access:

   ```bash
   rsync -a \
       --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
       --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '*.png' \
       --exclude 'server/.env' \
       --include 'setup-scripts/debian13/setup.env.example' \
       --exclude 'setup-scripts/debian13/setup.env*' \
       --exclude '*.env' --exclude 'secrets' \
       ./ root@vps:/root/orcaster/
   ssh root@vps 'chown -R root:root /root/orcaster && chmod -R go-w /root/orcaster'
   ```

   > **Never** sync `server/.env`, `setup-scripts/debian13/setup.env` (or
   > its backups `setup.env.<suffix>`), any `*.env` or `secrets/`: they are
   > the configuration and keys *of the VPS* (database password, signing
   > key, domain). The other `<name>.env` files are token files for
   > processes outside the server, which live at the destination next to the
   > code of their tool; `'*.env'`, without a slash, covers them by name at
   > any depth.
   > Overwriting them with values from another environment breaks the server
   > startup and passkey access.

2. On the VPS, as root (`/root` is `0700`), reinstall the application
   **from the checkout**; running the installer from `/opt/orcaster`
   aborts:

   ```bash
   cd /root/orcaster/setup-scripts/debian13
   sudo bash 03-app-install.sh
   ```

   This step copies the checkout to `/opt/orcaster` (root prepares
   it in a private directory and the service user syncs it with `rsync
   --delete`), syncs dependencies (`uv sync --frozen`; if `server/uv.lock`
   is missing it stops without installing anything) and rewrites
   `server/.env`; it **never** regenerates the signing key if one already
   exists.
   If `server/.env` already defines `TASK_SECRETS_KEYS_PATH` and `setup.env`
   does not declare it, or declares it with a different value, the script
   stops without modifying `server/.env` (see [Configuring the task secret
   keys](#configuring-the-task-secret-keys)).

3. Restart the service:

   ```bash
   sudo systemctl restart orcaster.service
   ```

4. Check that it responds (see also [Managing the
   service](#managing-the-service)):

   ```bash
   cd /root/orcaster/setup-scripts/debian13
   bash 08-smoke-test.sh
   ```

Only run `05-systemd.sh` again if you changed a variable that affects the
systemd unit (for example `SERVER_START_TIMEOUT` or `UVICORN_WORKERS`): the
incremental re-deploy above does not rewrite the unit file.

The server applies database migrations automatically at startup
(`MIGRATE_ON_STARTUP=true`); if you want to confirm it beforehand in an
explicit and noisy way (it fails early if the database or the credentials
are wrong), run this before the restart:

```bash
cd /root/orcaster/setup-scripts/debian13
sudo bash 04-migrate.sh
```

> **Migrations and installer keys.** With `MIGRATE_ON_STARTUP=true` the
> migrations run by themselves at startup, before the server accepts
> traffic; if you disabled that option, run `04-migrate.sh` before the
> restart. Before reinstalling with
> `03-app-install.sh`, check that `setup.env` declares the same
> `TASK_SECRETS_KEYS_PATH` as `server/.env`: otherwise the installer stops
> without modifying `server/.env` (see [Configuring the task secret
> keys](#configuring-the-task-secret-keys)).

<details>
<summary>Technical Information</summary>

- There is no `Makefile` target for a remote re-deploy. The only bridge in
  the `Makefile` is `make setup-server debian13`, which runs the full
  provisioner again (`run-all.sh`, scripts `00` to `08`) from the root
  checkout; the incremental code re-deploy consists of running
  `03-app-install.sh` by hand, as above.
- `MIGRATE_ON_STARTUP=true` is suitable for a single server instance.
  If you ever run more than one server instance against the same database,
  it is recommended to migrate out of band (an explicit pipeline step)
  instead of letting each instance migrate at startup.

</details>

<a id="managing-the-service"></a>
### Managing the service

The server runs as the systemd service `orcaster.service`.

```bash
sudo systemctl status orcaster.service
sudo systemctl restart orcaster.service
journalctl -u orcaster.service -f                  # live logs
journalctl -u orcaster.service -n 30 --no-pager    # last 30 lines
```

At startup, the service can take a while to be ready to serve real
traffic: `systemctl` marks it **active** before it finishes applying the
database migrations. If you need to confirm that it is ready, use
`GET /readyz` instead of relying on the systemd state (see [Monitoring the
system](#monitoring-the-system)).

**Caddy** acts as the reverse proxy and handles the issuance and automatic
renewal of the TLS certificate (Let's Encrypt); it needs no manual
intervention as long as the domain keeps resolving to the VPS. The
configuration file is `/etc/caddy/Caddyfile`.

Neither Caddy nor uvicorn writes an access log in the default
`setup-scripts/debian13` setup (the
Caddyfile has no `log` directive and uvicorn runs with `--no-access-log`).
If you add a proxy, CDN or WAF in front of the server (Cloudflare, a load
balancer, another reverse proxy), or enable the Caddy log, **do not log the
paths of `/api/v1/secrets/`**: the name of a secret travels in the URL and a
token pasted there by mistake would end up in that log.

The **firewall** (`ufw`) allows only ports 22 (SSH), 80 (HTTP) and 443
(HTTPS), and denies all other incoming traffic by default.

To check manually that the server responds:

```bash
cd /root/orcaster/setup-scripts/debian13
bash 08-smoke-test.sh
```

<details>
<summary>Technical Information</summary>

- Unit generated by `05-systemd.sh`: user/group `${APP_USER}` (no login),
  `WorkingDirectory` in `server/` (that is where it reads `server/.env`),
  `ExecStart` runs `uvicorn orcaster_server.main:app` with `Type=exec`,
  `Restart=on-failure`, `RestartSec=3` and
  `TimeoutStartSec=${SERVER_START_TIMEOUT}` (120 s by default). It depends on
  `network-online.target` and `postgresql.service`.
- Unit hardening: `NoNewPrivileges=true`, `PrivateTmp=true`,
  `ProtectSystem=strict`, `ProtectHome=true`, `ProtectKernelTunables=true`,
  `ProtectControlGroups=true`, `RestrictSUIDSGID=true`. Since
  `/var/lib/orcaster-artifacts` lies outside `/opt/orcaster` and
  `ProtectSystem=strict` denies writes outside declared paths,
  `ReadWritePaths` explicitly includes both directories.
- The smoke test (`08-smoke-test.sh`) needs no root and changes nothing: it
  retries `GET http://127.0.0.1:8000/healthz` up to 30 times (2 s wait
  between attempts) because a startup with heavy migrations can take a while;
  then it confirms `systemctl is-active`; finally it tries
  `GET https://<DOMAIN>/healthz` on a best-effort basis (if it fails, it only
  warns: it may be DNS that has not propagated or the certificate still being
  issued).
- Caddy is installed from its official repository and validates the
  configuration (`caddy validate`) before reloading.

</details>

<a id="request-size-limit"></a>
### Request size limit

The server rejects any request whose body exceeds a limit, so that a huge
request cannot exhaust its memory. The rejected request receives `413` (too
large), or `400` if it declares a size that is not a number. The default
limits:

| What is sent | Default limit |
|--------------|------------------|
| Any request (dashboard forms, JSON API, sign-in, worker enrollment) | 4 MiB |
| The output of a task that a worker sends | 32 MiB |
| The artifacts that a worker uploads | 208 MiB: the larger of the per-run total (200 MiB) and the per-file size, plus 8 MiB of margin |

The server and Caddy apply the same numbers, so the limit holds even if the
server is reached without going through Caddy. If a worker or a user receives
`413`, compare the size of what they send with this table before raising a
limit. A task whose **Environment variables** list exceeds its maximum (256
names) receives `422`, not `413` (see [Isolation (env /
egress)](#isolation-env--egress)).

To change a limit, define it in `setup.env` (in bytes, positive integers) and
apply the installation and Caddy again:

```bash
cd /root/orcaster/setup-scripts/debian13
$EDITOR setup.env        # MAX_REQUEST_BODY_BYTES, MAX_OUTPUT_BODY_BYTES, ...
sudo bash 03-app-install.sh && sudo bash 06-caddy.sh
sudo systemctl restart orcaster.service
```

<details>
<summary>Technical Information</summary>

- Variables (all in bytes): `MAX_REQUEST_BODY_BYTES` (4 MiB),
  `MAX_OUTPUT_BODY_BYTES` (32 MiB), `ARTIFACT_UPLOAD_OVERHEAD_BYTES` (8 MiB)
  and, for artifact uploads, the larger of `ARTIFACT_MAX_TOTAL_BYTES` and
  `ARTIFACT_MAX_FILE_BYTES` plus that overhead. The first three and
  `ARTIFACT_MAX_TOTAL_BYTES` are optional in `setup.env`: if you do not
  declare them, the server default applies.
- `03-app-install.sh` writes to `server/.env` (what the server reads) the ones
  that `setup.env` declares; `06-caddy.sh` takes each value from
  `server/.env`, then from `setup.env` and, if neither declares it, from the
  default. After a manual adjustment in `server/.env`, `06-caddy.sh` is
  enough.
- A value that is not a positive integer made only of digits (for example
  `4MB`, which Caddy would read as 4,000,000 bytes and not as 4 MiB) aborts
  the script without touching the current Caddyfile; the script validates the
  Caddyfile in a temporary file before replacing it. The server also does not
  start, with an explicit message, if a limit (including the upload overhead)
  is less than 1 or if the artifact limit does not come out positive.
- Caddy has a per-route limit, not a global one (Caddy limits stack and the
  lowest one would also win on the upload routes).

</details>

<a id="configuring-the-task-secret-keys"></a>
### Configuring the task secret keys

[Task secrets](#task-secrets) are stored encrypted with keys that the
server reads from a file. While that file is not configured, the server
works as usual, but the **Secrets** page shows a warning, the API and
`orcaster secret` answer that the feature is disabled when you set secrets
(deleting them keeps working) and tasks keep taking their values from each
worker's environment. The exception is a task that declares a name with an
already stored secret (for example, after the file was removed): the server
does not hand it out and leaves it queued, instead of running without the
secret, until the file is configured and the server is restarted. The
startup records this in the log with an `ERROR` (*N task secret(s) are
stored but TASK_SECRETS_KEYS_PATH is not configured*); the server starts
anyway. See [A task stays queued because the server cannot decrypt a
secret](#task-queued-because-a-secret-cannot-be-decrypted).

The file has one key per line. The first one is the **primary**: it encrypts
new values. All the others still serve to decrypt, which allows rotating
without interrupting the service. Empty lines and lines that start with `#`
are ignored.

> **Back up the key file outside the VPS, encrypted.** If all the keys are
> lost, the stored secrets cannot be recovered and you must set them again
> one by one. The procedure, with `age` and a public key, is in [Backing up
> the key file](#backing-up-the-key-file).

The commands in this section run the script as the service user, with the
same `HOME` and the same `uv` cache that the `setup-scripts/debian13` scripts
use.

**Option 1: file with restricted permissions.** This is the simplest way.

1. Generate a key and save it in a file readable only by the service user,
   inside `secrets/`, which the deploy does not overwrite. The file is created
   with `0600` permissions by the service user itself, and the key does not
   pass through the terminal:

   ```bash
   cd /
   sudo -u orcaster env HOME=/opt/orcaster \
       UV_CACHE_DIR=/opt/orcaster/.uv-cache \
       sh -c 'set -C; umask 077; exec /usr/local/bin/uv run \
           --directory /opt/orcaster/server \
           python scripts/task_secrets_keys.py generate \
           > /opt/orcaster/secrets/task-secrets-keys'
   sudo stat -c '%a %U' /opt/orcaster/secrets/task-secrets-keys
   ```

   - `umask 077` makes the redirection create the file with `0600`
     permissions, with no window in which it is readable by others.
   - `set -C` makes the shell refuse to overwrite an existing key file, which
     would lose the current keys.
   - `/usr/local/bin/uv` is the path where `setup-scripts/debian13` installs
     `uv`.
   - `stat` must answer `600 orcaster`. Do not print the file with `cat`.
   - If `uv` fails, the redirection has already left an empty file. Delete it
     before retrying, because `set -C` will prevent reusing it, and do not
     leave the path declared with an empty file: the server would not start.
     This command deletes the file only if it is empty:

     ```bash
     sudo find /opt/orcaster/secrets/task-secrets-keys \
         -maxdepth 0 -empty -delete
     ```

2. Declare the path in `server/.env`:

   ```bash
   TASK_SECRETS_KEYS_PATH=/opt/orcaster/secrets/task-secrets-keys
   ```

3. Restart the service and confirm in the log the line
   `lifespan: task secrets keys loaded; central task secrets enabled (primary
   key id=<ID>, key ids=<ID>)`. The ids are non-secret identifiers, 12
   hexadecimal characters long, that do not reveal the key; they serve to
   compare the file with what the server loaded (see [Rotate the
   key](#rotate-the-key)).

**Option 2: encrypted systemd credential.** systemd decrypts the file when
the service starts and leaves it in a private directory of the unit, whose
path it exports in `$CREDENTIALS_DIRECTORY`. On disk the key remains
encrypted as a credential, not in clear, but that encryption depends on a
host key:

- Without a TPM2, `systemd-creds encrypt` encrypts with the host key,
  `/var/lib/systemd/credential.secret`, which is on the same disk and can be
  read only by root. The credential is tied to that host: it protects
  against copying the encrypted file on its own, but not against someone with
  root nor against someone who obtains the whole disk or a backup that
  includes that key.
- With a TPM2, the key is derived from the chip and is not stored on the host.
  The default mode (`--with-key=auto`) uses the TPM2 if it finds one and also
  the host key if `/var/lib/systemd/` is on persistent disk, and requires
  both to decrypt.
- The encrypted credential cannot be decrypted on another host, so it is not
  by itself a backup outside the VPS. For that, follow [Backing up the key
  file](#backing-up-the-key-file).

1. Encrypt the key as a credential, without writing it in clear on disk:

   ```bash
   cd /
   sudo install -d -m 700 /etc/credstore.encrypted
   sudo -u orcaster env HOME=/opt/orcaster \
       UV_CACHE_DIR=/opt/orcaster/.uv-cache \
       uv run --directory /opt/orcaster/server \
       python scripts/task_secrets_keys.py generate \
     | sudo systemd-creds encrypt --name=task-secrets-keys - \
         /etc/credstore.encrypted/task-secrets-keys
   ```

2. Create a unit extension, for example
   `/etc/systemd/system/orcaster.service.d/task-secrets.conf`:

   ```ini
   [Service]
   LoadCredentialEncrypted=task-secrets-keys:/etc/credstore.encrypted/task-secrets-keys
   ```

3. Declare the path in `server/.env`, with `$CREDENTIALS_DIRECTORY` **without
   braces**:

   ```bash
   TASK_SECRETS_KEYS_PATH=$CREDENTIALS_DIRECTORY/task-secrets-keys
   ```

4. Apply and restart: `sudo systemctl daemon-reload && sudo systemctl
   restart orcaster.service`.

> On a VPS provisioned with `setup-scripts/debian13`, `server/.env` is
> regenerated on every run of `03-app-install.sh`. For the path to survive,
> declare `TASK_SECRETS_KEYS_PATH` in `setup.env` (with single quotes if it
> contains `$CREDENTIALS_DIRECTORY`) and the script copies it to the `.env`.
> If `server/.env` already defines the path (with a non-empty value, with or
> without `export` and with spaces around `=`) and `setup.env` does not
> declare it, the script **stops without modifying `server/.env`**
> with a message whose first line is `<path>/server/.env sets
> TASK_SECRETS_KEYS_PATH=<value>, but <path>/setup.env does not declare it.`,
> names both files, says that `server/.env` was not modified and asks you to
> declare it in `setup.env`: regenerating the `.env`
> without the path would disable secrets and the tasks that declare them
> would stop being handed out. Declare the same value and run the script
> again. If both files define it with different values, the script also
> **stops without modifying `server/.env`**, with a message whose first line
> is `<path>/server/.env sets TASK_SECRETS_KEYS_PATH=<value>, but <path>/setup.env sets a different value: TASK_SECRETS_KEYS_PATH=<value>.`:
> regenerating `server/.env` would make the server load another key file on
> its next restart, and the stored secrets, encrypted with the keys of the
> current file, might no longer decrypt. To keep the current file, copy the
> value of `server/.env` to `setup.env`. To change files on purpose, first
> check that the new file contains the keys that encrypted the stored secrets
> and set the same new value in `setup.env` and in `server/.env`; then run
> the script again. With equal values (quotes and spaces at the edges do not
> count, and `'$CREDENTIALS_DIRECTORY/task-secrets-keys'` in `setup.env` is
> equivalent to `$CREDENTIALS_DIRECTORY/task-secrets-keys` in `server/.env`)
> the script continues. The unit extension, in contrast, lives in its own
> file and is not overwritten.

If the file is configured but cannot be read, has no keys or a line is not a
valid key, the server **does not start** and the log names the path and, if
applicable, the line number (never the key). This is deliberate: a server that
started without being able to decrypt would hand out tasks without their
secrets.

#### Rotate the key

1. Generate a new key with `generate` and add it as the **first line** of the
   file, keeping the previous ones below.
2. Restart the service. From then on, every new value is encrypted with the
   new key. Confirm that the server loaded the new key as the primary one with
   the last startup line:

   ```bash
   sudo journalctl -u orcaster.service | grep "task secrets keys loaded" | tail -n 1
   ```

   The line ends in `(primary key id=<ID>, key ids=<ID>,<ID2>,...)`. The
   `<ID>` of the primary must be that of the new key. The ids are non-secret
   identifiers of 12 hexadecimal characters: they do not reveal the key and
   serve to compare the file with what the server loaded.
3. Re-encrypt the existing secrets and the stored URLs of the active share
   links with the primary key:

   ```bash
   cd /
   sudo -u orcaster env HOME=/opt/orcaster \
       UV_CACHE_DIR=/opt/orcaster/.uv-cache \
       uv run --directory /opt/orcaster/server \
       python scripts/task_secrets_keys.py rotate
   ```

   Before rotating, the command prints `keys file primary key id: <ID>
   (<N> key(s): <ID>,<ID2>,...)`: the id of the primary key of the file it is
   going to use and those of all its keys. With option 1, that file is the
   same one the server reads. Then it prints how many secrets and how many
   share link URLs it re-encrypted (*rotated N task secret(s) and M share
   link URL(s) to the primary key*) and recomputes the fingerprints of the
   secrets, which depend on the primary key: after rotating, all the
   fingerprints change even though the values are the same. Revoked share
   links and those with no stored URL are not touched. If any secret or URL
   cannot be decrypted with any key in the file, or a URL cannot be
   re-encrypted, nothing changes and it names the affected ones as
   `<workspace>/<name>` (with ` (worker <id>)` at the end if the secret
   belongs to a single worker) or `<workspace>/share-link:<id>`, in two
   groups:
   - *no key in TASK_SECRETS_KEYS_PATH decrypts*: no key in the file
     decrypts them. For a share link, restore the old key or revoke that
     link.
   - *share link URLs that no key can fix*: the stored URL does not match its
     link, is not valid text, was encrypted with other keys or is damaged;
     the message states the cause of each one. Restoring a key does not fix
     it: revoke that link.

   Then run `rotate` again.
4. Remove the old key from the file and restart again.

Respect the order: restart with the new key before `rotate`. With option 1,
if you run `rotate` before restarting, the server rereads the key file,
once, when it detects that it changed, in the poll that is about to refuse a
delivery because it does not decrypt or in those of a worker with that
refusal just remembered, and keeps delivering (it logs
`task_secrets.keys_reloaded: TASK_SECRETS_KEYS_PATH changed on disk; keyring
reloaded; primary key id=<ID>, key ids=…`); if the file changed but is not
valid, it keeps the keys already loaded and logs
`task_secrets.keys_reload_failed`. With option 2 no reload is possible: the
service only sees the new credential when it restarts, and between `rotate`
and the restart the tasks that declare stored secrets stay queued.

That reload in the poll happens when the server is about to refuse a
delivery because it does not decrypt and in the polls of a worker with that
refusal remembered. The other uses of the keys on a running server (setting
a secret and the stored URLs of share links) check the file before using the
keys and reread it if it changed, so they use the file as it is on disk: with
option 1 there is no need to wait for a poll for a set secret to be encrypted
with the new key, and that holds with any number of processes
(`UVICORN_WORKERS`). Even so, the order above remains the supported
procedure, because of two cases that the reload does not cover: if the file
changed but is not valid, the server keeps the keys already loaded, so a
secret set in that interval is encrypted with the previous primary and you
must run `rotate` again before removing that key (step 4); and with option 2
no reload is possible.

The file changes in steps 1 and 4: repeat the [key file
backup](#backing-up-the-key-file) after each change. Do the one for step 1
before restarting, because from that restart on the new values are encrypted
with a key that the previous backup does not have.

With option 2, the content is edited by decrypting and re-encrypting the
credential, without leaving it in clear on disk. For step 1 (new key first,
the previous ones below):

```bash
cd /
{ sudo -u orcaster env HOME=/opt/orcaster \
      UV_CACHE_DIR=/opt/orcaster/.uv-cache \
      uv run --directory /opt/orcaster/server \
      python scripts/task_secrets_keys.py generate
  sudo systemd-creds decrypt --name=task-secrets-keys \
      /etc/credstore.encrypted/task-secrets-keys -; } \
  | sudo systemd-creds encrypt --name=task-secrets-keys - \
      /etc/credstore.encrypted/task-secrets-keys.new
sudo mv /etc/credstore.encrypted/task-secrets-keys.new \
    /etc/credstore.encrypted/task-secrets-keys
```

For step 4, the same pattern keeping only the first line
(`decrypt … - | head -n 1 | encrypt …`). Since `$CREDENTIALS_DIRECTORY` only
exists inside the service, `rotate` (step 3) run from a shell fails with
*TASK_SECRETS_KEYS_PATH references $CREDENTIALS_DIRECTORY, which is not
set*. Decrypt the credential to a temporary file in `/run` (tmpfs, does not
touch the disk) readable only by `orcaster`, pass its path with
`--keys-file`, which replaces `TASK_SECRETS_KEYS_PATH` for that run, and
delete it when finished.

With `--keys-file`, `rotate` checks before touching the database that the
server has the primary key of that copy, because rotating with a primary that
the server did not load leaves all the secrets unreadable to it. Since here
the script cannot read the service credential (the path in `server/.env` uses
`$CREDENTIALS_DIRECTORY`), it **refuses to rotate, changing nothing, unless
you pass `--confirm-server-key-id <ID>`**, with the id of the primary of the
copy. The `<ID>` must come from the startup line **of the server** (step 2:
`primary key id=<ID>`), not from the output or the refusal message of the
script. The script prints the id of the file you passed it, and that proves
nothing about what the server loaded: copying it from there defeats the
check. Taken from the server log, the check detects a server that was not
restarted with the new key or a wrong copy of the credential. If the id is
not that of the primary of `--keys-file`, the script refuses and says so.
Without the flag, it prints the id of the primary of the copy and what to
compare with the server log.

```bash
cd /
sudo journalctl -u orcaster.service | grep "task secrets keys loaded" | tail -n 1
# Note the value of "primary key id=" in that line: it is the <ID> below.
sudo install -m 600 -o orcaster -g orcaster /dev/null /run/orcaster-secrets-keys
sudo systemd-creds decrypt --name=task-secrets-keys \
    /etc/credstore.encrypted/task-secrets-keys /run/orcaster-secrets-keys
sudo chown orcaster:orcaster /run/orcaster-secrets-keys
sudo chmod 600 /run/orcaster-secrets-keys
sudo -u orcaster env HOME=/opt/orcaster \
    UV_CACHE_DIR=/opt/orcaster/.uv-cache \
    uv run --directory /opt/orcaster/server \
    python scripts/task_secrets_keys.py rotate \
    --keys-file /run/orcaster-secrets-keys \
    --confirm-server-key-id <ID>
sudo rm -f /run/orcaster-secrets-keys
```

The `chown` and `chmod` after decrypting reassert owner and permissions: the
`systemd-creds` page does not specify what mode it writes the output file
with. If `rotate` fails, run the `rm -f` anyway. If the server can read its
file from the shell (for example, the path in `server/.env` does not use
`$CREDENTIALS_DIRECTORY`), the script compares it by itself: the primary of
`--keys-file` must be among the keys of that file and, if it is not, it
refuses even if `--confirm-server-key-id` is passed. Without `--keys-file`,
the flag is ignored and there is no check: the script reads the same file as
the server.

Another way, without a temporary file, is to run `rotate` in a transient unit
that loads the credential like the service, with the path that systemd
exports inside it:

```bash
sudo systemd-run --wait --pipe --collect \
    -p User=orcaster \
    -p LoadCredentialEncrypted=task-secrets-keys:/etc/credstore.encrypted/task-secrets-keys \
    -p Environment=HOME=/opt/orcaster \
    -p Environment=UV_CACHE_DIR=/opt/orcaster/.uv-cache \
    -p WorkingDirectory=/opt/orcaster/server \
    /bin/sh -c 'exec uv run python scripts/task_secrets_keys.py rotate \
        --keys-file "$CREDENTIALS_DIRECTORY/task-secrets-keys"'
```

In this form the path in `server/.env` does resolve inside the transient
unit, so the script compares the primary of `--keys-file` with the credential
that unit loads and does not need `--confirm-server-key-id`. That credential
is the one on disk, not the one the server loaded: the check does not detect
a server that was not restarted with the new key. Do the check of step 2
anyway (id of the primary in the startup line) before running it.

> **Not verified on a real host.** The two `rotate` procedures with option 2
> follow the `systemd-creds` and `systemd-run` documentation, but have not
> yet been run on a server. The `systemd-run` page does not list which
> properties a transient unit accepts, so `LoadCredentialEncrypted=` with
> `-p` is not confirmed either, and the transient unit must find `uv` in its
> `PATH`. The `--keys-file` guard has not yet been run with these two forms
> either. Try them first with a test database or keep the key file backup at
> hand.

<a id="backing-up-the-key-file"></a>
#### Backing up the key file

The backup must leave the VPS encrypted and with a key that the VPS does not
have. Use [age](https://github.com/FiloSottile/age) with a public-key
recipient: the server knows only the public key, so it can encrypt the backup
but not decrypt it, and the private key never travels to it.

1. On the operator's workstation, **never on the server**, generate the
   identity and get the recipient:

   ```bash
   install -d -m 700 ~/.config/age
   age-keygen -o ~/.config/age/orcaster.txt
   age-keygen -y ~/.config/age/orcaster.txt
   ```

   The first command creates the identity (private key) with `0600`
   permissions and refuses to overwrite an existing one. Keep it out of
   synced or versioned folders, and store a separate copy, for example in a
   secrets manager: without it the backup cannot be decrypted. The second
   command prints the recipient, a line that starts with `age1`. Only that
   public line travels to the server.

2. On the server, install `age` and encrypt the key file, as `orcaster`, with
   the recipient (`<RECIPIENT>` is the `age1…` line from the previous step):

   ```bash
   sudo apt install age
   sudo -u orcaster sh -c 'set -C; umask 077; age -r <RECIPIENT> \
       /opt/orcaster/secrets/task-secrets-keys \
       > /opt/orcaster/secrets/task-secrets-keys.$(date -u +%Y%m%dT%H%M%SZ).age'
   ```

   The name carries a UTC timestamp and `set -C` prevents overwriting a
   previous backup. With option 2 the file does not exist in clear: encrypt
   the output of `systemd-creds decrypt` directly, which travels through a
   pipe and is not written to disk (the result is owned by `root`):

   ```bash
   sudo sh -c 'set -C; umask 077; systemd-creds decrypt --name=task-secrets-keys \
       /etc/credstore.encrypted/task-secrets-keys - \
     | age -r <RECIPIENT> \
       > /opt/orcaster/secrets/task-secrets-keys.$(date -u +%Y%m%dT%H%M%SZ).age'
   ```

   This variant follows the `systemd-creds` and `age` documentation, and has
   not yet been run on a server.

3. Copy **only the `.age` file** off the host, never the key file in clear.
   `<STAMP>` is the timestamp of the name created in step 2 (`sudo ls
   /opt/orcaster/secrets` shows it) and `<VPS>` is the SSH
   destination:

   ```bash
   ( umask 077; ssh <VPS> sudo cat \
       /opt/orcaster/secrets/task-secrets-keys.<STAMP>.age \
       > ~/Backups/task-secrets-keys.<STAMP>.age )
   ```

4. Verify the backup on the workstation without writing the clear text:
   compare the hash of what `age` decrypts with that of the file on the host.
   The two hashes must match.

   ```bash
   age -d -i ~/.config/age/orcaster.txt \
       ~/Backups/task-secrets-keys.<STAMP>.age | sha256sum
   ssh <VPS> sudo sha256sum /opt/orcaster/secrets/task-secrets-keys
   ```

   On macOS, `shasum -a 256` replaces `sha256sum`. With option 2, the
   host-side hash is obtained with `sudo systemd-creds decrypt
   --name=task-secrets-keys /etc/credstore.encrypted/task-secrets-keys - |
   sha256sum`.

5. Repeat the procedure every time the file changes, that is, after each key
   rotation (see [Rotate the key](#rotate-the-key)).

<details>
<summary>Technical Information</summary>

- Variable: `TASK_SECRETS_KEYS_PATH` (`server/orcaster_server/config.py`). If undefined
  or empty, the server starts with a single `WARNING`
  (`TASK_SECRETS_KEYS_PATH not configured`) and, if there are stored secrets,
  with an `ERROR` (`lifespan: N task secret(s) are stored but
  TASK_SECRETS_KEYS_PATH is not configured: tasks that declare them stay
  queued and are not delivered until the keys file is configured and the
  server restarted`). Setting a secret answers `503`, deleting keeps working,
  the listing remains available (with `keys_configured: false` in the API)
  and the poll delivers `secrets: {}`. If a task declares a name with a
  stored secret, the server does not deliver it: it stays `queued` and an
  `ERROR` `task_secrets.keys_missing` is logged.
- Key ids: each key has a non-secret id (the first 12 hexadecimal characters
  of an HKDF-SHA256 derivation of the key with its own label; see
  [HKDF](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf)).
  The server logs them at startup and when it reloads the file.
  `rotate --keys-file PATH` checks, before opening the database, that the
  server has the primary of PATH: if it reads `TASK_SECRETS_KEYS_PATH` from
  the shell, it requires that primary to be among its keys; if it does not
  read it (undefined or with `$CREDENTIALS_DIRECTORY`), it requires
  `--confirm-server-key-id <id>` equal to the id of the primary of PATH. Every
  refusal says `nothing was rotated`.
- The path expands environment variables (`$VAR` and `${VAR}`). A referenced
  variable that is not defined is an error that names it. In `server/.env`
  the form without braces is preferable: the `.env` reader replaces `${VAR}`
  with its value and, if it is not defined, with an empty string, so the
  error would end up being a nonexistent file.
- The keys are Fernet (`cryptography.fernet.MultiFernet`): it encrypts with
  the first one and tries all of them when decrypting. `rotate` uses
  `MultiFernet.rotate` on all the rows of `task_secrets` and on the
  `token_ciphertext` of the active share links, with keys derived by HKDF and
  separate from those of the secrets, in a single transaction, first verifies
  that each token matches its `token_hash` and recomputes the fingerprint of
  the secrets (HMAC-SHA256 with a key derived by HKDF from the primary); it
  needs `DATABASE_URL` and `TASK_SECRETS_KEYS_PATH`, or `--keys-file PATH`,
  which replaces the variable for that run, and always prints beforehand
  `keys file primary key id: <id> (<n> key(s): <id1>,<id2>,...)`.
- `LoadCredentialEncrypted=` and `$CREDENTIALS_DIRECTORY`: see
  [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html),
  [systemd-creds](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html)
  and [systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html).
  `systemd-creds encrypt` stores the credential name inside the encrypted
  file; use the same name in `--name=` and in the identifier of
  `LoadCredentialEncrypted=`.
- `systemd-creds` host key: the manual page describes the `host` mode as "A
  secret key stored in the `/var/lib/systemd/credential.secret` file which is
  only accessible to the root user" and clarifies that, since it is on the
  host file system, "decryption is possible with access to the host file
  system and sufficient privileges". With a TPM2, "decryption is only
  possible with access to the original TPM2 chip" (see
  [systemd-creds](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html)).
- Backup with `age`: `-r` indicates a recipient, `-i` an identity and `-d`
  decrypts; `age-keygen -o` writes the identity and `age-keygen -y` prints
  the recipient of an identity (see the
  [age README](https://github.com/FiloSottile/age)).

</details>

<a id="backing-up-unrecoverable-state"></a>
### Backing up unrecoverable state

These server assets cannot be regenerated if they are lost. Before any other
operational task, know which they are and what happens if they are missing:

| Asset | Default path (`setup-scripts/debian13`) | If it is lost |
|--------|---------------------|---------------|
| PostgreSQL database (`orcaster`) | local `postgresql` service | All tasks, workers, workspaces, credentials and the audit history are lost. |
| Private signing key (Ed25519) | `secrets/ed25519.pem` | The server **does not start** without it. If a new one is generated, all deployed workers keep the old public key and **reject every task** until the new public key is distributed to them. |
| Deployment configuration | `setup-scripts/debian13/setup.env` (in the root checkout, `/root/orcaster`) | The database password and the configured domain are lost; it has to be rebuilt by hand. |
| Task secret key file, if configured | the one that `TASK_SECRETS_KEYS_PATH` indicates, for example `/opt/orcaster/secrets/task-secrets-keys` (with option 2, the encrypted credential can only be decrypted on that host and is not useful as a backup) | The stored task secrets cannot be decrypted: you must set them all again (see [Configuring the task secret keys](#configuring-the-task-secret-keys)). |
| Worker artifacts | `/var/lib/orcaster-artifacts` | The files produced by tasks that have not yet been backed up to external storage are lost (see [Freeing storage space](#freeing-storage-space)). |

> **The key `secrets/ed25519.pem` is the most critical asset.** Losing it
> invalidates signature verification on all the workers already deployed and
> the server does not start without it (it fails fast, instead of starting
> without signing). Back it up together with `setup.env` as soon as the
> initial installation finishes (the same warning appears in the section
> [Starting the Orchestrator Server](#starting-the-orchestrator-server)).

**The repository does not include backup automation.** There is no script,
cron job or `Makefile` target that runs `pg_dump`, `pg_restore` or copies
`secrets/` or `setup.env`. What follows is the procedure **recommended by
this manual**, not a tool of the repository:

1. Back up the database with `pg_dump`, outside the VPS itself:

   ```bash
   PGPASSWORD="$DB_PASSWORD" pg_dump -h 127.0.0.1 -U orcaster -d orcaster \
       -Fc -f orcaster-$(date -u +%Y%m%dT%H%M%SZ).dump
   ```

2. Copy `secrets/ed25519.pem` (from `/opt/orcaster`) and
   `setup-scripts/debian13/setup.env` (from the root checkout) to a safe
   place outside the VPS (for example, a secrets manager or a separate
   encrypted volume). Both files are deliberately outside version control
   because they contain credentials. A local copy of `setup.env` on the VPS
   goes next to it, in the root checkout, as `setup.env.<suffix>` and mode
   `600`; never in `/opt/orcaster`, which belongs to the service
   user.
3. If task secrets are configured, back up their key file encrypted with
   `age`, following [Backing up the key
   file](#backing-up-the-key-file), and copy only the `.age` file out of the
   VPS.
4. To restore, reverse the process: recreate the empty database, apply the
   `.dump` with `pg_restore`, and put `ed25519.pem` and `setup.env` back in
   their original paths (the latter owned by root and mode `600`) **before**
   starting the service. If you use task secrets, also put back the key file,
   which is decrypted with `age -d` on the workstation and must end up with
   `0600` permissions and owned by `orcaster`.

<details>
<summary>Technical Information</summary>

- The `pg_dump` command above is built by analogy with the connection that
  the provisioning scripts already use (`psql -h 127.0.0.1 -U orcaster -d
  orcaster`), not an instruction documented in the repository.
- PostgreSQL is Orcaster's only infrastructure dependency, so it is the one
  thing to back up; Orcaster does not back it up for you.
- The "Collected" mechanism (see [Downloading the
  Artifacts](#downloading-the-artifacts)) backs up outside the server only
  the indicator artifacts that are already synced; the rest of the contents
  of `/var/lib/orcaster-artifacts` has no redundancy.

</details>

<a id="freeing-storage-space"></a>
### Freeing storage space

Each workspace has an artifact quota: the common server quota or, if the
host set one, a quota of its own for the workspace (see [Artifact quota per
workspace](#artifact-quota-per-workspace)). The **storage** indicator in the
dashboard top bar shows it as `used / quota` (see [Checking Fleet
Status](#checking-fleet-status)). The server stops accepting new artifacts
for two independent reasons, and stops delivering tasks while either of them
remains active:

- **Logical quota exceeded**: the sum of bytes registered for the workspace
  exceeded the configured limit.
- **Physical disk floor**: the real free space of the volume that contains
  `/var/lib/orcaster-artifacts` fell below the configured minimum.

| Indicator | Meaning | What to do |
|-----------|--------------|-----------|
| **⚠ storage full** (dashboard chip) | The workspace exceeded the quota, the disk is low, or both | Free space (see the mechanisms below) or ask the host to raise the workspace quota (see [Artifact quota per workspace](#artifact-quota-per-workspace)) |
| `507 Insufficient Storage` response when uploading an artifact | A task tried to upload an artifact while storage is full | Same as above; the task will have to retry the upload once space is freed |

<a id="artifact-quota-per-workspace"></a>
#### Artifact quota per workspace

By default all workspaces share the same quota value,
`ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES`. The host can set a quota of its own
for a workspace (an *override*), for example to give more space to one
workspace than to another. Only the host changes it, with a script against
the database; the interface shows it but does not edit it, so that a
workspace cannot assign itself more disk.

See all workspaces with their quota, its origin (`override` or `default`)
and their usage, and how much they can still grow compared with the free
space above the disk floor:

```bash
cd server && uv run python scripts/workspace_quota.py
```

Set the quota of a workspace (by its slug):

```bash
cd server && uv run python scripts/workspace_quota.py --workspace example --set 20GiB
```

Return a workspace to the common server quota:

```bash
cd server && uv run python scripts/workspace_quota.py --workspace example --clear
```

`--set` accepts bytes or an integer with a binary suffix (`MiB`, `GiB`,
`TiB`); it rejects decimal units such as `GB`, values that are not greater
than zero and values greater than `9223372036854775807` bytes (the maximum
the database supports). The change takes effect without restarting the
server: the quota is read from the database on every upload and every poll.
After setting or removing a quota, the script prints the full listing and
warns with `WARNING: quota headroom exceeds the free space above the floor`
if the quotas no longer fit on the disk (see below). If the new quota ends up
equal to or below what the workspace already uses, the change is applied
anyway and the script warns with `WARNING: <slug> already uses ...`: the
workspace is paused by quota and its uploads receive `507` until it frees
space or receives a larger quota. Any member of the workspace sees the origin
of the quota in the **Quota source** row of the storage indicator. The change
is not recorded in the interface audit, because it is made from the host.

**Quotas that do not fit on the disk.** The free disk floor
(`ARTIFACT_DISK_MIN_FREE_BYTES`) is a single one for all workspaces, because
they share the same disk: when the free space falls below that floor, task
delivery stops for **all** workspaces, not only the one that filled it. For
the floor to be the last safety net and not the usual limit, what all
workspaces (disabled ones included) can still grow must fit in the free
space of the volume of `/var/lib/orcaster-artifacts` above that floor. What a
workspace can grow is its quota minus what it already uses, or zero if it is
already above its quota: its excess bytes are not space in which another
workspace can grow. For example, with a workspace A of quota 1 GiB that
already uses 5 GiB, an empty workspace B of quota 10 GiB and 8 GiB free above
the floor, B can upload 10 GiB and cross the floor, so the server warns. The
total size of the volume is not counted, because the database, logs or git
clones may live on the same disk. The server checks it every few minutes and
leaves in its log (`journalctl -u orcaster.service`):

| Log line | What it means | What to do |
|-----------------|---------------|-----------|
| `WARNING housekeeping: artifact quotas over-committed: N workspace(s) (M with override) can still grow X bytes (...) but only Y bytes are free above the floor (free F - ARTIFACT_DISK_MIN_FREE_BYTES P); committed quotas C bytes, artifact usage U bytes; ...` | What the workspaces can still grow (X) exceeds the free space above the floor (Y): filling the quotas can push the disk below the floor and stop all workspaces | Reduce quotas with `--set` or `--clear`, or enlarge the disk |
| `INFO housekeeping: artifact quotas fit the disk again: ...` | The quotas fit on the disk again | Nothing |
| `WARNING housekeeping: artifact quota over-commit check skipped: cannot measure the free space of ...` | The server could not measure the disk; there is no verdict | Check that `ARTIFACTS_ROOT` exists and is accessible |

The warning is not repeated on every check: it appears when the server starts
and every time the verdict or any quota changes (not when only the free space
or the usage vary). It is only a warning: it does not change quotas or stop
tasks.

Other mechanisms that free space or limit its growth:

- **Command retention**: the records of the commands table are purged after
  `COMMANDS_RETENTION_DAYS` days (7 by default).
- **Clear cache of a worker**: frees space on the disk of that machine, not
  on the server (see [Clearing the worker cache (Clear
  cache)](#clearing-the-worker-cache-clear-cache)).

<details>
<summary>Technical Information</summary>

- Relevant variables (`server/orcaster_server/config.py`): `ARTIFACTS_ROOT` (default
  `/var/lib/orcaster-artifacts`), `ARTIFACT_MAX_FILE_BYTES` (50 MiB in code;
  `setup.env.example` sets 200 MiB), `ARTIFACT_MAX_TOTAL_BYTES` (200 MiB),
  `ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES` (10 GiB in code; `setup.env.example`
  sets 20 GiB; it is the default quota, which a workspace override replaces),
  `ARTIFACT_DISK_MIN_FREE_BYTES` (2 GiB, shared by all workspaces),
  `ARTIFACT_DISK_FREE_CACHE_TTL_S` (5 s of cache on the free disk
  measurement), `ARTIFACT_USAGE_RECONCILE_PASSES` (10 housekeeping passes,
  about 5 minutes with the default interval, to correct drift of the quota
  counter).
- With the `setup.env.example` values, a single 200 MiB file consumes the
  whole per-run quota, because `ARTIFACT_MAX_TOTAL_BYTES` is also 200 MiB.
- The effective quota of a workspace is the column
  `workspaces.artifact_quota_bytes` (migration `0055`) if it is not `NULL`,
  and otherwise `ARTIFACT_WORKSPACE_MAX_TOTAL_BYTES`. The upload answers `507`
  when the usage plus the batch exceeds the effective quota; the poll pauses
  when the usage reaches it. The over-commit check runs at the same cadence
  as the counter reconcile (`ARTIFACT_USAGE_RECONCILE_PASSES`).
- If both conditions become active at once, the quota ("quota") takes
  priority over the disk ("disk") in the `storage_paused` field of the poll.
- There is no automatic deletion by age for generic artifacts, nor a
  retention/TTL policy for `output_events`: today it grows indefinitely and
  requires manual cleanup.

</details>

<a id="monitoring-the-system"></a>
### Monitoring the system

The server exposes two health endpoints with different purposes:

```bash
curl -fsS http://127.0.0.1:8000/healthz   # always "ok"; does NOT query the database
curl -fsS https://<DOMAIN>/readyz         # does query the database
```

- `GET /healthz` answers `{"status": "ok"}` without checking anything else.
  It is the one the smoke test uses to confirm that the process is alive.
- `GET /readyz` does check the database connection: it answers `503` if it
  cannot connect (for example, a misconfigured `DATABASE_URL` or the database
  being down) and `200 {"status": "ready"}` if everything is in order. Use it
  when you need to confirm that the server can already serve real traffic,
  not just that the process started.

For logs:

```bash
journalctl -u orcaster.service -f
```

In the dashboard itself, these indicators anticipate problems:

- The **storage** chip and the **⚠ storage full** warning (see
  [Checking Fleet Status](#checking-fleet-status) and
  [Freeing storage space](#freeing-storage-space)).
- The **artifacts** indicator (**Produced** / **Uploaded**), when the
  feature is active (see [Checking Fleet
  Status](#checking-fleet-status)).
- The status badge of each worker: **online**, **paused**, **offline**
  (see [Worker states](#worker-states)).

If the server domain goes through Cloudflare, configuring the cache and rate
limiting rules on `/status/*` is a security precondition, not an option:
without them, every anonymous visit to a public status page runs a heavy
query against PostgreSQL. Configure two rules on
`URI Path starts with "/status/"`:

- **Cache Rule**: *Eligible for cache*, with Edge TTL *Use cache-control
  header if present, bypass cache if not* (the origin emits
  `Cache-Control: public, max-age=10` only on the `200` responses of the page
  and the summary; the rest goes out `no-store`). In *Caching →
  Configuration*, set Browser Cache TTL to **Respect Existing Headers**: the
  zone default (4 hours) rewrites the `Cache-Control` the browser receives
  and freezes the page refresh. Do not enable *Ignore query string*: the
  summary changes with `?variant=`.
- **Rate Limiting Rule**: 20 requests every 10 seconds per IP; action
  *Block* for 10 s (or the minimum the plan allows). A legitimate visitor
  makes one request every 10 s.

Revoking a share token cuts access at the origin instantly, but the edge may
keep serving the last cached response for up to 10 seconds.

Signs that something is wrong:

| Sign | What it means | What to do |
|-------|-----------------|-----------|
| A worker goes **offline** | It sent no heartbeat for more than `WORKER_HEARTBEAT_TIMEOUT_SECONDS` (120 s by default) | Check the worker machine; see [Diagnosing and Resolving Worker Problems](#troubleshooting) |
| `storage_paused` non-empty in a worker's poll | The workspace storage is blocking task delivery | See [Freeing storage space](#freeing-storage-space) |
| `WARNING housekeeping: artifact quotas over-committed` in the server log | The sum of the workspace quotas does not fit on the disk | See [Artifact quota per workspace](#artifact-quota-per-workspace) |
| `HOUSEKEEPING_ENABLED=false` in `server/.env` | The process that reclaims expired leases and marks workers offline was disabled | Do not disable it in production except for one-off debugging |

<details>
<summary>Technical Information</summary>

- After a restart, the server gives a margin of `STARTUP_GRACE_SECONDS`
  (150 s by default) during which housekeeping neither marks workers offline
  nor reclaims leases, so as not to penalize healthy workers that have not yet
  managed to send their first heartbeat.
- The in-process cache of the public status page snapshot has a TTL of
  `STATUS_SNAPSHOT_CACHE_TTL_S` (10 s by default; `0` disables it). The
  resolution of a share token never uses this cache: it always queries the
  database, so that a revocation is immediate.
- There is no external active monitoring (alerting, Prometheus/Grafana)
  beyond `/healthz`, `/readyz`, `journald` and the dashboard itself.

</details>

<a id="post-installation-management"></a>
### Post-installation management

Tasks that repeat after the server is already installed and running:

- **Create the first owner**: see [Bootstrapping the First
  Owner](#bootstrapping-the-first-owner). It is not part of the automatic
  provisioner; it is a manual step, done once.
- **Revoke credentials**: both the [worker enrollment
  tokens](#worker-enrollment-tokens) and the [personal access
  tokens](#personal-access-tokens) are revoked manually from the dashboard
  when they are no longer needed or are suspected to have leaked. There is no
  scheduled periodic rotation mechanism for either of them: only revocation
  on demand.
- **Update the operating system packages**: every time the provisioner is run
  again (`run-all.sh` or `00-preflight.sh` directly), `apt-get update &&
  apt-get upgrade` is run as part of that run. There is no independent
  periodic update configured.

<a id="pending-operational-work-known-gaps"></a>
### Pending operational work (known gaps)

Orcaster does not yet automate the following; plan for them in your
deployment:

- **No automated backup** of the database, the signing key, `setup.env` or the
  task secret key file (see [Backing up unrecoverable
  state](#backing-up-unrecoverable-state)).
- **`output_events` with no retention/TTL policy**: it grows indefinitely;
  it requires manual cleanup.
- **No major PostgreSQL upgrade procedure** (for example, from version 17 to a
  later one): it would require `pg_upgrade` or a dump/restore planned
  separately.
- **No external alerting**: the available monitoring is limited to
  `/healthz`, `/readyz`, `journald` and the dashboard indicators; there is no
  integration with an alerting system.

---

## Frequently Asked Questions

### I don't see any worker in the Workers panel

The panel shows **No workers registered.** when no workers are connected.
Connect one (see [How do I connect a worker?](#how-do-i-connect-a-worker)).
It will appear as soon as it sends its first heartbeat.

### A task is stuck in queued and does not advance

**queued** means the task is waiting for a worker to claim it. Check that
there are **online** workers that meet the task's requirements. If you set
**Run at**, the task is not eligible until that time.

### A task shows as completed but I see failed attempts

This is normal after retries: the aggregate state (**completed**) indicates
that the task reached its target of successful runs, even though earlier
attempts failed. The detail marks that situation with the **⚠** sign next to
the attempt breakdown.

### What does the outcome badge next to a completed task mean?

It is the **outcome**: the nuance with which the task closed (for example,
`no_result`). A task can be **completed** and still carry an outcome that
specifies how it ended. It still counts as a success; the outcome only adds
detail. Analyze them in [Analyzing Completions and Failures
(Outcomes)](#analyzing-completions-and-failures-outcomes).

> Note: a server plugin can declare an alias state name that the filters
> accept and translate to **completed** with an outcome. Without a plugin,
> an unknown state name is rejected (`422` in the API, `400` in the admin
> views).

### What is the difference between Cancel, Retry, Fork and Delete?

- **Cancel task**: stops a task that has not finished yet.
- **Retry**: runs the same (terminal) task again, keeping its ID, cache and
  history.
- **Fork…**: creates a new task from the parameters of the current one.
- **Delete task**: deletes a terminal task and all its runs (it cannot be
  undone).

See [Intervening on a Task](#intervening-on-a-task).

### I generated an invitation or a token and can no longer see it

Both the invitation link and the enrollment token are **shown only once**,
when you create them. If you lost it, generate a new one (see [Administering
the Workspace](#administering-the-workspace)).

### <a id="how-do-i-connect-a-worker"></a>How do I connect a worker?

See the section [Connecting a Worker](#connecting-a-worker) of this manual
for the complete step-by-step procedure, including notes per operating
system (macOS, Linux, FreeBSD, Windows/WSL2).

### I changed `client/.env` and nothing happens?

`make restart client` does not reread the `.env` file: it only restarts the
process inside the same container. For a configuration change to take effect,
use `make up client`, which recreates the container with the updated file.
See [Renaming the worker](#renaming-the-worker).

### Why did my worker end up paused after the machine rebooted?

Because **paused** is a decision of yours that the server keeps; neither
`make down client` nor the heartbeat expiry reverts it. Only the **Resume**
button in the dashboard returns it to **online**. See [Pausing, Stopping and
Resuming a Worker](#pausing-stopping-and-resuming-a-worker) and [Worker
states](#worker-states).

### Do I need to configure an API key for the worker?

No. The server issues it at registration time and the worker stores it in its
data volume. See [About the worker identity
(`api_key`)](#about-the-worker-identity-api_key).

### Can I use Docker instead of Podman?

No. The project is Podman-only; there is no supported path with Docker.

### The worker container shows no health status. Is that normal?

Yes. The worker container has no healthcheck, so `podman ps` does not show
`healthy` or `unhealthy`. To diagnose a worker that does not respond, use [A worker shows as `offline` in
the dashboard](#a-worker-shows-as-offline-in-the-dashboard).

### How do I validate a complete environment end to end?

With the end-to-end smoke script, which starts the server and a worker and
verifies the full flow:

```bash
./client/scripts/smoke-e2e.sh
```

It accepts variables such as `SERVER_URL`, `POSTGRES_DSN`, `SKIP_BUILD`,
`SKIP_SERVER` and `WORKER_NETWORK_MODE` to adjust the test environment.

---

<a id="json-api-reference"></a>
## JSON API Reference

This section is for developers and integrators who consume the API directly,
without using the dashboard.

The server generates the OpenAPI documentation while it is running. It
requires credentials, like the rest of the service: `GET /openapi.json`
accepts a PAT (`Authorization: Bearer orcaster_pat_…`) or the dashboard
session cookie and answers `401` without them; `/docs` (Swagger UI) and
`/redoc` are browser pages and only open with the dashboard session (without
a session they redirect to the login; a PAT does not open them). If the
request to `/openapi.json` carries an `Authorization: Bearer …` header, only
that PAT is validated (with an invalid PAT it answers `401` even if there is
a session); other `Authorization` values are ignored and the cookie is used.
The scheme name is case-insensitive (`bearer`, `BEARER` and `Bearer` are the
same, and more than one space may separate the scheme from the token); the
token is case-sensitive. In local development, `API_DOCS_PUBLIC=true` leaves
them open without authentication; do not use it in production.

### Authentication

The system uses two different authentication mechanisms depending on the
origin of the request:

| Origin | Mechanism |
|--------|-----------|
| Workers (agents) | HTTP header `X-API-Key` with the value delivered at registration. |
| Dashboard (human operators) | WebAuthn/passkey session; session cookies managed by the browser. |

The worker API key is obtained at registration (`POST /v1/workers/register`).
The server stores only `sha256(api_key)` hex; the plain-text key is delivered
only once and cannot be recovered.

Any credential mismatch on the worker endpoints returns `401 Unauthorized`
without detailing which check failed. If the worker was deleted
(soft-delete), the server answers `410 Gone`.

### Worker endpoints

Workers interact with the server through three endpoints of the `/v1` API.

#### `POST /v1/workers/register` — Registration

Registers a new worker in the workspace that the enrollment token belongs to.
It is called only once, at first startup, when there are no persisted
credentials.

It does not require `X-API-Key`; the authentication is the
`enrollment_token`.

**Request body:**

| Field | Type | Req. | Description |
|-------|------|------|-------------|
| `enrollment_token` | string | Yes | Enrollment token generated in the dashboard; it allows several registrations until its usage allowance runs out. |
| `hostname` | string | Yes | Hostname of the worker host. |
| `os` | string | Yes | Operating system: `linux`, `darwin`, `freebsd` or `windows`. |
| `arch` | string | Yes | Architecture, e.g. `x86_64`, `aarch64`. |
| `ram_mb` | int | No | Total RAM of the host in MB. |
| `disk_mb` | int | No | Total disk of the host in MB. |
| `version` | string | No | Agent version. |
| `tags` | list[string] | No | Eligibility tags. Default: `[]`. |
| `capabilities` | dict | No | Worker capabilities. Default: `{}`. |
| `name` | string | No | Name shown in the dashboard (max. 128 characters). |

**Response `200 OK`:**

```json
{
  "worker_id": "<uuid>",
  "api_key": "<token>"
}
```

The `api_key` is used as the value of the `X-API-Key` header in all later
calls. The `worker_id` identifies the worker in the `/poll` path.

**Errors:**

| Code | Cause |
|--------|-------|
| `401` | Token nonexistent, revoked, already used or expired. |
| `422` | Invalid body (`enrollment_token` field missing or malformed). |

A `401` or `422` on this call produces, in the worker, an
`EnrollmentTokenRejectedError` with the message: *"The server rejected the
registration with HTTP `<code>`: the enrollment token is invalid, expired or
has no uses left."*

#### `POST /v1/workers/{worker_id}/poll` — Unified poll (heartbeat + claim + signals)

Central endpoint of the worker lifecycle. A single round trip per cycle
combines:

- Update of the heartbeat and worker telemetry.
- Implicit renewal of the lease of the run in progress (if `status=busy`).
- Delivery of cancellation signals for active runs.
- Delivery of a new task (by claim or by targeted push).

> The separate endpoints `/heartbeat`, `/claim` and `commands/{uuid}/ack`
> **do not exist**. Everything happens in `/poll`.

**Authentication:** `X-API-Key` + `{worker_id}` in the path.

**Request body:**

| Field | Type | Req. | Description |
|-------|------|------|-------------|
| `status` | `"idle"` \| `"busy"` | Yes | `idle`: the worker is free and can receive tasks. `busy`: it is running and only accepts targeted pushes. |
| `lease_token` | UUID | No | Token of the active run. Present when `status=busy`; it allows renewing the lease implicitly. |
| `current_task_id` | int | No | ID of the task being run (informational). |
| `current_execution_progress` | int 0-100 | No | Progress reported to the dashboard. |
| `lease_seconds` | int 1-3600 | No | Requested lease TTL. Default: `120`. |
| `name` | string | No | Updates the worker's visible name. |
| `ram_mb` | int | No | Total RAM (in MB). |
| `ram_used_mb` | int | No | RAM currently used (in MB). |
| `disk_mb` | int | No | Total disk of the container rootfs (in MB). |
| `disk_used_mb` | int | No | Disk currently used (in MB). |
| `cpu_pct` | float | No | Percentage of CPU used by the container (cgroup), not by the host. |
| `cache_flush_completed` | bool | No | Confirms to the server that a **Clear cache** requested from the dashboard finished. |

**Response `200 OK`:**

```json
{
  "task": {
    "task_id": 42,
    "command": "run_batch",
    "payload": { "commands": ["echo hello"], "requirements": [] },
    "created_at": "2026-06-01T10:00:00Z",
    "delivery_mode": "claim",
    "signed": true,
    "signature": "<base64>",
    "worker_id": "<worker uuid>",
    "lease_token": "<uuid>",
    "lease_expires_at": "2026-06-01T10:01:00Z",
    "execution_timeout_seconds": null,
    "secrets": { "API_TOKEN": "<value>" }
  },
  "cancel_signals": [
    { "task_id": 39, "lease_token": "<uuid>" }
  ],
  "next_poll_after_seconds": 1
}
```

`task.secrets` carries the workspace [task secrets](#task-secrets) whose name
is declared by `payload.task_env`; it is `{}` without `task_env`, without
matches or without keys configured on the server. It is not part of the
signature. The server delivers tasks that use stored secrets only to workers
that support task secrets.

`task` is `null` when there are no tasks available. `cancel_signals` can
carry several entries; each one identifies a run that the worker must
terminate. `next_poll_after_seconds` is the cadence suggested by the server:
1 s with a task, 5 s when busy, 30 s when idle.

The signature in `signature` is Ed25519 over a canonical message of 6 fields
in lexicographic order (`command`, `created_at`, `idempotency_key`,
`payload`, `task_id`, `worker_id`). The worker verifies it with
`server-ed25519.pub` before running.

**Cadence:** the client clamps `next_poll_after_seconds` to the range
`[ORCH_POLL_MIN_SECONDS, ORCH_POLL_MAX_SECONDS]` (defaults: 1 s and 30 s).

#### `POST /v1/tasks/{task_id}/complete` — Complete a run

The worker calls it when it finishes a run, whether successful, failed or
cancelled.

**Authentication:** `X-API-Key` (without `worker_id` in the path; the key acts
as the identity).

**Request body:**

| Field | Type | Req. | Description |
|-------|------|------|-------------|
| `lease_token` | UUID | Yes | Token of the run; proof of ownership. |
| `status` | `"completed"` \| `"failed"` \| `"cancelled"` | Yes | Result of the run. |
| `exit_code` | int | No | Exit code of the process. |
| `cancelled_reason` | string | No | Only when `status=cancelled`. E.g.: `outer_timeout`. |

**Response `200 OK`:**

```json
{ "ok": true }
```

**Error `409 Conflict`:** the lease already expired or was claimed by another
worker. The worker's work is discarded in this case.

The server evaluates the auto-retry policy after marking the run as
terminal. If the policy applies and attempts remain, it re-enqueues the task
before closing it.
If the task does not declare `auto_retry`, or declares it with
`max_attempts: 0` (the default value), there is no automatic retry: an exit
`75` closes the task as `failed`.

### Task endpoints (from the dashboard / admin)

The server uses these endpoints internally when the dashboard form is
submitted. Any HTTP client with an active session (WebAuthn session cookie)
can also call them.

#### `POST /admin/tasks` — Create a task

**Request body (main fields):**

| Field | Type | Req. | Description |
|-------|------|------|-------------|
| `payload` | dict | No | Content of the task. For `run_batch`: `{ "commands": [...], "requirements": [...] }`. |
| `target` | string | No | Dispatch mode. Default: `"1"`. |
| `priority` | int | No | Priority. Default: `0`; higher runs first. |
| `claim_timeout_seconds` | int | No | Maximum time for a worker to claim the task. |
| `execution_timeout_seconds` | int | No | Total time budget for all the commands. |
| `run_at` | datetime | No | Earliest time from which the task is eligible (`count` mode only). |
| `auto_retry` | object | No | Auto-retry policy. `null` = no automatic retry. `max_attempts` is 0 by default, and 0 = off: to retry you must set it greater than 0 (maximum 20). |

**Example: automatically retry an exit `75`.** Exit `75` is in
`retryable_exit_codes` by default, but it is only retried if `max_attempts` is
greater than 0. This policy retries up to 3 times, waiting 60 s between
attempts:

```json
{
  "payload": { "requirements": [], "commands": ["./my-script.sh"] },
  "auto_retry": { "max_attempts": 3, "backoff_seconds": 60 }
}
```

The other `auto_retry` fields (`sticky`, `retryable_exit_codes`,
`retryable_cancelled_reasons`, `sticky_offline_timeout_seconds`) have default
values; `sticky: true` sends the retry to the same worker again.

**Values of the `target` field:**

| Value | Meaning |
|-------|-------------|
| `"N"` (integer ≥ 1) | N copies to any available worker (`count` mode). |
| `"all-available"` | One copy for each worker online when the task is created (snapshot). |
| `"worker:<uuid>"` | One copy targeted at a specific worker. |
| `"workers:<u1>,<u2>,..."` | N copies, each on a worker of the specified subset. |
| `"all:<u1>,<u2>,..."` | Snapshot restricted to the subset; one copy per eligible worker of the subset. |

All tasks are signed with Ed25519; the `signed` field in the body is accepted
but ignored.

**Response `200 OK`:**

```json
{
  "task_id": 42,
  "status": "queued",
  "target": "all-available",
  "target_mode": "all_available",
  "target_count": 3
}
```

#### `POST /admin/tasks/{task_id}/complete` — not an admin endpoint

Closing tasks is done by the worker through `POST /v1/tasks/{id}/complete`.
There is no equivalent admin endpoint.

### Endpoint summary table

| Method | Path | Who calls it | Purpose |
|--------|------|----------------|---------|
| `POST` | `/v1/workers/register` | Worker (first startup) | Register with an enrollment token |
| `POST` | `/v1/workers/{id}/poll` | Worker (periodic) | Heartbeat, task claim and cancel signals |
| `POST` | `/v1/tasks/{id}/complete` | Worker | Close a run (success / fail / cancel) |
| `POST` | `/admin/tasks` | Dashboard / HTTP client | Create a new task |
| `GET` | `/api/v1/secrets` | `orcaster` CLI / client with PAT | List the task secrets (without values) |
| `PUT` / `DELETE` | `/api/v1/secrets/{name}` | `orcaster` CLI / client with PAT (owner) | Set or delete a task secret |
| `GET` | `/openapi.json` | Client with PAT or session | OpenAPI schema (`401` without credentials) |
| `GET` | `/docs`, `/redoc` | Developer with a session | Interactive OpenAPI (Swagger UI, ReDoc); without a session, 303 to the login |
| `GET` | `/healthz` | Monitoring | Server health check |

---

## Glossary

| Term | Meaning |
|---------|-------------|
| **Workspace** | Isolated space that groups the tasks, workers and records of a team. |
| **Worker** | Connected machine that runs the tasks. |
| **Task** | One or more shell commands that the fleet runs. |
| **Execution** | Each attempt to run a task on a worker. |
| **Payload** | The content of the task: the commands and requirements to run. |
| **Target mode** | Dispatch mode: how many and which workers the task reaches. |
| **Target count** | Number of copies to dispatch in **count** mode. |
| **Priority** | Priority of the task; higher runs first. |
| **Claim timeout** | Maximum time that an assigned worker has to claim the task. |
| **Execution timeout** | Total time budget for all the commands of the task. |
| **Heartbeat** | Periodic signal that the worker sends to the server to report that it is alive. Implemented inside the `/poll` endpoint (it is not a separate call). |
| **Liveness** | Visual indicator of how recent a worker's last heartbeat is. |
| **Enrollment token** | Secret that allows one or several workers to register in a workspace, until the allowance chosen in **Max uses** (1 to 1000) runs out or until it expires or is revoked. Generated in the dashboard. |
| **API key** | Worker credential, delivered at registration. The worker includes it in the `X-API-Key` header in every call. The server stores only its SHA-256 hash. |
| **Lease token** | UUID that the server assigns to each run so that the worker proves it "owns" that run. It travels in the poll and in the complete. |
| **Poll** | Unified worker round trip to the server: it delivers heartbeat, telemetry, cancel signals and, when available, a task. |
| **owner / member** | Roles within a workspace; **owner** has additional administrative permissions. |
| **Audit log** | Record of the administrative actions of the workspace. |
| **Version (variant)** | Tool and version that produced a task, derived automatically from the payload. The **Version** lens filters listings and counters by version; **Current** shows the most recent one of each tool. |
| **Outcome** | Nuance of a terminal close (for example, `no_result`) that the task declares according to its exit code. A `completed` with an outcome still counts as a success. |
| **Artifact** | File that a task produces and uploads when it finishes, downloadable from the task detail. It must be enabled with **Collect output artifacts** when creating the task. |
| **Share link** | Public read-only link to the workspace status page; an **owner** creates it and it is revoked to cut off access. |
| **Status page** | Public page (`/status/<token>`) that a share link opens: progress, counters and anonymized fleet, without login. |
| **Personal access token (PAT)** | Personal credential to operate the workspace from the `orcaster` CLI or the `/api/v1` API; it is shown only once. |
| **Sidecar (`orcaster-proxy`)** | Container that runs next to the worker and acts as the only exit point to the Internet when egress isolation is active. |
| **Golden snapshot** | Pre-baked virtual machine image with the code and the worker image already in place; a requirement of `cloud-init-worker.sh`, which does not clone or download code on its own. |
| **Egress isolation** | Restriction of the worker's (and each task's) network output to an explicit list of allowed domains. |
| **`task_env`** | List of environment variables that a task declares it receives. The value comes from the workspace secret with that name or, if none exists, from the worker's environment. |
| **Task secret** | Value that the server stores encrypted for the workspace and delivers to the tasks that declare its name in `task_env`. It is write-only: only its fingerprint is shown. |
| **`egress_allowlist`** | List of domains that a task declares as allowed for its own network output. |
| **`state.json`** | File with the persisted identity of the worker (`worker_id`, `api_key`), inside the `orcaster-data` volume. |

### Main environment variables (`client/.env`)

| Variable | Default | Purpose |
|----------|---------|----------|
| `ORCH_BASE_URL` | `http://localhost:8000` | URL of the orchestrator server. |
| `ORCH_ENROLLMENT_TOKEN` | (empty) | Enrollment token; used only at the first startup. |
| `ORCH_WORKER_NAME` | (empty) | Name shown in the dashboard. |
| `ORCH_ED25519_PUBLIC_KEY_PATH` | `/var/lib/orcaster/ed25519.pub` | Path to the server public key, inside the container. |
| `ORCH_LEASE_SECONDS` | `120` | Lease TTL requested in each poll. |
| `ORCH_POLL_MIN_SECONDS` / `ORCH_POLL_MAX_SECONDS` | `1.0` / `30.0` | Range of the polling cadence. |
| `ORCH_ENFORCE_ISOLATION` | `true` | Strict per-task isolation mode (see [Enabling strict mode](#enabling-strict-mode-orch_enforce_isolation)). `true` by default in the code; an explicit `false` keeps the legacy mode. Mandatory on a worker with a variable from `ORCH_ISOLATION_REQUIRED_ENV`. |
| `ORCH_ISOLATION_REQUIRED_ENV` | (empty) | Exact names, separated by commas or spaces, of variables that require strict mode; with one present and the flag off, the worker does not start (see [Enabling strict mode](#enabling-strict-mode-orch_enforce_isolation)). In `setup.env`: `ISOLATION_REQUIRED_ENV`. |
| `ORCH_ORCASTER_CACHE_ROOT` | `/var/cache/orcaster` | Root of the task cache; also the base on which the disk reported to the dashboard is measured. |
| `ORCH_ORCASTER_CACHE_DELETE_ON_SUCCESS` | `true` | Whether it deletes a task's cache when closing as **completed**. |

<details>
<summary>Technical Information</summary>

Full list of `ORCH_*` variables, unprefixed variables read from the process
environment and variables exported to the subprocess of each task: see
`client/src/orcaster_worker/config.py` (`Settings`, `env_prefix="ORCH_"`) and
`client/README.md`.

</details>

### Main make targets (from the repository root)

| Command | What it does |
|---------|----------|
| `make build client` | Builds the worker image. |
| `make up client` | Starts (or recreates) the worker, rereading `client/.env`. |
| `make down client` | Stops and removes the worker container and its egress sidecar. Without `N`, only the unsuffixed pair (`orcaster-worker`, `orcaster-proxy`); with `N=<n>`, only that worker; with `ALL=1`, all the workers of the host. It does not touch volumes or networks. |
| `make restart client` | Restarts the process without recreating the container (does not reread `.env`). |
| `make logs client` | Follows the worker logs live. |
| `make shell client` | Opens a shell inside the worker container. |
| `make purge client` | Removes the worker container, its egress sidecar and the data, cache and egress allowlist volumes (deletes the identity). The egress networks, shared by the host's fleet, survive. |
| `make up-proxy` | Starts only the egress sidecar. |

### Task states

| State | Meaning |
|--------|-------------|
| **queued** | Waiting for a worker to claim it. |
| **dispatching** | Being assigned to one or more workers right now. For an **all-available** task it is also the state of one that is **waiting for an eligible worker**: the panel says so in the row and in the detail (see [A task waiting for an eligible worker](#task-waiting-for-an-eligible-worker)). |
| **paused** | Held by an operator; it will not be dispatched. |
| **completed** | Reached its target of successful runs. |
| **cancelled** | Stopped by an operator before completing. |
| **failed** | Exhausted its retry budget without success. |

<a id="task-waiting-for-an-eligible-worker"></a>
### A task waiting for an eligible worker

An **all-available** task is distributed among the workers that could run it **at the moment it was created**. If
none could at that moment, the task does not fail and is not lost: it stays in **dispatching**, the listing marks it
with *waiting for an eligible worker* and its detail says why, with one of these four causes:

| What the detail says | What to do |
|------------------------|-----------|
| There is no worker online | Start a worker (or resume a paused one). |
| There are workers online, but none declares the capabilities that the task asks for | Start a worker with those capabilities in `ORCH_CAPABILITIES`; the detail names them. |
| The workers that qualify are held back by free disk | Free space on those workers. |
| The task uses stored secrets and no worker that qualifies would receive them | Update a worker. |

The reason is recomputed every time you open the page: it describes the fleet **now**, not the one at the time you
created the task. So, a task created without workers goes on to say "none has the capability" as soon as one starts
that does not declare it.

As soon as a worker that can run it comes in, the server assigns the task to it on its own, up to about 30 seconds
later, plus the worker's poll cadence. There is nothing to retry by hand. If the task is no longer of interest,
**Cancel** closes it, and **Pause** takes it out of the wait until you resume it.

This holds for **all-available**. An **all-subset** task (`all:<ids>`) names specific workers, so if none of them
could run it at creation, the task closes **failed** right away and its detail says which of the four causes it was,
measured at that moment.

<a id="run-states"></a>
### Run states

| State | Meaning |
|--------|-------------|
| **assigned** | Reserved for a worker that has not started it yet. |
| **claimed** | Taken by a worker; about to start. |
| **running** | Running on a worker right now. |
| **completed** | Finished successfully. |
| **failed** | Failed during the run. |
| **expired** | The claim window expired before the worker started. |
| **cancelled** | Cancelled by an operator. |
