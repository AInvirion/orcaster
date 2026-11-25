# Worker provisioning on Debian 13 (rootless Podman)

Scripts to deploy **only the worker** (client) on a fresh Debian 13 host. It is
the counterpart of [`../debian13/`](../debian13/) (which provisions the server),
but the worker runs as a **rootless Podman container**, not as a native service.

Scope: **complete configuration except the token**. The scripts leave the host
ready (rootless Podman, `/etc/orcaster/env`, server public key, image built)
but **do not start** the container: without `ORCH_ENROLLMENT_TOKEN` the worker
aborts at boot. The operator places the token and runs `make up client` at the end.

This suite does not deliver the code itself: it is pushed from the operator's
machine with [`ops/release/`](../../ops/release/), before running
`run-all-worker.sh`. The host clones nothing, does not reach out to GitHub and
stores no git credentials. That same release leaves a root copy in
`/root/orcaster`, from which the suite is run (see
[Where the suite runs](#where-the-suite-runs-root-copy)).

## What each script does

| Script | Purpose |
|--------|---------|
| `00-preflight.sh`    | apt update/upgrade; installs rootless Podman (incl. `passt`/`pasta`) + `make`; creates the `orcaster` user, subuid/subgid and linger. It does not install `git`: the code is pushed by the operator, not cloned. |
| `02-server-pubkey.sh`| Places `/etc/orcaster/server-ed25519.pub` (from `SERVER_ED25519_PUBKEY` or by hand). |
| `03-worker-env.sh`   | Generates `/etc/orcaster/env` (keeps the previous token/api_key on reruns). |
| `04-build.sh`        | `make build client` (rootless build of `localhost/orcaster-worker:local`). |
| `05-smoke-test.sh`   | Checks rootless setup, image, config and connectivity; prints the final steps. |
| `06-autostart.sh`    | Enables autostart at boot through a `systemd --user` unit; run it after `make up client`. Not part of `run-all`. Under `EGRESS_ISOLATION=1` it also persists the unit of the `orcaster-proxy$(SUFFIX)` sidecar and orders the worker after it (`--after`/`--wants`). |
| `user-bus.sh`        | Access to the worker user's `systemd --user` manager: derives `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` when missing, checks that the manager responds and stops the unit before the container is recreated. Used by `make up client`, `make up proxy` and `06-autostart.sh`; it is not a provisioning step and not part of `run-all`. |
| `run-all-worker.sh`  | Runs 00..05 in order. `--from NN` to resume. |
| `fleet-disk-check.sh`| Day-2 ops: measures over SSH the disk of each fleet worker against the same criterion as the gate. Read-only; not part of provisioning. |

## Host configuration

All live host configuration lives in `/etc/orcaster`, outside the checkout:

| File | Mode | What it is |
|---|---|---|
| `/etc/orcaster/setup.env` | `0640 root:orcaster` | Provisioning config (the one the operator edits). |
| `/etc/orcaster/env` | `0640 root:orcaster` | Base worker env. |
| `/etc/orcaster/env.N` | `0640 root:orcaster` | Env of fleet worker N. |
| `/etc/orcaster/server-ed25519.pub` | `0644 root:orcaster` | Server public key. Without it the worker rejects every task. |

`/opt/orcaster` is pure, disposable code: the deployment replaces it entirely.
Backups of `setup.env` and of the env files also go in `/etc/orcaster` (e.g.
`setup.env.bak-YYYYMMDD`, owned by root and without group or other write), never
in `/opt/orcaster` nor in the root copy `/root/orcaster`, which
`install-release.sh` also replaces on every release.

## Where the suite runs: root copy

The scripts in this folder that run as root (`run-all-worker.sh`,
`run-fleet.sh`, `06-autostart.sh` when invoked as root and
`cloud-init-worker.sh`) **are not run from `/opt/orcaster`**. That
tree belongs to the `orcaster` user (the worker uses it as the bind mount for the
code), so a compromised process of that user could edit `lib.sh` or a script and
the next run of the suite would execute it as root. Root must not execute code
that the service user can write.

That is why `ops/release/install-release.sh`, besides the `/opt/orcaster`
tree (`INSTALL_DIR`, owned by the worker), leaves a **root copy** of the same release in
`SOURCE_DIR`, by default `/root/orcaster` (owned by root, without group or
other write), and loads `lib.sh` from that copy. The suite is run from there:

```bash
cd /root/orcaster/setup-scripts/debian13-worker
sudo bash run-all-worker.sh
```

| Directory | Owner | Purpose |
|-----------|-------|---------|
| `/root/orcaster` (`SOURCE_DIR`) | `root:root`, no group or other write | Source of the provisioning suite. Renewed on every release. |
| `/opt/orcaster` (`INSTALL_DIR`) | `orcaster` | Code mounted by the container. `make -C /opt/orcaster up client` and `logs client` are still run **as `orcaster`**, not as root. |

Host config stays in `/etc/orcaster`. `SOURCE_DIR` is changed with
`install-release.sh --source-dir` or with `SOURCE_DIR` in the host's `setup.env`;
`push-release.sh` can only use the latter, because `sudo` strips the environment
(see [`ops/release/README.md`](../../ops/release/README.md)).

> `/root` is `0700` on Debian: commands that use that path are run as root
> (`sudo -i`, or SSH directly as root).

### Source tree guard

This suite's `lib.sh` carries the same guard as the server's: when it runs as
root, it aborts **before any other effect** if the scripts directory or any
ancestor up to `/` is not owned by root, has group or other write, or has an
ACL; if `lib.sh`, any `*.sh` of the suite or the `Makefile` is not a regular
file owned by root without group or other write (symbolic links are rejected);
if it was invoked through a path containing symbolic links; or if the suite tree
and `INSTALL_DIR` are the same directory or one contains the other. Running
from `/opt/orcaster` aborts. The error names the path, the owner uid
and the mode, and the supported layout.

The `setup.env` that the guard checks before sourcing it is
`$CONFIG_DIR/setup.env`, and **every ancestor of `CONFIG_DIR` must also be owned
by root and without group or other write**. The default layout satisfies this:
`/etc/orcaster` is `0750 root:orcaster` and `setup.env` is `0640 root:orcaster`. A
custom `CONFIG_DIR` (for example `/srv/orcaster`) requires root-owned ancestors.

The guard does not act when the process is not root, so the `Makefile` autostart
hook (`06-autostart.sh` as `orcaster`, from `/opt/orcaster`)
keeps working.

**`make up client [N=i]` reads those same files with no arguments.** If the
host has config (any `$CONFIG_DIR/env`, `$CONFIG_DIR/env.*` or
`$CONFIG_DIR/server-ed25519.pub` exists; `CONFIG_DIR` defaults to `/etc/orcaster`),
`make` uses `$CONFIG_DIR/env[.N]` and `$CONFIG_DIR/server-ed25519.pub`, and if
one is missing it fails naming that path, without falling back to `client/`.
With none of them (development checkout) it falls back to `client/.env[.N]` and
`client/server-ed25519.pub` with a warning on stderr. `setup.env` alone does not
count as host config. On the host the name is `env.N` (no leading dot); in the
checkout, `.env.N`. `WORKER_ENV=` and `WORKER_PUBKEY=` still work as overrides
and win over the above (`run-fleet.sh` and `install-release.sh` still pass them).
A file that exists but is not readable by the user running `make` (`env.N` is
`0640` `root:<worker user>`) is an explicit error.

## Server and worker on the same host: not supported

This suite assumes a dedicated worker host. The server and worker installers
use by default the same root copy (`/root/orcaster`) and the same
`INSTALL_DIR` (`/opt/orcaster`). What happens when installing the
worker on a host that has a server depends on how that root copy was built
(`setup-scripts/debian13/README.md`):

- **Git checkout** (has `.git`): `install-release.sh` **aborts** with an error
  saying that `SOURCE_DIR` is a git checkout (it has `.git`), before touching the
  installed code.
- **Copy uploaded with rsync** (no `.git`): it **does not abort**. It replaces
  `/root/orcaster` with the worker release and deletes the previous copy
  (`rm -rf "$source_old"`), including the server's
  `setup-scripts/debian13/setup.env` and its backups, which are lost.
  `INSTALL_DIR` is also replaced (the previous version stays in `.orcaster.old`).

This is an accepted limitation: run the server and the workers on separate
hosts. A different `--source-dir` / `INSTALL_DIR` is neither tested nor
supported.

## Prerequisites

- Debian 13 (Trixie), root access (over SSH, with privileges to run `sudo bash`).
- The worker code pushed from the operator's machine with
  [`ops/release/push-release.sh`](../../ops/release/) (or `install-release.sh` by
  hand for the first delivery; see Bootstrap below). No GitHub credential is
  needed on the host.
- The server's Ed25519 public key (PEM block).
- DNS/network: the VPS must reach `${ORCH_BASE_URL}/healthz`.
- `fs.protected_hardlinks=1` (the Debian default). `00-preflight.sh` and
  `install-release.sh` run `chown -R` as root over `INSTALL_DIR`, which
  `orcaster` controls: `chown -R` does not follow symbolic links, but it does change the
  owner of a hardlink's target. With the sysctl at `0` both stop (also if it is
  not exactly `1`, or cannot be read) with a message that names it.
  Remedy: `sysctl -w fs.protected_hardlinks=1`, persist it in
  `/etc/sysctl.d/60-protected-links.conf` (`fs.protected_hardlinks = 1`) and rerun.

> **`ORCH_BASE_URL` must be a stable DNS name, not an IP.**
> The value is stamped into each host's `/etc/orcaster/env` at provisioning, so
> with an IP any move of the server (another host, another provider) forces
> touching and re-enrolling the whole fleet. With a DNS name it is enough to
> repoint the `A` record and the workers keep working without intervention.

## Bootstrap (first start)

The host clones nothing and needs no GitHub credential. The code is pushed from
the operator's machine with the tools in
[`ops/release/`](../../ops/release/):
`make-release.sh` builds the package with `git archive` (no `.git`, with an
embedded `RELEASE` file that declares version and commit), and `install-release.sh`
installs it on the host.

**First delivery, on a freshly created host (no `orcaster` user yet).**
`push-release.sh` does not work for this step: by design it invokes
`install-release.sh` without `--no-restart`, and that path tries to recreate the
worker containers before the `orcaster` user or the config in `/etc/orcaster`
exist. The first delivery is done by invoking `install-release.sh` by hand, with
`--no-restart`:

```bash
# On the operator's machine, with the repo checked out:
(
    set -euo pipefail
    tgz="$(ops/release/make-release.sh)"; base="$(basename "$tgz")"
    host=root@new-worker
    # Private directory (0700, owned by the ssh user) with a random name on the host:
    # never a fixed /tmp name, which another local user could create first.
    stage="$(ssh "$host" 'umask 077; mktemp -d "${TMPDIR:-/tmp}/bootstrap.XXXXXXXXXX"')"
    # Removed on exit, also if something fails.
    trap 'ssh "$host" "rm -rf -- $stage"' EXIT
    scp "$tgz" "$tgz.sha256" ops/release/install-release.sh "$host:$stage/"
    # sha256sum -c inside the directory, BEFORE `sudo bash`.
    ssh "$host" "cd '$stage' && sha256sum -c '$base.sha256' \
        && sudo bash '$stage/install-release.sh' --tarball '$stage/$base' --no-restart"
)
```

The block (in a subshell, paste it as is) creates a random private directory on
the host, verifies the `sha256` there before `sudo bash` and removes it on exit,
also on failure; it does not leave the installer under a fixed `/tmp` name.

`install-release.sh` validates the `sha256`, extracts to a new directory and only
then replaces `/opt/orcaster`; it also leaves the root copy in
`/root/orcaster` (see [Where the suite runs](#where-the-suite-runs-root-copy)).
With `--no-restart` it does not try to touch any container. Once the code is
installed, continue on the host, **from the root copy**:

```bash
# The 'orcaster' group does not exist yet on a freshly provisioned host (it is
# created by 00-preflight.sh, which runs inside run-all-worker.sh): the
# fallback without -o/-g leaves the file root:root and 00-preflight.sh reconciles
# owner and mode as soon as it creates the user (same pattern as
# cloud-init-worker.sh).
install -d -m 0750 -o root -g orcaster /etc/orcaster 2>/dev/null \
    || install -d -m 0750 /etc/orcaster
cd /root/orcaster/setup-scripts/debian13-worker
install -m 0640 -o root -g orcaster setup.env.example /etc/orcaster/setup.env 2>/dev/null \
    || install -m 0640 setup.env.example /etc/orcaster/setup.env
$EDITOR /etc/orcaster/setup.env      # ORCH_BASE_URL + public key

sudo bash run-all-worker.sh
```

`run-all-worker.sh` runs `00-preflight.sh` (installs rootless Podman and creates
the `orcaster` user), `02-server-pubkey.sh`, `03-worker-env.sh`, `04-build.sh` and
`05-smoke-test.sh`. When it finishes, continue with
[Post-deploy steps](#post-deploy-steps-place-the-token-and-start) to
place the token and start the worker.

**Later releases, on an already provisioned host** (existing `orcaster` user,
`/etc/orcaster/setup.env` already in place): use
[`push-release.sh`](../../ops/release/), which builds the package, pushes it and
runs the full `install-release.sh` (code swap and container recreation, never a
plain restart: see [`ops/release/README.md`](../../ops/release/README.md)
for the details of the three pieces and `deploy.local.env`).

`push-release.sh` only serves hosts with the layout that `run-fleet.sh` builds:
`/etc/orcaster/env.N` per worker and suffixed containers (`orcaster-worker-N`,
1..`WORKERS`). A host finished by hand with the Bootstrap above
(`/etc/orcaster/env` without suffix, `orcaster-worker` without suffix) does
**not** match that layout: `install-release.sh` validates this before touching
the installed code and aborts reporting that `/etc/orcaster/env.1` is missing
instead of recreating the wrong container. For such a host to receive releases
through `push-release.sh`, first converge it to the `run-fleet.sh` layout (which
stamps `env.1` and creates the suffixed worker) instead of mixing the two
conventions.

## Mass deployment via cloud-init (DigitalOcean / Vultr / any VPS)

`cloud-init-worker.sh` is **exclusively the golden snapshot path**: it requires
the VPS to boot with the code already installed (the `RELEASE` file in
`/opt/orcaster`, plus the root copy in `/root/orcaster`, from
which the script itself runs) and the worker image already built. It clones
nothing, downloads nothing and accepts no git credential. If the VPS boots
without installed code, the script aborts immediately, saying that a release
must be pushed from the operator's machine with
[`ops/release/push-release.sh`](../../ops/release/).

This means a fleet deployment via cloud-init has two steps: first provision
**one** reference host by the manual path (Bootstrap above, until the worker is
started and working), and only then take a snapshot of it to use as the base for
the rest of the fleet. With the code and the image already baked into the
snapshot, the user-data of each new VPS is reduced to exporting the **CONFIG
block** and running the script that already lives inside:

```bash
#!/usr/bin/env bash
set -euo pipefail
export ORCH_BASE_URL="https://orcaster.example.com"
export ORCH_ENROLLMENT_TOKEN="<multi-use-token>"      # the SAME one for the whole fleet
export SERVER_ED25519_PUBKEY="-----BEGIN PUBLIC KEY-----
...=
-----END PUBLIC KEY-----"
export WORKER_COUNT=1
export WORKER_NAME_PREFIX="myfleet"
# export EXTRA_ENV="API_TOKEN=tok_xxx"                  # task secrets (optional)
# export WORKER_MEMORY="28g"                          # memory cap per container (optional)
# export WORKER_RO_MOUNTS="/opt/example-ro"                   # host dirs mounted :ro at the same path (optional)
# export ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION="0.05"  # disk gate floor (optional)
# export ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS="600"    # gate fail-open deadline (optional)
# export ORCH_ENFORCE_ISOLATION="true"                # strict isolation (optional; new host => true)
# export ISOLATION_REQUIRED_ENV="EXAMPLE_PROTECTED_VAR"  # variables that require isolation (optional)

exec bash /root/orcaster/setup-scripts/debian13-worker/cloud-init-worker.sh
```

There is no need to install `git` or clone anything in the wrapper: the code, the
image and the script itself are already on the snapshot's disk. The section
[Producing the reference snapshot](#producing-the-reference-snapshot)
below details how to reach that state and how to purge the worker identity
before snapshotting.

- **DO / Vultr:** the user-data above works unchanged for
  DigitalOcean ("User Data" when creating the Droplet) and Vultr ("Startup Script" /
  cloud-init); both run standard cloud-init on the Debian 13 image.
- **Multi-use token:** mint it with `max_uses >= WORKER_COUNT * (VPSs in the fleet)`,
  **with extra margin for retries**: a VPS destroyed and recreated from scratch
  after a failure enrolls again and consumes another use (the one from the failed
  attempt is not recovered). A single revocable secret enrolls the whole fleet; each
  worker receives its own server-side `api_key` (persisted in the `orcaster-data`
  volume; it does not re-enroll on reruns).
- **Base image Debian 13 (Trixie):** create the VPSs with that image (DO and Vultr
  offer it). The scripts warn but do not abort on other versions/distros
  (`require_debian13` in `lib.sh`).
- **Without baked code or image, the script aborts early and bootstraps
  nothing:** a VPS created from a base image (not from the reference
  snapshot) has neither `RELEASE` nor the worker image, so
  `cloud-init-worker.sh` has no way to fetch code on its own. The error
  message says to run `ops/release/push-release.sh` from the operator's
  machine; if the code is already there but provisioning is unfinished
  (`orcaster` user, image), it says to run
  `run-all-worker.sh` by hand on the host.
- **`ORCH_BASE_URL` validated early:** if CONFIG leaves it empty or with the
  example placeholder (`https://orcaster.example.com`), the script aborts
  before touching the system.
- **`EXTRA_ENV` / PEM with metacharacters:** `setup.env` is written with escaped
  single quotes (`env_quote` in `lib.sh`), so `$`, quotes, backticks or
  newlines inside `EXTRA_ENV` or `SERVER_ED25519_PUBKEY` are preserved
  literally when the file is sourced again, without being expanded or
  executed.
- **`WORKER_MEMORY` (memory cap per container):** optional; empty = no
  cap (default). With a value (number + mandatory suffix `b`/`k`/`m`/`g`, e.g.
  `28g`), each worker's `podman run` receives `--memory`. The worker also
  reports to the orchestrator the real memory of its cgroup (`memory.max`)
  instead of the host's RAM. An invalid format (including a number without a suffix,
  almost certainly a typo for plain bytes, or a zero) aborts early (cloud-init and
  `load_env`), before touching the fleet.
  **Manual reruns:** `make up client N=i` itself reads
  `WORKER_MEMORY` from `setup.env` when it comes neither from the environment nor
  from the command line, so a worker recreated by hand **keeps the host's cap**.
  An explicit empty value (`make up client N=i WORKER_MEMORY=`) is still the
  opt-out, and a value on the `make` line wins over the file. If a
  worker starts without a cap anyway, it says so in its own log
  (`container running with NO memory cap`).
- **`WORKER_RO_MOUNTS` (read-only host directories):** optional;
  empty = no extra mount (default) and the `podman run` stays the same as
  before. With a value (absolute paths separated by spaces, e.g. `/opt/example-ro`),
  each worker receives `-v <dir>:<dir>:ro`: the same directory, at the same
  path, read-only, without `z` or `Z`: both make podman relabel the host
  content for SELinux (`z` is the label shared between containers, `Z` the
  private one of a single container); Debian hosts do not run SELinux and the
  scripts do not relabel host content; SELinux hosts are out of scope. It is what
  tasks need that install `<package> @ file:///opt/example-ro/<wheel>`: that
  path is resolved inside the container, not on the host.
  **Validation:** a single rule, `check-ro-mounts.sh`. The syntax (absolute,
  only `[A-Za-z0-9._/-]`, no `.`/`..`, no trailing slash, neither `/` nor a path
  that the container already uses or a system path: `/app`, `/var`, `/tmp`, `/etc`,
  `/usr`, `/home/orcaster`, etc.) aborts in cloud-init and in `load_env`. The
  state of the host is measured, as `orcaster`, by `run-fleet.sh` before its loop, by the
  pre-swap gate of `install-release.sh` and by `make up client` before
  `podman rm -f`: the directory exists and `orcaster` can read and
  traverse it (podman resolves the source as that user), and in addition the
  directory is `o+rx` and everything under it is `o+r` (files) or `o+rx`
  (directories). The second part is what matters inside: the worker runs as
  uid 1000 of the image, which in rootless podman is a host subuid, neither
  `orcaster` nor in its group, and only the "other" bits reach it. An
  `/opt/example-ro` at `0750 root:orcaster` or a wheel at `0640` would pass a `test -r`
  done as `orcaster` and still give EACCES in the task. An error leaves the
  running container intact and names the first entry that
  fails.
  **Manual reruns:** like `WORKER_MEMORY`, `make up client N=i` reads
  the list from `setup.env` when it comes neither from the environment nor from the
  command line; an explicit empty value (`WORKER_RO_MOUNTS=`) is the opt-out. If whoever
  runs make cannot read `setup.env` (outside the `orcaster` group), the
  recipe WARNS about it instead of silently creating the container without the mounts.
  **A change is applied only when the container is RE-CREATED** (`make up client`,
  `run-fleet.sh` or a deployment); a `podman restart` or restarting the systemd
  unit starts the existing container with its previous mounts.
- **`WORKER_COUNT` / `EXTRA_ENV`:** the CONFIG above brings up 1 worker; for
  several, raise `WORKER_COUNT`. `run-fleet.sh` stamps `/etc/orcaster/env.1..N`, each
  with `EXTRA_ENV` appended (a generic channel for runtime secrets that the
  **TASKS** need, not the worker, e.g. `API_TOKEN`/`GITHUB_TOKEN`), and
  starts and enables autostart for each worker.
- **Name of each worker (`WORKER_NAME_PREFIX`):** a new slot
  (without `env.N`, or with an empty `ORCH_WORKER_NAME`) receives `PREFIX-N`, with the
  hostname as prefix if `WORKER_NAME_PREFIX` is empty. A rerun **keeps**
  the non-empty `ORCH_WORKER_NAME` that `env.N` already has, even if it does not follow that
  pattern (a `worker-1` worker does not become `worker-1-1`) and even if `WORKER_NAME_PREFIX`
  has changed: the worker sends its name on every poll and the server overwrites the one
  it had. To rename, edit or empty `ORCH_WORKER_NAME` in `env.N` and
  rerun `run-fleet.sh`. A name made only of spaces counts as
  empty. A pruned slot (see "Reducing `WORKER_COUNT`") loses its `env.N`, and
  with it the name: if the fleet grows again, the worker reuses its identity
  but comes back as `PREFIX-N`.
- **Worker disk gate (`ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION` /
  `ORCH_ORCASTER_CACHE_CLAIM_GATE_MAX_SECONDS`):** declarative
  channel in `setup.env`, separate from `EXTRA_ENV` — this is **worker** config
  (free disk floor / fail-open deadline of the claim gate), not task
  secrets. `stamp_worker_env` (`lib.sh`) writes them in their
  own commented block of each `/etc/orcaster/env(.N)`, and they **survive a manual
  rerun** (`03-worker-env.sh`, `run-fleet.sh`) because the value is re-read from
  `setup.env` on every run instead of being passed as a `make` argument. Both empty (default) = the
  line is not stamped and the worker applies its own default (floor: inherits
  `ORCH_ORCASTER_CACHE_MIN_FREE_FRACTION`, currently 0.20; deadline: 1800 s). The format is
  validated in `load_env` and in `cloud-init-worker.sh` (number 0.0-1.0 for the
  floor, number >= 0 for the deadline) so as not to stamp a value that makes
  `Settings()` blow up when the container starts; the range against the
  GC floor is validated by the worker itself at startup (see
  `client/src/orcaster_worker/config.py`), not by this script.
  **Precedence if the same key ends up in both channels:** `stamp_worker_env`
  writes the gate block AFTER the `EXTRA_ENV` one, and `podman run
  --env-file` (like python-dotenv) resolves a duplicated key with the
  LAST occurrence in the file, so the gate channel wins over a value
  also declared in `EXTRA_ENV`. `load_env` warns on stderr (without aborting) when it detects the same
  key in both channels, so that the operator cleans up the leftover in
  `EXTRA_ENV`.
- **Task environment isolation (`ORCH_ENFORCE_ISOLATION` /
  `ISOLATION_REQUIRED_ENV`):** declarative channel in `setup.env`,
  separate from `EXTRA_ENV`. With `ORCH_ENFORCE_ISOLATION=false` (legacy mode) a
  task without `task_env` inherits the worker's full environment, including
  `ORCH_ENROLLMENT_TOKEN` and `ORCH_API_KEY`. `stamp_worker_env` stamps the flag in
  its own block, after `EXTRA_ENV`: the value from `setup.env` wins; if it does not
  declare it, the line from `EXTRA_ENV` (with no line of its own, so as not to weaken it);
  if neither, the value from the previous `env.N`; an `env.N` that already exists without the line
  receives an explicit `false` (it keeps its behavior)
  and a new installation receives `true`. `setup.env` and `EXTRA_ENV` with different
  values abort. To move an existing host to strict mode: drain the
  tasks without `task_env`, set `ORCH_ENFORCE_ISOLATION=true` in `setup.env` and
  rerun `run-fleet.sh` (a `podman restart` does not re-read the
  `--env-file`). `ISOLATION_REQUIRED_ENV` is a list of exact names
  (commas or spaces; each one `[A-Za-z_][A-Za-z0-9_]*`) that is stamped as
  `ORCH_ISOLATION_REQUIRED_ENV=<comma,joined,names>`: with one of those
  variables present and isolation off, the worker does not start. The pre-swap
  gate of `install-release.sh` reads the same list: the last
  `ORCH_ISOLATION_REQUIRED_ENV` line of each `env.N` and, for `EXTRA_ENV`, this
  `ISOLATION_REQUIRED_ENV` plus the `ORCH_ENFORCE_ISOLATION` of `setup.env`
  itself; a malformed list aborts the deployment before the swap. A malformed name aborts in `load_env` and in
  `cloud-init-worker.sh`; declaring it inside `EXTRA_ENV` also aborts.
- **Two-step deployment (1/2 and 2/2):** `run-fleet.sh` reports `1/2` when the
  containers are started and only declares `2/2` (and the script,
  complete) when **every** worker has credentials issued by the
  orchestrator (`state.json` in its data volume), that is, when it is already
  visible in the dashboard. With an expired token or exhausted `max_uses` the deployment
  aborts with a timeout and a message saying so, instead of reporting a false
  success with the fleet in a crash loop. Knobs: `ENROLL_WAIT` (default 120 s) and
  `ENROLL_POLL` (default 5 s), via environment or `setup.env`.
- **Early credentials check:** before starting each worker,
  `run-fleet.sh` requires that it has something to register or identify itself with: the token
  from `setup.env`, a previous `ORCH_API_KEY`/token in its `env.N`, or a `state.json`
  with `worker_id` and `api_key` in its `orcaster-data-N` volume (the same probe
  as step 2/2, via `podman unshare` and without printing the content). With
  none of the three it aborts with an error saying that worker N has no credentials. A `setup.env`
  without a token already started, because `stamp_worker_env` keeps the previous token
  of `env.N`; `state.json` covers an `env.N` with neither token nor
  `ORCH_API_KEY` (deleted, regenerated or with the token cleared by hand) with the
  volume still enrolled, which thus keeps starting.
- **Network retries:** in the zero-touch boot, the only network operation
  of `cloud-init-worker.sh` is the `healthz` preflight, which retries up to 5
  times (`retry()` in `lib.sh`, `RETRY_MAX`/`RETRY_DELAY`, default 5 s each):
  a transient DNS/network blip does not leave the VPS half provisioned. The
  `apt-get` of `00-preflight.sh` does **not** go through `retry()`; it does not apply to
  this path anyway, because `cloud-init-worker.sh` runs
  `run-all-worker.sh --from 02` and never executes step `00` (the reference host
  already has rootless Podman and the `orcaster` user baked in).
- **Reducing `WORKER_COUNT`:** a rerun with fewer workers **prunes** the
  extra ones (disables the systemd unit, brings the container down and deletes its
  `/etc/orcaster/env.N`). The `orcaster-data-N` volumes are kept on purpose:
  if the fleet grows again, the worker reuses its already enrolled identity
  without consuming another use of the token.
  Pruning is driven by the `env.N` files in `/etc/orcaster`, which are the ones
  `run-fleet.sh` stamps: it is its own bookkeeping, not an inventory of the host.
  A `orcaster-worker-N` container whose `env.N` never existed there (for example a container created by hand, or an earlier prune whose `down`
  failed and deleted `env.N` anyway) falls outside that glob, so `run-fleet.sh` **warns** on
  stderr and does not prune it: bringing it down is the operator's decision, with
  `systemctl --user disable --now container-orcaster-worker-<n>.service` and
  then `make down client N=<n>`, both as `orcaster`.
- **Rerun on a running fleet:** before recreating each worker,
  `run-fleet.sh` stops its systemd unit if it is active; without that, the
  `podman rm -f` of `make up` races against the unit's `Restart=always`
  ("container name is already in use").
- **Log:** the full boot is kept in `/var/log/orcaster-cloud-init.log` on the VPS
  (mode `0600`, readable only by root).
  Exception: an abort of the source guard is not recorded there, only in
  `/var/log/cloud-init-output.log`, because `lib.sh` is sourced before the
  redirection to its own log. If a VPS was not provisioned and
  `orcaster-cloud-init.log` does not exist or is empty, look at that file (see
  [Source tree guard](#source-tree-guard)).
- **Recovering a half-provisioned VPS:** the script is idempotent; if a
  run aborted (e.g. enrollment timeout with an exhausted token), fix
  the cause, export the CONFIG again and rerun it over SSH:
  `bash /root/orcaster/setup-scripts/debian13-worker/cloud-init-worker.sh`.
  If instead it aborted because of missing code or image (wrong snapshot or
  one taken before the build finished), rebake the
  reference snapshot, or push the release by hand with
  `ops/release/push-release.sh` followed by `run-all-worker.sh` on the host.

### Producing the reference snapshot

The reference snapshot needs no credential carried in the provider's metadata.
All it needs
is a host provisioned by the manual path, with the code installed
and the image built. The recipe:

1. Provision a VPS through the Bootstrap above: `install-release.sh` (or
   `push-release.sh`, if the host already has a `orcaster` user from a previous
   run) to install the code, `setup.env` with `ORCH_BASE_URL` and the
   public key, and `sudo bash run-all-worker.sh` (from
   `/root/orcaster/setup-scripts/debian13-worker`) to leave it with
   rootless Podman, the `orcaster` user and the image built. The reference host
   must already have received a release that leaves the root copy:
   the snapshot must contain `/root/orcaster`, because the user-data above
   runs the script from there.
2. Start it with the [Post-deploy steps](#post-deploy-steps-place-the-token-and-start)
   below and confirm that it enrolls against the orchestrator.
3. **Purge the worker identity before snapshotting** (see the block
   below): without this step, every VPS that boots from the snapshot
   would clone the same identity.
4. Take the **snapshot** of the VPS with the provider (DO/Vultr).

From that snapshot, each new VPS in the fleet boots with `RELEASE` and the
image already present: `cloud-init-worker.sh` detects it (`should_bootstrap` in
`lib.sh`, which requires the `RELEASE` to be readable by `orcaster`, who is the one that reads it,
and the image to be present in `orcaster`'s rootless store) and proceeds directly with steps 02-05
(pubkey/env/build/smoke-test), which are idempotent and cheap: the build
resolves through Podman's layer cache instead of rebuilding from scratch, so
the boot is much faster than a cold provision, though not instantaneous.

**Updating the code of an already baked snapshot:** `install-release.sh` (via `push-release.sh` or by hand) verifies the
sha256 of the tarball, requires it to carry the `RELEASE` file and, after replacing
the installed code (and renewing the root copy), prints the manifest that
was installed. It does not compare the new release against any expected commit:
whoever decides what code runs on the host is the operator who pushes the
release, not a script check. To bring a new version to a whole fleet created
from a snapshot, the way is
`ops/release/push-release.sh` against each live host (see
[Deploying a new version to the fleet](../../docs/user-manual.md#deploying-a-new-version-to-the-fleet)),
or rebaking the reference snapshot with step 1 above and recreating
the fleet from the new snapshot.

**Before taking the snapshot, purge the worker identity from the reference
VPS.** As explained in "Worker identity" below,
`worker_id`/`api_key` are persisted in the `orcaster-data` volume
(`orcaster-data-N` for each suffixed worker) after the first enrollment; a
disk snapshot includes those volumes, so **every** VPS that
boots from that snapshot would clone the SAME worker identity
(conflicting heartbeats/leases against the orchestrator). Purge each volume of the
reference VPS before snapshotting. **Do not use `make purge client` as
root:** that target invokes ROOT's podman (rootful storage, distinct from
`orcaster`'s rootless one) and also swallows its own errors, so it reports
OK while the real rootless volumes survive intact. Always purge
as `orcaster` setting `XDG_RUNTIME_DIR`, the same convention the
rest of the suite uses (`as_worker` in `lib.sh`):

```bash
# `cd /` first: sudo inherits the caller's cwd, and from a root SSH session
# (cwd /root, mode 700) podman dies with "cannot chdir" before running.
cd /
# For each N provisioned on the reference VPS (the fleet always uses N=1..WORKER_COUNT):
sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$(id -u orcaster) \
    podman volume rm orcaster-data-1 orcaster-data-2
```

**Also purge `/etc/orcaster` on the reference VPS.** The live host config lives
there, outside the volumes: `setup.env` and each `env.N` carry the multi-use
`ORCH_ENROLLMENT_TOKEN` and whatever `EXTRA_ENV` declares (including `API_TOKEN`,
if the host uses it). A snapshot that carries those files leaks those credentials
to **every** VPS that boots from it, besides starting with the token already
used. It would also carry the `ORCH_WORKER_NAME` of each `env.N`, which
`run-fleet.sh` keeps on a rerun: all the clones
would appear with the reference host's name instead of `hostname-N`.
Delete them together with the volumes:

```bash
sudo rm -rf /etc/orcaster
```

Only with the volumes and `/etc/orcaster` purged is the snapshot
"clean": each VPS in the fleet enrolls from scratch with the multi-use token and
receives its own identity. This warning is specific to the snapshot path; the
manual path
(normal bootstrap, no snapshot) is not affected, because each VPS is born without a
previous `orcaster-data` volume.

> Fleet code updates are done with
> [`ops/release/push-release.sh`](../../ops/release/), host by host, not with
> a snapshot rebake (unless something baked into the
> snapshot itself also changes, such as the system user or the packages of `00-preflight.sh`).
> Server-driven `self_update` was removed from the contract; updates
> are operator-driven.

## Post-deploy steps (place the token and start)

```bash
# 0. `cd /`: sudo inherits the caller's cwd, and from a root SSH session
#    (cwd /root, mode 700) make and podman die with "cannot chdir" before
#    running, even with `make -C`. Applies to all the steps below.
cd /

# 1. Enrollment token (server dashboard → workspace → Add a worker):
$EDITOR /etc/orcaster/env     # ORCH_ENROLLMENT_TOKEN=<token>

# 2. Start the worker (rootless, as 'orcaster'). make resolves
#    /etc/orcaster/env[.N] and /etc/orcaster/server-ed25519.pub on its own (host
#    config exists); WORKER_ENV/WORKER_PUBKEY are not needed. If CONFIG_DIR is not
#    /etc/orcaster, also pass CONFIG_DIR=<dir> and ETC_SETUP_ENV_FILE=<dir>/setup.env.
sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$(id -u orcaster) \
     make -C /opt/orcaster up client

# 3. Enable autostart at boot (after the start):
sudo bash /root/orcaster/setup-scripts/debian13-worker/06-autostart.sh

# 4. Logs:
sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$(id -u orcaster) \
     make -C /opt/orcaster logs client
```

## Measuring fleet disk against the gate (pre-rollout)

`fleet-disk-check.sh` walks every fleet host over SSH and measures, per
worker, the free space of its `cache_root` **with the same criterion as the
worker's disk gate** (a measurement gate before the
rollout). It does not use the dashboard's `disk_mb`/`disk_used_mb`:
that pair does not reproduce the gate's criterion near the threshold (`used`
excludes the filesystem's reserved blocks, so `used + free != total`
exactly where it matters). It measures exactly what the worker measures:
`free/total` in bytes via `statvfs` (`f_bavail`, not `f_bfree`) over the
effective `cache_root` of each container, compared against
`ORCH_ORCASTER_CACHE_CLAIM_MIN_FREE_FRACTION` (if set, any
value) or otherwise `ORCH_ORCASTER_CACHE_MIN_FREE_FRACTION` (default `0.20`).

It is **read-only**: it deletes no cache, restarts no containers and modifies
no host.

```bash
# One host per line (user@host or host); '#' comments out, empty lines are ignored.
cat > fleet-hosts.txt <<'EOF'
root@worker-vps-1.example.com
root@worker-vps-2.example.com
EOF

bash setup-scripts/debian13-worker/fleet-disk-check.sh --hosts-file fleet-hosts.txt
```

It also accepts hosts as positional arguments or through the `FLEET_HOSTS`
variable (separated by spaces); there is no hardcoded host list in the repo, so
one must be provided through one of these routes. `--worker-user` changes the
user that runs rootless Podman on each host (default `orcaster`); see
`--help` for the other options.

Output per worker: host, container, `cache_root`, total, free, free
fraction, effective floor and a readable verdict: OK; below the floor, reported
as a failure; OK with the gate disabled when the effective floor is `0`; not
measured when the measurement was not possible; or not measured because the
container is stopped, when the worker exists but is not running. A host
unreachable over SSH is reported as such, never as "0 % free", and does not
abort the run for the rest of the fleet.

Exit codes, meant to be used as a rollout gate: `0` the whole measured
fleet has headroom; `1` at least one worker is below the floor (the real
gate is closed or would close there); `2` no worker is below the
floor, but the full state could not be confirmed (unreachable host, stopped
worker, or lost measurement on some worker — the real gate does not close without a
measurement, fail-open, but this check cannot vouch for the fleet either).

Before running it against the real fleet, verify it locally (without touching
any host): `bats setup-scripts/debian13-worker/tests/test_fleet_disk_check.bats`
stubs `ssh` and exercises the real floor-resolution logic and the `statvfs`
parsing (including a run against a real filesystem of the machine
running the tests, without mocking `python`).

**Fleet coverage:** the per-host listing uses `podman ps -a`, so it
includes both running and stopped containers. A worker that
exists but is not running is reported with its own row (not measured, container
stopped) and counts as incomplete coverage: the script exits
with `2`, never `0`, if any known worker was left unmeasured. This is
automatic — it does not depend on the operator comparing the number of rows
against any `WORKER_COUNT` by hand. Residual limitation: a completely
removed container (not just stopped) remains undetectable without an external
inventory of the expected workers per host; the script does not assume one
because no such inventory is declared in the repo.

## Notes

- **Rootless + reboot:** `make up client` starts with `--restart unless-stopped`,
  but `podman-restart.service` filters on `restart-policy=always` and does **not**
  restart that container at boot. That is why `06-autostart.sh` generates a
  `systemd --user` unit (`podman generate systemd` in adoption mode: the unit
  does `podman start`/`stop` of the existing container, without recreating it) and
  enables it on top of `loginctl enable-linger`: that is the mechanism that guarantees
  startup at boot (verified with a real reboot).
- **Egress isolation + reboot:** under `EGRESS_ISOLATION=1` the worker
  only has egress through the `orcaster-proxy$(SUFFIX)` sidecar, which is brought up
  with `--restart unless-stopped` (`podman-restart.service` does not restart it either).
  `06-autostart.sh` detects that sidecar and **also** persists its
  `systemd --user` unit, and generates the worker's with `--after`/`--wants` on the
  proxy's: at boot the proxy starts first and the worker is not left on the internal
  network with the proxy down (no egress). The adoption unit embeds a `PIDFile` with the
  container ID; when the worker is recreated with `EGRESS_ISOLATION=1` the ID changes, so
  the unit must be regenerated (otherwise systemd would fail at boot looking for the
  `PIDFile` of a container that no longer exists, leaving the worker unstarted despite
  `ORCH_ENFORCE_ISOLATION=true` in the `.env`).
- **`make up client` keeps autostart in sync:** when the host already has
  autostart enabled (the `container-$(WORKER_NAME).service` unit exists),
  `make up client` stops the unit before recreating (avoids the
  "container name is already in use" race against systemd's `Restart`) and regenerates
  that unit when done (`AUTOSTART_SYNC` hook, active by default). The
  stop is skipped only when the unit is at rest (`inactive`, `failed` or `unknown`); `active`,
  `activating` (which includes the automatic restart of a worker in a crash loop),
  `deactivating` and `reloading` are stopped. This way, recreating
  the unsuffixed worker with `make up client EGRESS_ISOLATION=1` leaves a state that
  **survives reboots** with egress isolation intact, without a separate manual
  step. It is a no-op on machines without that autostart (dev/CI). It can be disabled with
  `AUTOSTART_SYNC=0` (used by `run-fleet.sh`, which already runs `06-autostart.sh`
  explicitly after each recreation); it turns off the probe, the stops and the
  re-sync. `AUTOSTART_RESYNC=0` turns off only the final re-sync
  and keeps the probe and the stops (used by `install-release.sh`, point 3).
  `AUTOSTART_SYNC_FATAL` (default `1`) decides what happens if the
  re-sync fails (point 3).

  Both steps talk to the worker user's `systemd --user` manager, so
  `make up client` must be run **as the worker user** (`orcaster` in
  production), not as `root`. Its behavior with respect to that manager is:

  1. **Derives the environment if missing.** A `sudo -iu orcaster` shell does not open
     a logind session and does not bring `XDG_RUNTIME_DIR` or `DBUS_SESSION_BUS_ADDRESS`
     ([pam_systemd(8)](https://www.freedesktop.org/software/systemd/man/latest/pam_systemd.html)).
     Before stopping the unit, `user-bus.sh` sets `XDG_RUNTIME_DIR=/run/user/<uid>`
     if that variable is not defined and that directory exists, and
     `DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus` if that one is not
     defined either. A variable that is already defined is not modified.
  2. **Aborts before recreating anything if the manager does not respond.** With the
     environment resolved, `make up client` probes the manager (`systemctl --user
     show-environment`) as a prerequisite that runs before `up-proxy`, for the
     worker's unit and, with `EGRESS_ISOLATION=1`, also for the sidecar's. If
     it does not answer, it exits non-zero with a message that names the cause and
     the remedy, and recreates nothing, neither the worker nor its egress sidecar (it cuts off before
     the build even if only the sidecar's unit exists). A connection failure with
     the bus is not interpreted as "unit inactive". A `systemctl --user stop`
     that fails also aborts before the container is removed; if the sidecar had already
     been recreated in the same command, the error says so. `install-release.sh`
     does the same probe before the code swap (only if the worker user's home
     has any `container-orcaster-worker*.service` or
     `container-orcaster-proxy*.service` file) and, if the manager does not respond, aborts with
     the installed code intact.
  3. **A failed re-sync is an error.** If `06-autostart.sh` fails to
     regenerate the unit, `make up client` prints how to re-sync by hand and
     exits non-zero. In that case the container has already been recreated and
     is running, but the unit is not in sync with it: until it is re-synced,
     do not run `systemctl --user daemon-reload` on that host (see "Recovering a
     worker with an out-of-sync unit", at the end of this document).
     `06-autostart.sh` probes the manager before rewriting the unit, both with
     `RUN_AS=` (where it derives the same environment) and through the `as_worker` path: if
     the manager does not respond, it ends with an error without regenerating anything. With
     `AUTOSTART_SYNC_FATAL=0` that failure only prints the warning and `make up client`
     exits 0 (for whoever recreates several workers in a loop and verifies each unit
     on their own). `ops/release/install-release.sh` passes `AUTOSTART_RESYNC=0`
     (not `AUTOSTART_SYNC=0`, which would also turn off the probe and the stops of
     `user-bus.sh`), because on a tarball-installed host the Makefile tree is
     `INSTALL_DIR` and the source guard of `lib.sh`
     (`require_source_outside_install_dir`) would reject the hook. It also passes
     `AUTOSTART_SYNC_FATAL=0`: `push-release.sh` sends the current installer with
     the tarball of the requested `--ref`, and the Makefile of an earlier release (a
     rollback to an older release) does not know `AUTOSTART_RESYNC`, runs the hook,
     hits the guard and so only warns. Before the swap, if there are autostart units, it
     requires that the `WORKER_USER` and `WORKER_HOME` that `load_env` would resolve
     (with what `setup.env` declares, even empty, and the defaults of
     `lib.sh`) are those of the deployment (the user and its `passwd` home). Its
     pre-stop stops every unit that is not at rest (also `activating` and
     `deactivating`). After each successful `make up client`, if the worker unit's
     file exists in `~/.config/systemd/user/`, it runs as root
     `env CONFIG_DIR=<dir> N=<i> WORKER_USER=<u> WORKER_HOME=<home>
     [EGRESS_ISOLATION=<v>] bash $SOURCE_DIR/setup-scripts/debian13-worker/06-autostart.sh`
     (the root copy, by default `/root/orcaster`), like
     `run-fleet.sh`. If that call fails it only warns, so as not to cut the loop (the
     following workers would be left on inodes that have already been replaced), but it counts
     the worker's units as down; the final check exits non-zero
     and names the remedy. Running `make up client` by hand from `INSTALL_DIR` with autostart
     enabled still hits the guard and prints the remedy as root. The
     probe of point 2 does not depend on that variable.
  4. **Requires linger.** The user's manager exists without an open session only if
     the user has linger ([loginctl(1)](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)):
     `00-preflight.sh` enables it with `loginctl enable-linger orcaster`. Without linger
     `/run/user/<uid>` does not exist, there is nothing to derive and the command
     stops with an error that names `loginctl enable-linger`.
  5. **The error distinguishes three causes.** (a) `/run/user/<uid>` does not exist: linger
     is missing, and the remedy is `sudo loginctl enable-linger orcaster`. (b) The
     variables were derived but the manager does not respond: the remedy is
     `sudo systemctl status user@<uid>.service` and, if it is down,
     `sudo systemctl start user@<uid>.service`. (c) The variables came from the
     caller's environment and do not work: retry from `sudo -iu orcaster`
     without them. The `sudo` remedies are run from an administrator account,
     not from `orcaster`, which normally has no sudo. If the host no longer
     uses autostart at boot, the unit file
     (`~/.config/systemd/user/container-<name>.service`) is a leftover and removing
     it turns this step back into a no-op.

  Without the unit file, with `AUTOSTART_SYNC=0` or without `systemctl`, the
  manager is not queried. Supported invocations on a host with linger: open a shell
  with `sudo -iu orcaster` and run `make up client` from there, or the explicit form
  `sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$(id -u orcaster) make ...`, the
  same one used by the commands above.
- **`make up proxy` is not run standalone under autostart:** `up-proxy` stops its
  unit before recreating the sidecar (avoids the race with `Restart`; same
  at-rest state rules as the worker), with
  the same requirement on the `systemd --user` manager as `make up client` (if it does not
  respond, or the stop fails, it aborts with an error before touching the sidecar), but
  does **not** regenerate that unit; the proxy's re-sync is coupled to
  `make up client` (`AUTOSTART_SYNC` hook, which regenerates worker + proxy together via
  `06-autostart.sh`). Recreating only the sidecar with `make up proxy` on a host with
  autostart enabled would leave its `systemd --user` unit stopped and pointing at the
  `PIDFile` of the old container → it would fail on the next boot. To recreate the
  sidecar under egress isolation use `make up client EGRESS_ISOLATION=1` (which goes
  through `up-proxy` as a dependency and re-syncs both units at the end), or
  re-sync by hand with `sudo bash 06-autostart.sh` (from
  `/root/orcaster/setup-scripts/debian13-worker`) after `make up proxy`.
- **Egress isolation is the authoritative default:** `make up client` (and `run-fleet.sh`)
  bring the worker up with the egress topology enabled by default
  (`EGRESS_ISOLATION=1`). The opt-out `EGRESS_ISOLATION=0` disables it entirely
  (dev/CI without `aardvark-dns >= 1.11.0` or sidecar). `run-fleet.sh` propagates the flag to
  each worker `N=1..WORKER_COUNT` and stops the proxy-N unit before recreating.
  Only the values `0` and `1` are accepted (`load_env` aborts on any other: the
  `Makefile` requires an exact match of `1`, and a different "truthy" value would disable
  isolation silently).
  Topology note: `run-fleet` models the fleet as `worker-1..N` (all suffixed).
  Beware of the opt-out on a fleet that was already running under egress: it does not dismantle the
  pre-existing proxy-N (the container still exists, and `06-autostart.sh`, which
  detects by presence, persists its unit again). Remove it by hand:
  `systemctl --user disable --now container-orcaster-proxy-N.service` +
  `podman rm -f orcaster-proxy-N` as the worker user.
- **Worker identity:** `worker_id`/`api_key` are persisted in the `orcaster-data`
  volume after the first registration; the worker does not re-enroll on later
  starts. To force a new registration: `make purge client` (deletes the volume).
- **Reruns:** `run-all-worker.sh --from 03` (from
  `/root/orcaster/setup-scripts/debian13-worker`) resumes from the
  `.env` generation. The `.env` preserves the existing `ORCH_ENROLLMENT_TOKEN`.
- **Multiple workers per host:** see `make up client N=2` in the root Makefile
  (each one needs its own token); these scripts provision the base worker.

### Recovering a worker with an out-of-sync unit

A worker becomes out of sync when its container was recreated without the
`systemd --user` manager noticing: the manager still has the old unit loaded and a
`MainPID` that no longer exists. The worker runs normally until someone
runs `systemctl --user daemon-reload`, which reconciles that difference by stopping the
container. It is the state in which a recreation with `make up client` whose
last step failed ends up (the command exits non-zero and says so; with
`AUTOSTART_SYNC_FATAL=0` it only says so; in a deployment through `install-release.sh` the
script itself reports it and exits non-zero at the end).

The commands are run as the worker user, with the bus variables
defined. `<n>` is the worker index; for the unsuffixed worker, omit `-<n>`.

1. **Diagnose:** compare the `MainPID` that the manager has with the container's
   monitoring process (conmon).

   ```bash
   cd /
   uid=$(id -u orcaster)
   sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$uid \
       DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus \
       systemctl --user show -p MainPID --value container-orcaster-worker-<n>.service
   sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$uid \
       podman inspect -f '{{.State.ConmonPid}}' orcaster-worker-<n>
   ```

   If both numbers match, the unit is in sync and there is nothing to
   do. Repeat the comparison with `container-orcaster-proxy-<n>.service` and
   `orcaster-proxy-<n>` if the host runs with egress isolation.

2. **If they differ, do not run `systemctl --user daemon-reload` on its own.** That
   command is the one that stops the worker.

3. **Pause or wait.** The re-sync may restart the worker and its proxy.
   Pause the worker from the dashboard (soft drain) or wait until it has
   no task running.

4. **Re-sync** the unit, with the host's real configuration directory
   (the one that contains its `setup.env`):

   ```bash
   sudo env CONFIG_DIR=<host-config-dir> N=<n> \
       bash /root/orcaster/setup-scripts/debian13-worker/06-autostart.sh
   ```

   For the unsuffixed worker, also drop the `N=<n>` word from the command: the
   rule of omitting `-<n>` covers the unit names, not this variable.

5. **Verify** again that the two PIDs match (step 1) and that the container
   is running:

   ```bash
   sudo -u orcaster env XDG_RUNTIME_DIR=/run/user/$(id -u orcaster) podman ps
   ```

   If the unit was left stopped, start it as the worker user, with the bus
   variables defined:
   `systemctl --user start container-orcaster-worker-<n>.service`. Resume the worker
   in the dashboard if you had paused it.
