# Server deployment on Debian 13 (Trixie)

Provisioning scripts to install and configure the orchestrator **server** on a
VPS with a **fresh base Debian 13 install**. They do not install workers: those
run on other machines as an OCI container and connect through `ORCH_BASE_URL`.

## What it sets up

- **PostgreSQL 17** (Trixie base repos; the only infrastructure dependency).
- **uv**, system-wide, plus the server's virtual environment.
- The **Ed25519** task signing key.
- A **systemd** service (`orcaster.service`) that keeps `uvicorn` running.
- **Caddy** as a reverse proxy with **automatic HTTPS** (Let's Encrypt).
- A **firewall** (ufw: 22/80/443) and **sshd hardening** (key-only login).

> **WebAuthn requires HTTPS with a real domain.** The dashboard uses passkeys;
> without TLS and a `DOMAIN` that resolves through DNS to this VPS, login does
> not work.

## Prerequisites

1. A VPS with Debian 13 and SSH access (ideally with your public key already
   uploaded, via `ssh-copy-id`, so that step 07 can harden SSH without locking
   you out).
2. A **domain** with an `A` record pointing to the VPS public IP:
   ```
   orcaster.yourdomain.com.  A  <VPS_IP>
   ```
   Check it with `dig +short orcaster.yourdomain.com` before the Caddy step.

## Where the installer runs: the root checkout and `INSTALL_DIR`

The scripts in this folder run **as root**. For that reason they are not run
from `INSTALL_DIR` (`/opt/orcaster`), which `03-app-install.sh` leaves
owned by the service user (`APP_USER`, `orcaster`): if a server process were
compromised, it could edit `lib.sh` or a script in that tree and the next
installer run would execute it as root. Root must not execute code that the
service user can write.

The supported layout separates two trees:

| Directory | Owner | What it is |
|-----------|-------|------------|
| `/root/orcaster` (checkout) | `root:root`, no group or other write | Source of the installer. The scripts, the `Makefile`, `setup-scripts/debian13/setup.env` and its backups (`setup.env.<suffix>`, owned by root, mode 600) live here. Everything is run from here. |
| `/opt/orcaster` (`INSTALL_DIR`) | `APP_USER` | The copy the service runs, with `server/.venv`, `server/.env` and `secrets/`. |

`03-app-install.sh` always copies the checkout to `INSTALL_DIR` and leaves
`INSTALL_DIR` owned by `APP_USER`. The checkout never changes owner. The copy
is done in two steps: root copies the checkout to a private staging directory
under `/tmp` (mode `750`, group `orcaster`) and then `APP_USER` syncs it to
`INSTALL_DIR` with `rsync --delete` and the same exclusion list as always. After
the rsync, `secrets/`, the Ed25519 key and `server/.env` are also written by
`APP_USER`, never by root.

> `/root` is `0700` on Debian: the commands in this guide are run as root
> (`sudo -i`, or direct SSH as root). A leading `sudo` is harmless if you are
> already root.
>
> The installer needs `sudo` even when you run as root: it uses it to run
> commands as `APP_USER` and as `postgres` (`sudo -u`). A minimal Debian 13 does
> not ship it; `00-preflight.sh` installs it with the base packages. If it is
> missing when you run another script on its own, that script stops with an error
> that names `sudo`.
>
> **`fs.protected_hardlinks=1` is mandatory** (it is the Debian default).
> `03-app-install.sh` runs `chown -R` as root over `INSTALL_DIR`, which
> `APP_USER` controls: `chown -R` does not follow symbolic links, but it does
> change the owner of a hardlink's target. With the sysctl set to `0`, that user
> could hardlink a root-owned file and get root to hand it over.
> `00-preflight.sh` reads `sysctl -n fs.protected_hardlinks` and stops if it is
> not exactly `1` (or cannot be read). Fix: `sysctl -w fs.protected_hardlinks=1`,
> persist it in `/etc/sysctl.d/60-protected-links.conf`
> (`fs.protected_hardlinks = 1`) and run again.

### Source tree guard

When `lib.sh` runs as root, it aborts **before any other effect** if:

- `SCRIPT_DIR` (`setup-scripts/debian13` of the checkout) or any ancestor
  directory up to `/` is not owned by root, has group or other write, or has an
  ACL;
- `lib.sh`, `run-all.sh`, any other `*.sh` of the suite, the `Makefile` at the
  repo root or `setup.env` is not a regular file owned by root without group or
  other write (symbolic links are rejected);
- the script was invoked through a path that goes through symbolic links (run it
  by its real path);
- when loading `setup.env`, the checkout and `INSTALL_DIR` are the same
  directory or one is inside the other.

The error names the offending path, its owner uid and its mode, and repeats the
supported layout. Its form is:

```
ERROR Source guard: the installer runs as root and does not execute code that
another user can write. <path> has owner uid <N> and mode <mode> (required:
owner root, no group or other write, no ACL). Supported installation: a root
checkout (e.g. /root/orcaster, chown -R root:root and chmod -R
go-w), separate from INSTALL_DIR […]
```

Running from `/opt/orcaster` aborts with this error. No environment
variable relaxes the guard, and it does not repair a tree that was already
modified: it only prevents running from one that `APP_USER` can modify.

The guard does not apply when the process is not root. `08-smoke-test.sh`, which
does not need root, keeps running without it.

## Usage

### 1. Upload the code to the VPS

From your machine (either way):

```bash
# rsync option (does not require git on the VPS):
rsync -a \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    --exclude '*.png' \
    --exclude 'server/.env' \
    --include 'setup-scripts/debian13/setup.env.example' \
    --exclude 'setup-scripts/debian13/setup.env*' \
    --exclude '*.env' \
    --exclude 'secrets' \
    ./ root@vps:/root/orcaster/

# …or git clone, as root, on the VPS if there is already a remote:
#   git clone <repo-url> /root/orcaster
```

After each upload, leave the checkout as the guard requires (`rsync -a`
preserves the owner and mode of the source):

```bash
chown -R root:root /root/orcaster && chmod -R go-w /root/orcaster
```

> ⚠️ **Never sync `server/.env`, `setup-scripts/debian13/setup.env` (or its
> backups `setup.env.<suffix>`), any `*.env` or `secrets/`.** Those files are
> the configuration and keys **of the VPS** (DB password, Ed25519 key, domain).
> `'*.env'`, without a slash, also excludes any other env files you keep in
> the tree, by name at any depth. If
> your local copy has them with development values and you push them, you
> overwrite production: the server stops starting
> (`asyncpg.InvalidPasswordError`) and passkey login breaks (wrong RP_ID). The
> `--exclude` options above protect them.
>
> **To redeploy only code changes** (not the first install), update the root
> checkout (rsync from your machine, or `git pull` as root, plus the
> `chown`/`chmod` above) and re-run the install step **from the checkout**. That
> step copies the checkout to `INSTALL_DIR` (rsync as `APP_USER`) preserving
> secrets and dependencies:
>
> ```bash
> cd /root/orcaster/setup-scripts/debian13 && sudo bash 03-app-install.sh
> sudo systemctl restart orcaster.service
> ```
>
> (`sudo bash run-all.sh` from the same directory also works.)
>
> **Note:** that incremental redeploy does NOT rewrite the systemd unit. If you
> changed `SERVER_START_TIMEOUT` (or another directive of `05-systemd.sh`), also
> run `sudo bash 05-systemd.sh` once: it rewrites the unit and runs
> `daemon-reload`. The exception is the uvicorn module in `ExecStart`: 03 aligns
> it by itself with the installed package (see "Server package and unit" below).
>
> **From your machine, with `deploy.sh`.** `setup-scripts/debian13/deploy.sh`
> does that same redeploy over SSH: it uploads `git archive <ref>` (default
> `HEAD`; only what is committed is sent) to the root checkout with
> `rsync --delete` (preserving `setup.env` and its backups), leaves it
> `root:root` without group or other write, runs 03, aligns the unit's
> `ExecStart` with the `server-layout.sh` sent from your checkout, restarts the
> service and runs the smoke test. The configuration goes in `deploy.local.env`
> at the repo root (outside git; a template is created on the first run) or at
> the path in `DEPLOY_ENV`. The `VPS_SSH` user needs passwordless `sudo` (or to
> be root).
>
> ```bash
> setup-scripts/debian13/deploy.sh --dry-run        # dry-run rsync to the checkout
> setup-scripts/debian13/deploy.sh --ref <tag>      # deploy a tag
> setup-scripts/debian13/deploy.sh --source /tmp/x  # an already extracted tree
> ```
>
> The tree it sends leaves out the documentation and other paths not needed at
> runtime (list in `deploy_omitted_paths` in `deploy.sh`), since the server
> could be handed over to a third party. Both rsync calls use `--delete`, so
> those paths are also removed from the host if an earlier deploy left them.
>
> After the restart it watches the service for `DEPLOY_STABLE_SECONDS` seconds
> (default 20; environment variable or `deploy.local.env`): it fails if the
> service stops being active or if systemd restarts it (crash loop), and then it
> runs `08-smoke-test.sh`, which retries the local `/healthz`. Either failure
> shows the journal and ends with an error. With `--no-smoke` only the stability
> check remains.
>
> There is no `--fast` mode: code only reaches `INSTALL_DIR` through 03. Do not
> use `--migrate` in production (the schema migrates at startup).

#### Server package and unit

The server's Python package is `orcaster_server`. `server-layout.sh` brings the
host in line with the package that the installed tree ships:

- `align-unit`: changes only the `uvicorn <package>.main:app` module in the
  `ExecStart` of `orcaster.service`, with a backup in `/var/backups/orcaster/`
  and `daemon-reload`. It does not touch the drop-ins (`orcaster.service.d/`,
  e.g. `artifacts.conf`). An `ExecStart` it does not recognize is left as is and
  the command fails. To undo it: `sudo bash server-layout.sh restore-unit <backup>`.
03 runs it on every run. If you place files by hand in
`server/orcaster_server/static/` that are not in git, declare them in
`setup.env` (`HOST_ONLY_STATIC="robots.txt"`): without that line the rsync of
03 deletes them.

### 2. Configure

On the VPS, inside the root checkout:

```bash
cd /root/orcaster/setup-scripts/debian13
install -m 600 -o root -g root setup.env.example setup.env
$EDITOR setup.env        # set DOMAIN and ADMIN_EMAIL; leave DB_PASSWORD empty
```

`setup.env` lives in the checkout, owned by root with mode `600`: root sources
it, and the guard aborts if it is not owned by root or has group or other write.
The upload's `--exclude` keeps an rsync from overwriting it.

### 3. Run

With the `Makefile` (install `make` if missing: `apt-get install -y make`):

```bash
cd /root/orcaster
sudo make setup-server debian13
```

Or directly, without make:

```bash
cd /root/orcaster/setup-scripts/debian13
sudo bash run-all.sh
```

The runner executes `00 → 08` in order. It is **idempotent**: you can re-run it.
You can also run a single numbered script from that same directory.

## Scripts (execution order)

| # | Script | What it does |
|---|--------|--------------|
| 00 | `00-preflight.sh` | apt update/upgrade, base packages (includes `sudo`), system user |
| 01 | `01-postgresql.sh` | PostgreSQL 17, role + DB, autogenerated password |
| 02 | `02-python-uv.sh` | uv in `/usr/local/bin` |
| 03 | `03-app-install.sh` | copy of the root checkout to `INSTALL_DIR` (private staging in `/tmp` and rsync as `APP_USER`), `uv sync --frozen` (aborts if `server/uv.lock` is missing), `PLUGIN_PACKAGES` plugins and `ORCASTER_PLUGINS` verification (see "Server plugins"), Ed25519 key, `server/.env` |
| 04 | `04-migrate.sh` | `alembic upgrade head` |
| 05 | `05-systemd.sh` | `orcaster.service` unit, enable + start |
| 06 | `06-caddy.sh` | Caddy + automatic HTTPS → `127.0.0.1:8000`, with a per-route body limit (see "Body limits"). Renders the Caddyfile to a temporary file, validates it with `caddy validate` and only then replaces the current one (if it fails, the current one stays intact); keeps the previous one as `Caddyfile.prev` and restores it if caddy cannot reload or restart |
| 07 | `07-firewall-ssh.sh` | ufw (22/80/443) + sshd hardening |
| 08 | `08-smoke-test.sh` | checks the local `/healthz` and public HTTPS |

### Body limits

The app and the Caddyfile that 06 generates bound a request body with the same
numbers. Five variables set them (bytes):

| Variable | Default | What it bounds |
|----------|---------|----------------|
| `MAX_REQUEST_BODY_BYTES` | `4194304` (4 MiB) | every request without its own rule |
| `MAX_OUTPUT_BODY_BYTES` | `33554432` (32 MiB) | `POST /v1/tasks/{id}/output` |
| `ARTIFACT_UPLOAD_OVERHEAD_BYTES` | `8388608` (8 MiB) | multipart overhead of the artifact upload |
| `ARTIFACT_MAX_TOTAL_BYTES` | `209715200` (200 MiB) | total per execution; together with the other two it sets the limit of `POST /v1/tasks/{id}/artifacts`: `max(TOTAL, FILE) + OVERHEAD` |
| `ARTIFACT_MAX_FILE_BYTES` | `209715200` (200 MiB) | one file; same |

- **Optional**: the first four do not need to be declared (they are commented
  out in `setup.env.example`); when undeclared, the app default applies.
  `ARTIFACT_MAX_FILE_BYTES` is always written, with its 200 MiB default.
- **Propagation**: `03-app-install.sh` writes all five to `server/.env`, which
  is what the app reads. The four optional ones only if `setup.env` declares
  them, never a blind default. Since `server/.env` is regenerated in full on
  every run of 03, a manual adjustment of these keys there is lost on re-running
  03 unless it is also in `setup.env`.
- **Precedence** when rendering the Caddyfile in 06: `server/.env` > `setup.env`
  > default. `server/.env` wins because it is what the app really applies; so,
  after a manual adjustment there, re-running 06 (`sudo bash 06-caddy.sh`) is
  enough for the proxy to follow the app. 06 reads `server/.env` with the same
  forms the app accepts (python-dotenv): `KEY=v`, `export KEY=v`, leading
  indentation, `KEY = v`, quotes, CRLF and a trailing ` # ...` comment; if
  there are several lines, the last one wins. A line that mentions the key
  without being an assignment (`KEY: v`) is ignored with a warning, and a value
  that does not reduce to an integer aborts.
- **Key only in `setup.env`**: if one of the four optional ones is in
  `setup.env` but not in `server/.env` (03 was not re-run), 06 emits a `warn`
  that names the key: Caddy would use the `setup.env` value while the app keeps
  its default. It does not abort; re-run 03 so the app matches.
- **Validation**: each one must be a positive integer of digits only. `4MB`
  (Caddy would read it as 4,000,000 B, not 4 MiB), an empty value, `0` or one
  with leading zeros abort with a message that names the variable and the source
  (`setup.env` or `server/.env`), without touching the current Caddyfile. The
  app applies the same to `ARTIFACT_UPLOAD_OVERHEAD_BYTES` (`>= 1`): with 0, a
  file of the maximum size would receive `413` because of the multipart framing.
- **Applying a change** in `setup.env` (the app reads `server/.env` at startup):

  ```bash
  sudo bash 03-app-install.sh && sudo bash 06-caddy.sh
  sudo systemctl restart orcaster
  ```

### Server plugins

The core ships no task-specific code: plugins provide it (entry points of the
`orcaster.plugins` group). `uv sync --frozen` is exact and removes from the venv
any package that is not in `server/uv.lock`, so a plugin installed by hand
disappears on the next run of 03. For that reason they are declared in
`setup.env` (see `setup.env.example`; each plugin's README documents the real
values):

- `PLUGIN_PACKAGES`: pip requirements separated by spaces, with no internal
  spaces. Only three forms are accepted, all of fixed content:
  `name@git+https://host/org/repo@<40-hex-sha>` (full lowercase commit SHA: a
  tag or a branch can move and is rejected), `name==1.2.3` (exact version) and
  the absolute path of a wheel (`/path/x.whl`). Anything else (`git+http`, no
  name, no ref or a branch or tag ref, a name without `==`, `>=`, credentials or
  a query string in a URL) aborts with an error that names the requirement. 03
  reinstalls them on every run right after `uv sync --frozen`, with the lock's
  versions as `--constraint`: if a plugin requires a different version of a
  locked dependency, the run aborts.
  `git+https` requirements are fetched first with `git` as root, into a root
  temporary directory (mode 700, always in `/tmp` even if `TMPDIR` points
  elsewhere, outside `INSTALL_DIR`, deleted when done), with an empty
  environment (no exported variables or functions; if anything is left
  exported, git does not run) and without reading any git config
  (`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, empty `HOME`). The
  checkout's `.git` is deleted, the tree is copied to a temporary directory of
  the app user and uv installs it, as that user, as a local directory.
- `PLUGIN_GIT_TOKEN_FILE`: for a private repository on github.com, the absolute
  path to a file with a read-only token (fine-grained PAT, Contents: read),
  owned by root, mode 600 and outside the repo and `INSTALL_DIR`. The token is
  handed only to root's `git`, through an environment variable and via an
  askpass that `lib.sh` writes in root's temporary directory (it answers only
  for `https://github.com`); never to an app-user process, to uv or to the build
  backends, never in argv, in files or in the output. Credentials inside the URL
  are rejected.
- `ORCASTER_PLUGINS` (a server variable) travels through
  `SERVER_ENV_PASSTHROUGH`. 03 aborts if it is declared and not in that list, or
  if it names a plugin that did not end up installed; the server also refuses to
  start in that case.

Any failure in these steps stops the deployment at the step that failed (fails
closed). `uv run` (systemd unit, 04, 09) syncs inexactly and does not remove the
plugins; a manual `uv sync --frozen` on the host does: to restore them, run
`03-app-install.sh` again. No other deployment path runs `uv sync`.

### Runner flags

Run them from `/root/orcaster/setup-scripts/debian13`:

```bash
sudo bash run-all.sh --skip-hardening   # skips 07
sudo bash run-all.sh --skip-caddy        # skips 06 (external TLS: Cloudflare, LB…)
sudo bash run-all.sh --from 04           # resumes from script 04
```

> **Cloudflare + public status page**: if the domain goes through Cloudflare,
> the cache and rate limiting rules for `/status/*` are a security
> precondition, not an option — without them every anonymous visitor hits
> Postgres directly. Both rules (cache and rate limiting) are in the user
> manual, [Monitoring the system](../../docs/user-manual.md#monitoring-the-system).

> **Access logs and `/api/v1/secrets/`**: 05 runs uvicorn with
> `--no-access-log` and the Caddyfile from 06 has no `log` directive, so neither
> writes paths. A proxy, CDN or WAF in front (or a `log` added to the Caddyfile)
> **must not log the paths of `/api/v1/secrets/`**: a secret's name travels in
> the URL of `PUT` and `DELETE`, and a token pasted there by mistake would end
> up in that log.

## Bootstrapping the first owner

There is no public registration route. The first owner is created by issuing an
invitation that prints a `/auth/accept/{token}` link (valid ~15 min):

```bash
cd /root/orcaster
sudo make bootstrap debian13                  # adopts the Default workspace (idempotent)
# or a new named workspace (not idempotent):
sudo make bootstrap debian13 ARGS="--name Acme"

# equivalent without make:
sudo bash setup-scripts/debian13/09-bootstrap-owner.sh
```

Open the printed link in the browser, register your passkey and you will be the
**owner**. Afterwards you sign in at `https://<DOMAIN>/auth/login` with that
passkey.

## After deployment

- **Dashboard**: `https://<DOMAIN>` — passkey login (after the bootstrap above).
- **API docs**: `https://<DOMAIN>/docs` (requires a dashboard session; `/openapi.json` also accepts a PAT).
- **Server public key**: `/opt/orcaster/secrets/ed25519.pub`. Copy it to
  each worker as `server-ed25519.pub` (without it, the worker rejects every task).
- **Logs**: `journalctl -u orcaster.service -f`.
- **Restart**: `systemctl restart orcaster`.

## Notes

- `setup.env` and `secrets/` are **not versioned** (they contain credentials and
  the private key). Back them up: losing `ed25519.pem` invalidates all workers.
  `setup.env` is in the root checkout (`/root/orcaster/setup-scripts/debian13/`);
  `secrets/` is in `INSTALL_DIR`.
- `setup.env` backups: next to it in the checkout, as `setup.env.<suffix>`
  (e.g. `cp -p setup.env setup.env.bak-$(date +%Y%m%d)`), owned by root and mode
  600, or outside both trees in a root-owned directory. Never in `INSTALL_DIR`:
  it belongs to `APP_USER`. The rsync of 03 excludes `setup-scripts/debian13/setup.env*`
  (except the template), so those backups are neither copied to `INSTALL_DIR`
  nor deleted. A fresh `git clone` of the checkout does not bring them: copy
  them first.
- Changing configuration → edit `setup.env` in the root checkout and run
  `run-all.sh` again from there. `server/.env` is regenerated; the Ed25519 key
  is never overwritten.
- `TASK_SECRETS_KEYS_PATH` (the task secrets key path, optional) is declared in
  `setup.env`, with single quotes if it uses `$CREDENTIALS_DIRECTORY`. If
  `server/.env` already defines it (with or without `export` and with spaces
  around `=`) and `setup.env` does not, `03-app-install.sh` stops without
  modifying `server/.env`, with a message that names both files and says it was
  not modified: regenerating `server/.env` without the path would leave tasks
  with stored secrets undelivered. If both define it with different values, it
  also stops without modifying `server/.env`, with a message that starts with
  `<path>/server/.env sets TASK_SECRETS_KEYS_PATH=<value>, but <path>/setup.env sets a different value: TASK_SECRETS_KEYS_PATH=<value>.`:
  regenerating `server/.env` would make the server load a different key file on
  its next restart and the stored secrets might no longer decrypt. To keep the
  current file, copy the `server/.env` value into `setup.env`; to switch files on
  purpose, first check that it contains the keys that encrypted the stored
  secrets and set the same new value in both files. With equal values, the
  script continues.
- The server also migrates the schema on every startup (`MIGRATE_ON_STARTUP=true`).
