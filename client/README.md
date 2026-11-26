# orcaster-worker

Worker agent of Orcaster, an orchestrator for distributed tasks. It registers with a central
server, claims tasks, runs them as subprocesses and reports the results. The
worker is distributed and run as an **OCI container image** and is managed with
plain `podman run` (no compose) through the shortcuts in the root Makefile.
**Podman is the only supported runtime**.

**Local build.** After cloning the repository, `make up client` builds the
worker image locally from `Dockerfile` and starts it with `podman run`. The
only registry dependency is the public base image `python:3.13-slim-bookworm`
from Docker Hub; no prebuilt worker image is published. The full operations manual is in
[`docs/user-manual.md`](../docs/user-manual.md).

---

## Requirements

| Platform | Requirement | Notes |
|---|---|---|
| Linux x86_64 / arm64 | Podman >= 4.4 (rootless, preferred) | Main target. Podman-only by project decision. |
| macOS arm64 | Podman Machine (`podman machine init && podman machine start`) | VM overhead accepted. Recommended sizing: 4 vCPU, 4 GB RAM. |
| FreeBSD 15+ amd64 | Supported through the linuxulator (Linux compat layer). | Requires additional host configuration. See [FreeBSD 15 setup](#freebsd-15-setup). |
| Windows | **Not supported** | Use WSL2. |
| WSL2 | Works as Linux x86_64 | Not an official target; follow the Linux instructions. |

> **Host network prerequisites (egress isolation by default).** The default is
> `EGRESS_ISOLATION=1`: `make up client` creates an `--internal` network plus a
> proxy sidecar. This requires the following on the host, **in addition** to
> Podman:
>
> - `aardvark-dns >= 1.11.0` — **validated** by the Makefile (target
>   `.aardvark-check`), which aborts with a clear message if it is missing or too
>   old.
> - `nftables` (the `nft` binary), `netavark` and `passt` — used by Podman to
>   build the `--internal` network. **The Makefile does NOT validate them**: if
>   `nft` is missing, startup does not give an actionable error; it fails with
>   `netavark: nftables error: unable to execute nft`.
>
> Podman Machine (macOS) ships with them. On a freshly provisioned Linux host,
> install them first (Debian: `apt install aardvark-dns nftables passt`;
> `netavark` is installed together with Podman). For dev/CI without this
> infrastructure, use the opt-out `make up client EGRESS_ISOLATION=0` (see
> [Network topology deployment](#network-topology-deployment-authoritative-default)).

---

## FreeBSD 15 setup

Podman runs on FreeBSD by executing Linux containers through the
**linuxulator** (Linux compat layer). The daemon does not use an intermediate VM
as on macOS, so the host needs additional preparation. These steps are
**one-time** per host and persist across reboots.

> Prerequisite: `pkg install gmake uv python313 podman rust` (see the
> Prerequisites table in the root README). The worker needs Python 3.13 inside the
> container, but you also need `python313` on the host because `uv` does not
> download interpreters on FreeBSD.

### 1. Podman storage driver

The Podman port defaults to `driver = "zfs"`. If your root is UFS, edit
`/usr/local/etc/containers/storage.conf` and change it to `vfs`:

```sh
sed -i "" 's/^driver = "zfs"/driver = "vfs"/' /usr/local/etc/containers/storage.conf
# Clear any stale state from the previous attempt with zfs:
rm -rf /var/db/containers/storage /var/run/containers/storage
```

> `vfs` has no layer dedup (each image takes its full size), but it works on any
> filesystem. It is adequate for a single worker.

### 2. `pf` kernel module and NAT for the CNI network

Podman uses a CNI bridge (`cni-podman0`, subnet `10.88.0.0/16`) for the
containers. The bridge requires **pf** enabled with a NAT rule so that the
worker's outbound traffic reaches the server on the LAN.

```sh
# Load the pf module and make it persistent
kldload pf
sysrc -f /boot/loader.conf pf_load=YES

# IP forwarding
sysctl net.inet.ip.forwarding=1
sysrc gateway_enable=YES

# Minimal rule: NAT from 10.88.0.0/16 to the external interface
# Set `ext_if` to your real interface (ifconfig — typically em0, msk0, re0, vtnet0...)
cat > /etc/pf.conf << 'EOF'
ext_if = "msk0"

nat on $ext_if inet from 10.88.0.0/16 to any -> ($ext_if)

pass all
EOF

pfctl -e
pfctl -f /etc/pf.conf
sysrc pf_enable=YES
```

> `pass all` leaves the host unfiltered (it only does NAT). If you already use
> pf as a firewall, merge the `nat` rule into your existing rules instead of
> overwriting `/etc/pf.conf`.

### 3. Verification

```sh
# pf status
pfctl -s info | head -3                # must say "Status: Enabled"

# Connectivity from the host
curl -sS http://<ORCH_HOST>:8000/healthz

# From the container (after `gmake up client`)
podman exec orcaster-worker python -c \
    "import urllib.request; print(urllib.request.urlopen('http://<ORCH_HOST>:8000/healthz').status)"
```

### Additional notes

- On FreeBSD the `Makefile` requires `gmake` (GNU make). The default `make` is
  BSD make and fails with `invalid character " " in variable name "word 2,"`
  because of the use of `$(word ...)`.
- `cryptography` is compiled from source (PyPI does not publish wheels for
  FreeBSD); that is why `rust` is a `pkg` requirement.

---

## Quick install

1. Clone the repository and enter the client directory:

   ```bash
   git clone https://github.com/AInvirion/orcaster.git
   cd orcaster/client
   ```

2. Copy the `.env.example` template to `.env` and fill in the values:

   ```bash
   cp .env.example .env
   $EDITOR .env
   ```

   Required variables:

   ```bash
   ORCH_BASE_URL=https://orcaster.example.com   # see .env.example for 3 scenarios
   ORCH_ENROLLMENT_TOKEN=                           # enrollment token (first start)
   ```

   > The first time you run the worker, it registers itself using
   > `ORCH_ENROLLMENT_TOKEN` (see below) and persists the credentials
   > (`worker_id`, `api_key`) in the `orcaster-data` volume. On later starts it
   > reuses that identity and the token can be left empty.
   >
   > **The worker does not read an `ORCH_API_KEY` from the environment.** The
   > `api_key` is only obtained through self-registration (with an enrollment
   > token) and lives in `state.json` inside the volume; putting `ORCH_API_KEY=`
   > in `.env` has no effect (the runtime ignores it). The only configurable
   > startup credential is the enrollment token.

   > **Enrollment token required by the server (multi-tenancy).**
   > With server multi-tenancy, `POST /v1/workers/register` **requires** an
   > `enrollment_token`, scoped to a workspace. The token is issued by a
   > workspace member (owner or member, out-of-band) and determines which
   > workspace the worker is associated with. A token that is present but
   > invalid (nonexistent, revoked, exhausted or expired) returns `401`; a body
   > **without** the `enrollment_token` field returns `422` (request
   > validation).
   >
   > **Multi-use token for fleets.** The token can be minted as single-use
   > (`max_uses=1`, the default) or multi-use (`max_uses` up to 1000, with a TTL
   > selectable among 1h/1d/7d/30d). A single multi-use token is enough to enroll
   > a whole fleet of up to N workers with **one revocable secret**, instead of
   > minting a different token per host. Each worker still receives its own
   > `api_key` server-side at register; the shared token only authorizes the
   > initial registration step, and revoking it immediately stops future
   > registrations without affecting the workers already enrolled with it.
   >
   > This worker **sends** `enrollment_token` in the `register` body, taking it
   > from `ORCH_ENROLLMENT_TOKEN` (see `src/orcaster_worker/api.py::register` and
   > `config.py`). If it has to self-register (no `state.json` and no
   > `ORCH_API_KEY`) and the token is empty, the worker **aborts at boot** with
   > an actionable message instead of sending a register request that the server
   > would reject with `422`. The token is only used on the first start: once the
   > credentials are persisted in `state.json`, the worker reuses the identity
   > and `ORCH_ENROLLMENT_TOKEN` can be left empty (single-use) or keep pointing
   > to the same multi-use token to bring up the next worker of the fleet.

3. Build the image and start the container (first build: ~1-2 min; later runs
   are cached). From the repository root:

   ```bash
   make up client
   ```

   > **The default is `EGRESS_ISOLATION=1`** (egress topology). It requires the
   > host network prerequisites from the [Requirements table](#requirements)
   > (`aardvark-dns >= 1.11.0`, `nftables`, `netavark`, `passt`). If you do not
   > have them yet or only want to start the worker for dev/CI, use the opt-out:
   > `make up client EGRESS_ISOLATION=0`.

   This shortcut runs a plain `podman build` + `podman run` (see
   [Equivalent without the Makefile](#equivalent-without-the-makefile)). To force
   a rebuild after a `git pull`:

   ```bash
   make down client && make build client && make up client
   ```

   `make down client` without `N` only stops `orcaster-worker` and its
   `orcaster-proxy` sidecar; suffixed workers on the same host
   (`orcaster-worker-<n>`) are left alone. For one of them, pass the same `N` to
   both commands (`make down client N=2 && make up client N=2`).

4. Check that it started correctly:

   ```bash
   make logs client
   ```

---

## Configuration

### Environment variables (`.env`)

The following variables are read from the `.env` file (or from the container's
environment).

**Where `make up client` looks for the env file and the pubkey.** The
`Makefile` resolves both paths by itself, using the same paths as
`run-fleet.sh` and `install-release.sh`:

| Situation | Env file | Pubkey |
|---|---|---|
| Host with config: any of `$CONFIG_DIR/env`, `$CONFIG_DIR/env.*` or `$CONFIG_DIR/server-ed25519.pub` exists (`CONFIG_DIR` defaults to `/etc/orcaster`) | `$CONFIG_DIR/env` or `$CONFIG_DIR/env.N` | `$CONFIG_DIR/server-ed25519.pub` |
| Development checkout (none of the above) | `client/.env` or `client/.env.N` | `client/server-ed25519.pub` |

- On the host the file is called `env.N` (no leading dot); in the checkout,
  `.env.N`. A lone `setup.env` does not count as host config.
- With host config, if one of the two files is missing `make` fails naming the
  `$CONFIG_DIR` path; it does not fall back to `client/`.
- In the development checkout `make` prints a notice on stderr with the paths
  used. This quick start keeps working as written.
- `WORKER_ENV=` and `WORKER_PUBKEY=` are optional overrides and always win
  (`run-fleet.sh` and `install-release.sh` still pass them).
- A file that exists but is not readable by the user running `make` (the host's
  `env.N` is `0640 root:<worker user>`) is an explicit error.

| Variable | Default | Notes |
|---|---|---|
| `ORCH_BASE_URL` | `http://localhost:8000` | Base URL of the orchestrator (include `https://` in production). |
| `ORCH_API_KEY` | -- | **Not configured through env** (the runtime ignores it). The `api_key` is obtained through self-registration (with an enrollment token) and persisted in `state.json` (volume `orcaster-data`). See the startup callout in [Quick install](#quick-install). |
| `ORCH_ENROLLMENT_TOKEN` | -- | Enrollment token required on the **first start** when `ORCH_API_KEY` is empty (the worker does `POST /v1/workers/register`). It can be single-use or multi-use (`max_uses`) — one multi-use token serves several workers of a fleet. Once the identity is persisted in `state.json`, it can be left empty. See the startup callout above. |
| `ORCH_WORKER_NAME` | -- | Optional name to tell workers apart on the same host. |
| `ORCH_CAPABILITIES` | -- | Flat JSON name → boolean\|number that tasks request in `eligibility.capabilities`, e.g. `{"gpu":true,"gpu_memory_mb":8192}`. It is sent on `register` and on every `poll`, so changing it does not re-enroll. No quotes around it. Empty: declares nothing; `{}`: clears the stored ones. Invalid: the worker does not start. |
| `ORCH_HOSTNAME` | -- | Explicit override of the hostname reported on `register`. If empty, the worker uses `HOST_HOSTNAME` → `/etc/host_hostname` → `socket.gethostname()`. |
| `HOST_HOSTNAME` | -- | Hostname of the host where the container runs. Useful inside Podman/Docker: without this value, `socket.gethostname()` returns the container hash (e.g. `7f3a2b1c9e0d`). Pass `-e HOST_HOSTNAME=$(hostname)` or mount `/etc/hostname:/etc/host_hostname:ro`. |
| `ORCH_POLL_MIN_SECONDS` | `1.0` | Minimum cadence between polls to `/poll` (lower clamp of the `next_poll_after_seconds` suggested by the server). |
| `ORCH_POLL_MAX_SECONDS` | `30.0` | Maximum cadence between polls (upper clamp + exhausted backoff). |
| `ORCH_LEASE_SECONDS` | `120` | TTL of the lease requested on claim. |
| `ORCH_ED25519_PUBLIC_KEY_PATH` | `/var/lib/orcaster/ed25519.pub` | **Mandatory.** Path inside the container to the server's public key used to verify the signature of every task. Without a pubkey, the worker silently rejects every task. See [Server public key](#server-public-key-ed25519--mandatory) below. |
| `ORCH_ORCASTER_CACHE_ROOT` | `/var/cache/orcaster` | Root of the task-scoped scratch cache (`<cache_root>/tasks/<job_key>/`). Default: the **dedicated cache volume** `orcaster-cache` mounted at `/var/cache/orcaster` (same tree as the `uv` cache), **not** the `/var/lib/orcaster` data volume. Change it only if you remap the cache volume; without the volume mounted the scratch would land in the container's ephemeral overlay. Double prefix: `env_prefix` `ORCH_` over the `ORCASTER_CACHE_ROOT` alias. |
| `ORCH_ORCASTER_CACHE_DELETE_ON_SUCCESS` | `true` | If `true` (default), the worker deletes the task's task-scoped cache as soon as the execution completes successfully (`completed`); on failure/cancellation it keeps it for the retry. `false` leaves the cache until the periodic GC collects it (age / disk pressure). Double prefix: `env_prefix` `ORCH_` over the `ORCASTER_CACHE_DELETE_ON_SUCCESS` alias. |

> The credentials (`worker_id`, `api_key`) are **not** configured through env:
> they live in the `orcaster-data` volume (`state.json`) with `0600` permissions.

### Server public key (Ed25519) — mandatory

Every task that reaches the worker is signed by the server. The worker verifies
the signature with the server's Ed25519 public key before executing any task;
if the pubkey is not configured or the file does not exist, it **silently
rejects every task** and the server reclaims the lease when it expires. The
worker connects and sends heartbeats but executes nothing.

**1. Obtain the pubkey**

**Option A — from the dashboard (recommended, no host access):**

Open the dashboard → **Workspaces** → click **"Download server public key"**.
A **"Download server-ed25519.pub"** link also appears in the **"Add a worker"**
panel shown after creating an enrollment token. Any workspace member can
download it; no SSH access or access to the server host is required.

Save the downloaded file as `client/server-ed25519.pub` (development checkout)
or `/etc/orcaster/server-ed25519.pub` (host).

**Option B — from the server host (for users with SSH access):**

```sh
# On the server host, derive the pubkey from the private PEM:
openssl pkey -in server/.dev-ed25519.pem -pubout -out server-ed25519.pub
```

Copy the resulting file to `client/server-ed25519.pub` (development checkout) or
`/etc/orcaster/server-ed25519.pub` (host). `make keygen-server` writes to the
resolved `WORKER_PUBKEY` path: `client/` in a checkout, `$CONFIG_DIR` on a host
with config.

**2. Mount it in the worker**

In a development checkout, put `server-ed25519.pub` at
`client/server-ed25519.pub` (next to `.env`); on a host with config it goes in
`$CONFIG_DIR/server-ed25519.pub` (`/etc/orcaster/server-ed25519.pub`). The
`Makefile` picks the path according to the section "Where `make up client` looks
for the env file and the pubkey" and mounts it at `/var/lib/orcaster/ed25519.pub`
inside the container. If you want to keep it at another host path, pass
`WORKER_PUBKEY=/path/to/server-ed25519.pub` to `make up client` (optional
override).

**3. Verify**

```sh
make logs client | grep -i 'signature\|ed25519'
```

`ED25519_PUBLIC_KEY_PATH not configured` and `signature verification failed`
must not appear.

### Volumes

The Makefile mounts two named volumes through `podman run -v`:

- `orcaster-data` (`/var/lib/orcaster`): local state (`state.json`, the command
  idempotency sqlite, logs). It must persist across container recreations to
  keep the worker's identity.
- `orcaster-cache` (`/var/cache/orcaster`): the worker's regenerable cache. It
  holds the `uv` cache (`/var/cache/orcaster/uv`) and the **task-scoped scratch
  cache** (`/var/cache/orcaster/tasks/<job_key>/`, the default `cache_root`).
  It is deliberately separate from the data volume (FHS `/var/cache` vs
  `/var/lib`): deleting it does not touch the worker's identity. Without this
  mount the scratch would land in the container's ephemeral overlay.

> **Disk telemetry**: the `disk_mb` / `disk_used_mb` the worker reports to the
> server on every heartbeat measures the filesystem backing
> `ORCH_ORCASTER_CACHE_ROOT` (default `/var/cache/orcaster`, that is, the
> dedicated `orcaster-cache` volume), not the container's rootfs
> (`sysinfo.detect_disk`). The reason: that is where the tasks' heavy downloads
> land (the `uv` cache and the task-scoped scratch cache), so it is the relevant
> metric for "do I have space for the next task?". It only falls back to the
> rootfs (`/`) if `cache_root` does not exist yet (for example the volume is not
> mounted yet at startup). See also
> [Task execution environment](#task-execution-environment).

### Naming a worker

There are two name-related variables, with different purposes:

| Variable | Where it is defined | What it is for |
|---|---|---|
| `ORCH_WORKER_NAME` | `client/.env` (or `client/.env.N` per worker) | Worker label in the dashboard. Optional; sent on `register` and on every `poll`. |
| `N` | Makefile argument (`make up client N=2`) | Automatic suffix for container, volumes and env file when running several workers on one host. Default: empty (single worker). |

**A single worker on the host** — `ORCH_WORKER_NAME` in `.env` is enough:

```bash
# client/.env
ORCH_WORKER_NAME=my-prod-worker
```

**Changing the name after the first start:** edit `ORCH_WORKER_NAME` in
`client/.env` and restart the container (`make restart client`). The worker
sends the new name on the next poll and the dashboard reflects it on refresh
(polling every 3 s). The worker's identity (`worker_id` in `state.json`) does
not change — only the visible label.

To run **several workers on the same host**, you do not need to touch these
variables by hand: use the Makefile's `N` variable, which derives the container,
volume and env file suffix automatically. See the next section.

### Multiple workers on the same host

To run more than one worker on the same machine, use the Makefile's `N`
variable. `make up client N=2` derives a `-2` suffix automatically:

| Resource | Without `N` (default) | With `N=2` |
|---|---|---|
| Container | `orcaster-worker` | `orcaster-worker-2` |
| Data volume | `orcaster-data` | `orcaster-data-2` |
| Cache volume | `orcaster-cache` | `orcaster-cache-2` |
| Env file (development checkout) | `client/.env` | `client/.env.2` |
| Env file (host with config) | `/etc/orcaster/env` | `/etc/orcaster/env.2` |

The server pubkey (`WORKER_PUBKEY`; `client/server-ed25519.pub` in a checkout,
`/etc/orcaster/server-ed25519.pub` on a host) and the bind-mounted source code
(`WORKER_SRC`, `client/src/orcaster_worker`) are **shared** by all workers —
same server, same repository —, so they carry no suffix.

All targets honor `N`:

```bash
make up client N=2
make logs client N=2
make restart client N=2
make shell client N=2
make purge client N=2
make down client N=2        # stops only worker 2 (and its egress sidecar)
make down client ALL=1      # sweeps ALL the workers on the host (lists them before deleting)
```

`N` is not tied to numbers: `N=staging` also works (`orcaster-worker-staging`,
`-staging` volumes, env file `client/.env.staging` in a checkout). However, for
`make down client ALL=1` to sweep all the workers at once (each with its egress
sidecar), keep the suffixes **numeric**: the sweep only recognizes
`orcaster-worker-<number>`. Without `ALL=1` or `N`, `make down client` only
stops the unsuffixed pair (`orcaster-worker`, `orcaster-proxy`).

#### Why running `make up client` twice is not enough

`make up client` uses a fixed container name (`orcaster-worker`) and, before
creating the container, runs `podman rm -f` on that same name. Running it twice
does not start two workers: the second start **kills the first** and replaces
it. Both would also share the named volumes (`orcaster-data`,
`orcaster-cache`).

Cloning the repository into two folders does not work either: rootless Podman
has a single store of containers and named volumes **per user**, not per
folder. Two clones of the repository still point to the same `orcaster-worker`
container and the same named volumes — they isolate nothing. The suffix derived
from `N` is what separates them.

#### What each worker needs

1. **A valid `ORCH_ENROLLMENT_TOKEN`** in its own env file. For worker 2,
   create `client/.env.2` (on a host with config, `/etc/orcaster/env.2`, which
   `run-fleet.sh` stamps):

   ```bash
   cp client/.env.example client/.env.2
   $EDITOR client/.env.2        # enrollment token + optional ORCH_WORKER_NAME
   ```

   A descriptive `ORCH_WORKER_NAME` (e.g. `worker-gpu-2`) helps tell it apart in
   the dashboard.

   The token can be **single-use** (a different one per `.env.N`,
   `max_uses=1`) or the **same multi-use token** pasted into all the `.env.N`
   files of the fleet — mint it once with `max_uses` >= the number of workers to
   start and reuse the same secret in each env file. Each worker still receives
   its own `api_key` at register; the shared token only authorizes the
   registration step.

2. **Its own data volume** (`orcaster-data-2`). The worker's identity is
   persisted there in `/var/lib/orcaster/state.json` (`{worker_id, api_key}`).
   If two workers shared the data volume, the second would reuse the first's
   identity and would never consume a use of the token of its own. The `N`
   suffix already guarantees one volume per worker.

With that ready, starting the second worker is:

```bash
make up client N=2
```

### First start (self-registration)

On the first `make up client` the worker detects that it has no persisted
credentials, sends `POST /v1/workers/register` to the server, receives
`{ worker_id, api_key }` and saves them in the volume. From then on it uses
those credentials for all polls (which double as an implicit heartbeat and as
task claims). Do not delete the `orcaster-data` volume if you want to keep the
worker's identity.

---

## Common operations

| Operation | Command |
|---|---|
| First start (build + run) | `make up client` |
| Rebuild after `git pull` | `make down client && make build client && make up client` (only the unsuffixed worker; for another one, the same `N=<n>` on `down` and `up`) |
| View logs | `make logs client` |
| Restart worker | `make restart client` |
| API key rotation | Edit the env file (`client/.env`; `/etc/orcaster/env` on a host) and `make down client && make up client` (with `N=<n>` on both if the worker is suffixed) |
| Multiple workers on the same host | Create `client/.env.N` (on a host, `/etc/orcaster/env.N`; its own enrollment token) and `make up client N=2`. See [Multiple workers on the same host](#multiple-workers-on-the-same-host). |
| Uninstall | `make purge client` (deletes the worker's container, its egress sidecar and its `orcaster-data`, `orcaster-cache` and `orcaster-egress-vol` volumes). The `orcaster-egress`/`orcaster-egress-out` networks are shared by the host's whole fleet: delete them by hand (`podman network rm`) only when no worker is left. |
| Purge worker (containers + volumes) | `make purge client`: deletes the worker's container, its `orcaster-proxy` sidecar and its `orcaster-data`, `orcaster-cache` and `orcaster-egress-vol` volumes (use it if `state.json` was orphaned after recreating the server's DB). With `N`: `make purge client N=2`. |

> **Multiple workers:** use the `N` variable (`make up client N=2`), which derives
> the container, volume and env file suffix automatically. Each worker needs its
> own env file (`client/.env.N`, or `/etc/orcaster/env.N` on a host) with its own
> `ORCH_ENROLLMENT_TOKEN` and its own data volume (the identity in `state.json`
> is not shared). Running `make up client` twice does not start two workers: the
> second start kills and replaces the first.

---

## Task examples

The worker image ships with `python3.13`, `curl`, `coreutils` (includes
`sha256sum`), `git` and `uv` preinstalled. For portable tasks that do not
assume additional binaries, using the Python stdlib (`urllib`, `hashlib`) also
works.

**Download a file and verify its SHA-256:**

With Unix tools (shorter, requires `curl` + `sha256sum`):

```json
{
  "requirements": [],
  "commands": [
    "curl -fsSL -o /tmp/payload.bin https://example.com/payload.bin && echo 'EXPECTED_SHA256  /tmp/payload.bin' | sha256sum -c -"
  ]
}
```

With the stdlib (portable to minimal workers or other platforms):

```json
{
  "requirements": [],
  "commands": [
    "python3 -c \"import hashlib, urllib.request, sys; url='https://example.com/payload.bin'; expected='EXPECTED_SHA256'; data=urllib.request.urlopen(url, timeout=30).read(); got=hashlib.sha256(data).hexdigest(); sys.exit(0 if got==expected else f'sha256 mismatch: {got}')\""
  ]
}
```

Both patterns exit with a non-zero exit code on failure, which the orchestrator
interprets as `failed`.

---

## Task execution environment

Each execution runs in a **unique ephemeral `cwd`** created right before
dispatch and removed when it finishes (even on failure or cancellation). Do not
assume that the working directory is persistent or shared across executions or
retries. Files written with a relative path are discarded when it finishes.

To persist results, use an absolute path built from the variables that the
worker exports to the subprocess:

| Variable | Lifecycle | Typical use |
|---|---|---|
| `ORCASTER_CACHE_DIR` | Persists across retries after a failure; deleted on successful completion (default ON) | Reusable download cache (e.g. `--local-dir $ORCASTER_CACHE_DIR`). The worker also points `HF_HOME`, `PIP_CACHE_DIR` and `XDG_CACHE_HOME` to subdirectories of this path. |
| `ORCASTER_ARTIFACTS_DIR` | Persists; downloadable from the dashboard | The job's output artifacts. |
| `TASKORCH_CACHE_DIR` | Same directory as `ORCASTER_CACHE_DIR` | **Deprecated** alias, kept for tasks written before the rename. Use `ORCASTER_CACHE_DIR` in new tasks. |
| `TASKORCH_ARTIFACTS_DIR` | Same directory as `ORCASTER_ARTIFACTS_DIR` | **Deprecated** alias, kept for tasks written before the rename. Use `ORCASTER_ARTIFACTS_DIR` in new tasks. |

The worker sets the four names itself after it builds the task environment, so
a value declared in `task_env` (from the worker environment) or stored as a task
secret does not replace them. They are reserved names: the server rejects them
as secret names and the worker discards a server value for them. If the cache
directory cannot be created, the task gets none of the four, not a value from
elsewhere.

---

## Per-task env and egress isolation

In legacy mode (`ORCH_ENFORCE_ISOLATION=false`, task without `task_env`) a
task inherits the worker process's full environment (all
the secrets in `.env`: `ORCH_API_KEY`, `ORCH_ENROLLMENT_TOKEN`, `GITHUB_TOKEN`,
`API_TOKEN`, etc.) and has unrestricted network egress (same network namespace
as the worker). With task isolation the operator can declare, per task, which
environment variables and which network domains that specific task needs — the
rest stays out.

### The two payload fields

Two optional keys inside `payload` (they travel signed together with the rest
of the payload, Ed25519, with no migration required):

| Field | Type | Semantics |
|---|---|---|
| `task_env` | `list[str]` | Names of environment variables allowed for this task, in addition to a fixed non-secret base (`HOME`, `LANG`, `PATH`, etc.). Each entry is a name (`[A-Za-z_][A-Za-z0-9_]*`, ASCII), never `NAME=value` or a pasted token: the server validates it on create (see below). Declaring a name does not guarantee it arrives: a name that neither the worker's environment nor a stored secret provides is not injected, the task starts anyway and the worker reports it with one `WARNING` per execution, also when the name is optional by design (see "Operational limits under the topology", below). |
| `egress_allowlist` | `list[str]` | Domains allowed for this task's network egress (exact match, or subdomain match with a `.`/`*.` prefix, e.g. `.example.com`). It only has real effect under the internal network topology (`EGRESS_ISOLATION=1`, see below); without it the field is accepted and stored but there is no network enforcement. |

**Tri-state semantics**, identical for both fields: key **absent** from the
payload = legacy (no restriction); key **present, even
`[]`** = strict mode (only what is declared, no extra secrets or domains). They
are independent of each other: you can declare `task_env` without
`egress_allowlist` or vice versa.

The warning about declared names that are not injected **does not depend on
`EGRESS_ISOLATION`**: `ephemeral_venv` emits it on any worker that runs the
task in strict mode. Its exact format, its bounds and the case of a name that is
optional by design are in "Operational limits under the topology", below; it is
described there because the proxy variables are its most frequent case, not
because it only applies to them.

Variables with the `ORCH_` prefix (the worker's control-plane secrets:
`ORCH_API_KEY`, `ORCH_ENROLLMENT_TOKEN`, etc.) are **never** injected into a
task even if explicitly declared: the server rejects with `422` any `task_env`
that includes them, and the worker drops them anyway as a second layer of
defense.

The server validates each `task_env` entry when creating the task (and when
forking it, which is a creation). It must be a string, must not start with a
known token prefix (`hf_`, `ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_`,
`github_pat_`, `glpat-`, `xoxb-`, `xoxp-` or `sk-`, which covers `sk-or-`;
anchored at the start and case-sensitive; it is the same `_TOKEN_PREFIX_RE`
pattern as in `task_env.py`), must be a whole ASCII environment variable name,
`[A-Za-z_][A-Za-z0-9_]*` (the same rule as `_VALID_NAME` in `task_env.py`), and
must not start with `ORCH_`; otherwise it responds `422` with
`payload.task_env[N] ...`, where `N` is the entry's index counted from 0 (`...
looks like a credential (known token prefix), not a variable name; declare the
variable name only`, `... is not a valid environment variable name; declare the
variable name only (letters, digits and underscores, not starting with a
digit), not NAME=value`, `... may not declare ORCH_ vars; those are
worker/control-plane secrets` or `... must be a string`). The message does not
repeat the entry, and the `422` of creating and forking a task
(`/admin/tasks`, `/api/v1/tasks` and their `/{id}/fork`) strips pydantic's
`input` and `ctx.error` from each error. The rule is a prefix guard plus a shape
check, not a secret detector: a credential without one of those prefixes (an AWS
key such as `AKIA...`, a random alphanumeric string) has the shape of a name and
passes, and `API_TOKEN`, `GITHUB_TOKEN` or `http_proxy` remain valid names.

### Declaring isolation from the CLI

```bash
orcaster task create \
  --target 1 \
  --env GITHUB_TOKEN \
  --egress-allow github.com --egress-allow api.github.com \
  --egress-allow codeload.github.com \
  --egress-allow objects.githubusercontent.com \
  --egress-allow release-assets.githubusercontent.com \
  --egress-allow raw.githubusercontent.com \
  --egress-allow pypi.org --egress-allow files.pythonhosted.org \
  --payload '{"requirements": ["requests"], "commands": ["python3 job.py"]}'
```

`--env`/`--egress-allow` are repeatable and are merged **into** the `--payload`
you pass (they do not replace it). Passing either flag, even once, enables
strict mode for that field; for an unrestricted (legacy) task, simply pass
neither.

### Operational limits under the topology

Behavior to keep in mind when declaring isolation with `EGRESS_ISOLATION=1`
active:

- **The proxy is not transparent to strict `task_env`.** `HTTPS_PROXY`,
  `HTTP_PROXY` and `NO_PROXY` are not part of the fixed passthrough base: a task
  with strict `task_env` does not inherit them, so its traffic does not use the
  sidecar and, on the `--internal` network, does not get out (not even to hosts
  in its `egress_allowlist`). If the task needs network access, declare them
  explicitly in `task_env` next to its secrets (`--env HTTPS_PROXY --env
  HTTP_PROXY --env NO_PROXY ...`); they are re-injected from the worker's env.
- **A declared name that the worker does not have is reported, not a failure.**
  In strict mode, a `task_env` name that is not in the worker's environment
  (`EXTRA_ENV`) and is not provided by a stored secret of the task is not
  injected and the task starts anyway. `ephemeral_venv` logs, once per
  execution and before launching `uv`, a `WARNING` with the task id
  (`task_id=<id>`, the one shown by `orcaster task`) and the absent names, only
  names and never values, each with `%r`: `task_id=<id> task_env declares N
  name(s) absent from the worker environment and not provided by the task's
  stored secrets; not injected: 'A', 'B'`. The warning is bounded: it lists at
  most 50 names (`MISSING_NAMES_LOG_LIMIT`) and appends `; M more name(s) not
  listed`; a name longer than 128 characters (`TASK_ENV_NAME_MAX_LEN`, the same
  limit as the server's secret names) is not written and is counted as `; K
  name(s) longer than 128 characters (text omitted)`. `N` is always the total
  number of absent names. It does not warn in legacy mode or for `ORCH_` names
  (they have their own warning). A name that is optional by design, which a
  task always declares even when the worker does not have it, warns on every
  execution in which it is missing: the warning does not distinguish a forgotten
  name from one that is declared on purpose even though it may be missing.
- **An entry that is not a name is counted and not printed.** This is an entry
  that is not a valid variable name (`API_TOKEN=value`, one with line breaks) or
  that starts with a known token prefix (`hf_...`, `ghp_...`, `glpat-...`,
  `sk-...`, the same ones the server rejects on create), from a row stored
  before the server validation. It is only counted: in the warning above it
  appears as `; N entry(ies) are not valid variable names (text omitted)` or,
  with no absent names, as `task_env has N entry(ies) that are not valid
  variable names (text omitted, not injected)`. For an entry with the `ORCH_`
  prefix (denylist) the worker logs only the part before the first `=`, and if
  that part is not a valid name, it only counts it. The line
  `task_id=<id> task_env sources: ...` lists in `missing` and `denied` only
  valid variable names without a token prefix, and appends `invalid_missing=N`
  or `invalid_denied=N` after the list when there were entries that are only
  counted (absent when they are zero; one with a token prefix counts in
  `invalid_missing`). `missing` is bounded like the absent-names warning: at
  most 50 names, plus `missing_not_listed=M` with those not listed and
  `missing_too_long=K` with those longer than 128 characters, which are not
  written; both tokens go after the list, are absent when they are zero and do
  not touch `server=[...]` or the rest of the line. What is printed are names:
  those in `missing`, `denied` and in the absent-name and denied-name warnings
  are valid variable names, and those in `server`, `worker_env`,
  `ignored_server_secrets`, `refused_server` and in the ignored- or
  refused-secret warnings are keys that really exist in the server's secrets or
  in the worker's environment, quoted and escaped with `repr` if they do not
  have the shape of a name (unlikely: the server validates secret names when
  storing them). Injection does not change: a name with a token prefix that the
  worker or the server does have is injected as usual and listed in
  `worker_env` or `server`. If any `server` value came from a secret scoped to
  this worker (`task.worker_scoped_secrets`), the line ends with the separate
  token `server_worker_scoped=[...]`, with names only; without them the line is
  unchanged.

The per-task `egress_allowlist` **does** apply during venv installation: the
worker writes the allowlist before creating the venv, so `uv venv` /
`uv pip install` see the same network permission as the task's commands. A
dependency declared by URL (a `github.com` tarball, `git+https://...`) installs
fine as long as its host is in the `egress_allowlist`.

Note: the strict `task_env` limit from the previous point applies **also to the
install phase**. `uv venv`/`uv pip install` run with the same scrubbed env as
the task's commands (`venv_manager.py::_spawn_uv` uses
`build_scrubbed_base_env(task_env)`), so a task with strict `task_env` that does
not declare `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` leaves `uv` without a proxy
inside the `--internal` network: `venv setup` fails within seconds with a DNS
resolution error (`dns error`, `Name does not resolve`; not a proxy `DENY` nor a
connection timeout), even if the host is in the `egress_allowlist`. If the task
declares `task_env` and its `requirements` need network access, include the
three proxy variables in `task_env`.

### Recommended allowlist for tasks that download packages or data

For tasks that download packages from PyPI and code or release assets from
GitHub, the reference allowlist is the following; add the hosts of any data the
task downloads:

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

An entry with a leading dot (`.example.com`) covers the domain and all its
subdomains. `release-assets.githubusercontent.com` is the host to which
`api.github.com` currently redirects release-asset downloads (without it, a
task's `curl` of the wheel gets a `403` from the proxy);
`objects.githubusercontent.com` is kept for the flows that still use it.

### Related worker environment variables

| Variable | Default | Notes |
|---|---|---|
| `ORCASTER_EGRESS_ALLOW_FILE` | `/var/lib/orcaster-egress/allow.txt` | Per-task file that the worker writes (`_apply_task_egress`) before the venv setup and cleans when the task ends; the proxy sidecar re-reads it on every request. With `ORCH_EGRESS_KEY` the worker signs it (first line `#orcaster-egress-mac v1 <hex>`, HMAC-SHA256 of the body) and the proxy ignores the per-task entries of a file without a valid signature. At startup, the worker sweeps an orphaned allowlist left on the volume by a hard kill (SIGKILL/OOM). It only matters under `EGRESS_ISOLATION=1`. |
| `ORCH_EGRESS_KEY` | -- | Key of the signed allowlist: 32 bytes or more, in hex. It is not written by hand: `make egress-net` creates the Podman secret `orcaster-egress-key[-N]` (per worker, 32 random bytes) once, and `make up client` hands it to the worker and its proxy as this variable (`--secret ...,type=env`; `podman inspect` masks it). The worker removes it from `os.environ` at startup, so a task without `task_env` does not inherit it. Without the key the proxy accepts the file without a signature and logs a `WARNING`; a malformed key stops the worker or the proxy with exit code 1. To rotate it: pause and drain the worker; then, as the rootless user, `podman secret rm orcaster-egress-key[-N]` immediately followed by `make up client N=<n>` (recreates the secret, the sidecar and the worker). Do not use `make purge client`: it also deletes the data volume (`state.json`, the worker's identity). Podman resolves the secret on every container start, so a container created with it does not start without the secret (`podman start`: `no such secret`), nor does the `systemd --user` unit at boot or on its restarts: do not leave the secret deleted while the containers exist, and in a rollback redeploy the previous release first (it recreates the containers without `--secret`) and delete the secret afterwards. Limits: the key is per worker, not per task; `state.json` remains readable by the task (same uid). |
| `ORCH_ENFORCE_ISOLATION` | `true` (code default) | Promotion flag: with `true`, an absent key **or one that is present but malformed** is treated as `[]` (strict) instead of legacy; it closes the escape route to the unrestricted mode. A worker without the variable runs strict; an explicit `false` keeps the legacy mode and is only appropriate while the queue has tasks without `task_env`. The code default is `true`. The installer (`stamp_worker_env`) stamps `ORCH_ENFORCE_ISOLATION=true` into the env file of a new worker and an explicit `ORCH_ENFORCE_ISOLATION=false` into an existing file that lacks the line (it keeps the host's behavior). `push-release` and `install-release` do not stamp `env.N`, so keep the line explicit in each worker's env file. With the flag off, every worker logs a `WARNING` at startup saying that a task without `task_env` inherits the full environment, including `ORCH_ENROLLMENT_TOKEN` and `ORCH_API_KEY` (names only, never values). It is declared in `setup.env` (its own variable `ORCH_ENFORCE_ISOLATION`, `true` or `false`), in `EXTRA_ENV` (if both declare it with different values, the installer aborts) or in `client/.env`. Limit: it only closes environment inheritance; it is not a sandbox (see the egress allowlist limitation). |
| `ORCH_ISOLATION_REQUIRED_ENV` | `""` | List of variables that require strict isolation: exact names separated by commas or spaces, no patterns. The worker's built-in list is empty; the effective one is the union of the built-in list and this setting, and no source can remove a name that another one added. A token that is not a valid name (`^[A-Za-z_][A-Za-z0-9_]*$`) is an error: the worker logs an `ERROR` with the token and exits with code 1 before registering (fails closed). On every start it logs an `INFO` `startup: isolation-required variables checked: ...` with the names (or `(none)`). If a variable in the list is present and non-empty with `ORCH_ENFORCE_ISOLATION` off, the worker logs a `startup:` `ERROR` with the name (never the value) and the recipe, and exits with code 1 before registering (with `--restart unless-stopped`, a crash loop until fixed and recreated). Example: `ORCH_ISOLATION_REQUIRED_ENV=EXAMPLE_PROTECTED_VAR`. On a fleet host it is declared as `ISOLATION_REQUIRED_ENV` in `setup.env`, which the installer stamps into every `env.N`; declaring it inside `EXTRA_ENV` aborts the installer. |
| `ORCH_FLUSH_MARKERS` | `""` | stderr markers that trigger the cache self-flush: literal phrases separated by `;`. Empty = no detection (the core ships none). The worker logs the ones it uses on every start (`startup: flush markers used: ...`). |

### Network topology deployment (authoritative default)

The internal network + CONNECT proxy sidecar is the **authoritative default**
(`EGRESS_ISOLATION=1`): `make up client` brings it up with no additional flags.
The explicit opt-out, for dev/CI without the host's egress infrastructure
(sidecar, `aardvark-dns >= 1.11.0`, `nftables`, `netavark`, `passt`; see the
[Host network prerequisites](#requirements)), is:

```bash
make up client EGRESS_ISOLATION=0
```

With the opt-out `up-client` adds nothing of the topology (no dependency on
`up-proxy`, no internal network, no proxy envs, no egress volume). It only
accepts `0` or `1`: the fleet scripts (`load_env`) reject any other value,
because the `Makefile` enables the topology with an exact match on `1` and a
different "truthy" value would silently disable it. `make up-proxy` brings up
only the sidecar (`up client` already includes it as a dependency by default).
See `make help` and the `Makefile` comments (targets `up-client`, `up-proxy`,
`egress-net`) for the details of the topology (`--internal` `orcaster-egress`
network + `orcaster-egress-out`, `orcaster-egress-vol` volume rw in the worker /
ro in the proxy).

The opt-out does not dismantle a pre-existing proxy: if the worker was running
under egress, the sidecar container still exists (and `06-autostart.sh`, which
detects by presence, persists its systemd unit again). To remove it:
`systemctl --user disable --now container-orcaster-proxy<-N>.service` and
`podman rm -f orcaster-proxy<-N>` as the worker's user.

The `podman run` hardening (`--cap-drop=ALL`, `--security-opt=no-new-privileges`,
`--read-only`, `--pids-limit`) is independent of `EGRESS_ISOLATION`: it is
always active in `up-client`, also under the opt-out.

Honest limits of the model: same UID, a residual channel to the proxy's base
domains, and CONNECT is opaque to the content.

---

## Exit code convention

The worker reports the batch's final `exit_code` (the last command executed) to
the server in `POST /v1/tasks/{id}/complete`. The server interprets it as
follows:

| Exit code     | Meaning                                                                                                      |
|---------------|--------------------------------------------------------------------------------------------------------------|
| `0`           | Success. The execution closes as `completed`.                                                                |
| `75`          | `EX_TEMPFAIL` — transient, retry-eligible failure. If the task has `auto_retry` configured and `75 ∈ retryable_exit_codes` (default), the server triggers an automatic sticky retry on the same worker. Without `auto_retry`, it is terminal like any other non-zero code. |
| `>0` (others) | Terminal failure. The execution closes as `failed`.                                                          |

`75` comes from `<sysexits.h>` (`EX_TEMPFAIL`): "temporary failure; user is
invited to retry". Adopting it in scripts lets the server tell recoverable
failures (throttling, rate limits, transient unavailability of the origin) from
genuine ones (a bug, a corrupt file, invalid credentials).

**Example: a rate-limited download that exits with `75`.**

```python
import sys
import urllib.error
import urllib.request

URL = "https://example.com/data.bin"

try:
    data = urllib.request.urlopen(URL, timeout=30).read()
except urllib.error.HTTPError as e:
    if e.code == 429:
        print(f"download throttled: {e}", file=sys.stderr)
        sys.exit(75)
    raise
```

The script does not need to know whether the task has `auto_retry` configured:
it always emits `exit(75)` when throttled. The server decides what to do with
that code according to the task's policy. If the task has no `auto_retry`, the
`75` is treated like any non-zero exit (terminal `failed` closure).

---

## Development

There are two iteration modes. Both work the same on Mac arm64, Linux
x86_64/arm64 and WSL2.

### Mode A — Development on the host (fast, no container)

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.13+ installed locally.

```bash
cd client
uv sync --extra dev
uv run pytest -q                  # unit suite in client/tests/ (see also make test client)
uv run ruff check && uv run ruff format --check
uv run orcaster-worker --help
```

### Mode B — Development inside the container (no Python on the host)

Useful when the worker must be tested against the production runtime (Python
3.13 on Debian slim, not your host's Python). The `make up client` target
mounts `client/src/orcaster_worker/` as a `:ro` volume at `/app/src/` and
prepends `PYTHONPATH=/app/src` to the container's `sys.path`, so edits are
visible without a rebuild. The mechanism is independent of the base image's
Python version: if you bump the base from 3.13 to 3.14, the bind mount keeps
working unchanged.

```bash
cd client
cp .env.example .env              # edit ORCH_BASE_URL
cd ..
make up client                    # build + run with a src/ bind mount

# Edit a package file (any .py in client/src/orcaster_worker/)
# and apply the changes:
make restart client

# Follow the logs live:
make logs client

# Shut down (only orcaster-worker and orcaster-proxy; with N=2, only that worker):
make down client
```

#### Makefile shortcuts (from the repository root)

| Shortcut | Action |
|---|---|
| `make build client` | Build only (do not start) |
| `make up client` | Build + start the worker with the dev bind mount of `src/` |
| `make down client` | Stop and remove the worker's container and its egress sidecar (does not touch volumes or networks). Without `N`, only the unsuffixed pair; `ALL=1` sweeps all the workers on the host |
| `make restart client` | Restart the worker (applies edits without a rebuild) |
| `make logs client` | Follow logs live |
| `make shell client` | Open bash inside the container |
| `make purge client` | Remove the worker's and its egress sidecar's containers + the per-worker volumes (`orcaster-data`, `orcaster-cache`, `orcaster-egress-vol`); use it if `state.json` was orphaned |

All these targets accept `N=<suffix>` to operate on a specific worker when you
run several on the same host (e.g. `make up client N=2`,
`make logs client N=2`). `make down client` without `N` only stops the
unsuffixed worker and its sidecar; the suffixed ones keep running. To stop all
the workers on the host (the default one and the numerically suffixed ones),
each with its egress sidecar, use `make down client ALL=1`, which lists the
containers before deleting them and cannot be combined with `N`. The egress
networks are shared by the host's whole fleet and are deleted by neither `down`
nor `purge`. See
[Multiple workers on the same host](#multiple-workers-on-the-same-host).

All targets require `podman` in PATH; if it is not there, the Makefile aborts
with a clear message.

#### Equivalent without the Makefile

For advanced users, `make up client` is equivalent to:

```bash
podman build --os linux --network=host -t localhost/orcaster-worker:local client/

podman run -d \
    --name orcaster-worker \
    --restart unless-stopped \
    --env-file client/.env \
    -e PYTHONPATH=/app/src \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONUNBUFFERED=1 \
    -v orcaster-data:/var/lib/orcaster \
    -v orcaster-cache:/var/cache/orcaster \
    -v $(pwd)/client/src/orcaster_worker:/app/src/orcaster_worker:ro \
    --no-healthcheck \
    localhost/orcaster-worker:local
```

In development mode the environment variables are read from the adjacent `.env`
or from process variables through `pydantic-settings`. `Settings` uses
`env_prefix="ORCH_"` (`env_prefix_target="all"`), so the real name in the
environment or `.env` always carries the `ORCH_` prefix:

| Variable | Default | Notes |
|---|---|---|
| `ORCH_BASE_URL` | `http://localhost:8000` | Base URL of the orchestrator. |
| `ORCH_STATE_DIR` | `~/.orcaster-worker` | Local state (`state.json`). |
| `ORCH_ENROLLMENT_TOKEN` | -- | Enrollment token (first start without an `api_key`). |
| `ORCH_POLL_MIN_SECONDS` | `1.0` | Minimum cadence between polls (lower clamp). |
| `ORCH_POLL_MAX_SECONDS` | `30.0` | Maximum cadence between polls (upper clamp + backoff). |
| `ORCH_LEASE_SECONDS` | `120` | Lease TTL. |
| `ORCH_CANCEL_GRACE_SECONDS` | `5.0` | SIGTERM -> SIGKILL grace (POSIX). |
| `ORCH_ED25519_PUBLIC_KEY_PATH` | unset | Without the key, every task is discarded (the signature cannot be verified). |
| `ORCH_RUN_BATCH_PER_COMMAND_TIMEOUT_SECONDS` | `60` | Per-command timeout window in run_batch. |

An alternate `.env` can be passed with `--config-path /path/.env`.

---

## E2E smoke test

The `scripts/smoke-e2e.sh` script validates a full flow: it starts the server
with PostgreSQL, builds the worker image, registers it, enqueues a `run_batch`
task and verifies the output.

```bash
# From client/; starts a temporary PostgreSQL unless DATABASE_URL is set
./scripts/smoke-e2e.sh
```

Environment variables recognized by the script:

| Variable | Default | Notes |
|---|---|---|
| `SERVER_URL` | `http://localhost:8000` | Base URL of the orchestrator. |
| `POSTGRES_DSN` | — | PostgreSQL DSN. If not provided, the script starts a temporary container. |
| `SKIP_BUILD` | — | If defined, skips `podman build` (use when the image already exists). |
| `SKIP_SERVER` | — | If defined, skips migrations and server startup (assumes it is already running). |
| `WORKER_NETWORK_MODE` | — | If `host`, the worker is started with `--network host` (useful in CI). |

---

## Known limitations

- **macOS needs a VM** (Podman Machine).
- **Tasks cannot modify the host directly**: installing host daemons, opening
  privileged ports (<1024) or accessing the host filesystem is outside the
  model. Tasks run inside the container's namespace.
- **GPUs and hardware devices** require manual mapping (`--device` in the
  `podman run`). Not supported out of the box.
- **Tasks that listen on ports** need extra `-p host:container` flags in the
  `podman run`; this requires manual operator intervention.
- **No self-update**: to update a worker, run
  `make down client && make build client && make up client` on the host.

---

## Cancellation guarantee

When the server cancels a task (by admin cancel or by worker-driven close), the signal reaches the worker in the `/poll` response as `cancel_signals[]`. The subprocess kill happens in the same event loop tick that processes that response, **without waiting for an additional poll**.

Internal mechanism (path of a work task):

1. `poll_loop` calls `_process_cancel_signals(resp.cancel_signals, busy)` in `main.py`, which does `busy.abandon_event.set()`.
2. The dispatch in progress is `_run_external` → `dispatch_run_batch(..., abandon_event=busy.abandon_event)` (`main.py`), which forwards the event to `_run_single` (`dispatcher.py`). There the subprocess is awaited together with the event using `asyncio.wait(..., return_when=FIRST_COMPLETED)`; when it is set, `kill_process_group(proc, kill_grace_seconds)` runs in the same tick.
3. `is_busy` (`main.py`) returns `True` while `executor_task` is not `done()`, which only happens **after** the kill. Therefore, while the old process is alive the worker reports `status="busy"` and the server does not assign it another task: there is no concurrent execution on the same worker.

> The kill grace (SIGTERM→SIGKILL) is configurable through **`CANCEL_GRACE_SECONDS`** (default 5 s): `dispatch_run_batch` propagates `settings.cancel_grace_seconds` to `_run_single` (`dispatcher.py`). It only runs out if the subprocess ignores SIGTERM.

**Residual window**: the only unavoidable latency is server-side propagation + the interval until the next poll + the kill grace (`CANCEL_GRACE_SECONDS`). On the order of the poll interval, clamped to `[POLL_MIN_SECONDS=1 s, POLL_MAX_SECONDS=30 s]`. Empirical measurement (poll fixed at 2 s, 5 reps): signal→kill ≈ 1.48 s median (bound = poll interval), kill + `complete` ≈ 0.15 s (SIGTERM is enough, the grace does not run out), total window ≈ 1.63 s median / 1.67 s max. With the production default `POLL_MAX_SECONDS=30 s`, the signal→detection stretch can approach 30 s in the worst case.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| The container does not start / restarts in a loop | `ORCH_BASE_URL` misconfigured or the server does not respond. | Check `client/.env` and the network connectivity from the host. |
| Logs show `unauthorized` or `401` | Invalid API key, or the worker was removed from the server (orphaned `state.json` → crash loop). | Force a new registration by purging the state: `make purge client && make up client` (with a valid `ORCH_ENROLLMENT_TOKEN` in `.env`: register requires it again). |
| `podman ps` shows the worker with no health status (neither `healthy` nor `unhealthy`) | Expected: the worker container has no healthcheck (`--no-healthcheck`). | To know whether a worker is alive, use the dashboard's `online`/`offline` status (heartbeat) and `make logs client`. |
| Worker shows as `offline` in the dashboard | Heartbeat timeout; it may be a network loss or a stopped container. | Check `podman ps -a --filter name=orcaster-worker` and `make logs client`. |
| `make up client` fails because `podman` was not found in PATH | Podman is not installed or is not in the PATH of the shell that invokes make. | Install Podman (https://podman.io). This project is podman-only; there is no docker fallback. |

---

For the full operation of the server and the fleet see
[`docs/user-manual.md`](../docs/user-manual.md).
