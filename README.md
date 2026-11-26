# Orcaster

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![GitHub Issues](https://img.shields.io/github/issues/AInvirion/orcaster.svg)](https://github.com/AInvirion/orcaster/issues)
[![GitHub Pull Requests](https://img.shields.io/github/issues-pr/AInvirion/orcaster.svg)](https://github.com/AInvirion/orcaster/pulls)

> A self-hosted orchestrator for distributed tasks: one server with a PostgreSQL
> queue, and lightweight Python workers that run signed tasks inside hardened
> Podman containers.

## Table of Contents

- [About](#about)
- [Why the name Orcaster?](#why-the-name-orcaster)
- [Features](#features)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Production](#production)
- [Usage](#usage)
  - [Plugins](#plugins)
- [Known limitations](#known-limitations)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)

## About

Orcaster runs shell and Python tasks on a fleet of machines that you operate.
The server keeps the queue in PostgreSQL, signs every task with an Ed25519 key,
and hands tasks to workers through a single poll endpoint. Each worker runs as a
rootless Podman container on Linux, FreeBSD 15 or newer, or macOS (through a
Podman machine), verifies the signature of every task before it runs it, and
executes the task in an ephemeral `uv` virtual environment. Windows is supported
only through WSL2, as Linux, and is not an official target.

The project exists for teams that need a task queue across their own hosts with
one infrastructure dependency (PostgreSQL), no message broker, and explicit trust
rules: workers run only tasks signed by the server, inside a hardened rootless
container. See [SECURITY.md](SECURITY.md) for what a task can and cannot reach.

Orcaster has three parts:

- **Server**: a FastAPI application with a server-rendered dashboard (passkey
  login, workspaces, workers, tasks) and a JSON API.
- **Worker**: a Python agent packaged as an OCI image and run with Podman. The
  same agent is the Python package `orcaster-worker`; the server is the package
  `orcaster-server`.
- **CLI**: `orcaster`, a Go binary that operates a workspace through the JSON API
  with a personal access token.

## Why the name Orcaster?

The name Orcaster blends “orchestra”, “orchestrator” and “orc caster”, a nod to the days when I played a multi-target mage in an old mmorpg. And last but not least, it also plays on swapping the S and R in Oscar, a coincidence too fitting to pass up, making the name a small tribute of gratitude to a great friend, who gave me this opportunity.

## Features

- One infrastructure dependency: PostgreSQL 16 or newer. No Redis, RabbitMQ or
  Celery. Workers claim tasks with `SELECT ... FOR UPDATE SKIP LOCKED`.
- Every task is signed with Ed25519 by the server and verified by the worker
  before it runs.
- One worker endpoint, `/poll`, combines heartbeat, claim and cancellation.
- Fan-out per task with the `target` field (`"N"` for a positive integer,
  `"all-available"`, `"worker:<id>"`, `"workers:<id1>,<id2>"` and
  `"all:<id1>,<id2>"`), and eligibility by worker-declared capabilities
  (`eligibility.capabilities`, for example `model_context>=8192`; workers
  declare them with `ORCH_CAPABILITIES`).
- Opt-in sticky automatic retry (`auto_retry`; the default trigger is exit code
  75) with configurable backoff (the retry stays on the same worker to reuse its
  local cache), manual retry, and task forks with lineage.
- Workspaces with passkey (WebAuthn) login, invitations, enrolment tokens for
  workers and personal access tokens for the CLI and the API.
- Per-workspace task secrets, encrypted at rest and delivered to the tasks that
  reference them. They are enabled by setting `TASK_SECRETS_KEYS_PATH` on the
  server (a Fernet key file); without it the server answers 503 when you set a
  secret.
- Hardened worker container: all capabilities dropped, `no-new-privileges`,
  read-only root filesystem, bounded `tmpfs`, PID limit, optional memory and CPU
  limits.
- Per-task egress isolation: the worker reaches the network only through a proxy
  sidecar that applies a per-task allowlist (see
  [Known limitations](#known-limitations)).
- JSON API with OpenAPI at `/openapi.json`, `/docs` and `/redoc`, behind
  authentication.
- Database migrations with Alembic.

## Getting Started

The quick start below brings up a development server, one worker and one task on
a single host. Every command runs from the root of the repository unless the
step says otherwise.

### Prerequisites

| Component | Version | Notes |
|---|---|---|
| Python | 3.13 or newer | Server host. |
| [uv](https://docs.astral.sh/uv/) | latest stable | Manages the Python environments. |
| [Podman](https://podman.io/) (rootless) | 4.4 or newer | Runs the development database and the worker. Orcaster is Podman-only. |
| `aardvark-dns` | 1.11.0 or newer | Worker host, needed when egress isolation is on (the default for production; this quick start turns it off). On Debian: `apt-get install aardvark-dns`. |
| `netavark`, `nftables`, `passt` | recent | Worker host, needed when egress isolation is on: they provide its internal network. |
| PostgreSQL | 16 or newer | `make db-up` starts one in Podman for development. |
| OpenSSL | 1.1.1 or newer | Generates the server Ed25519 signing key. The LibreSSL that macOS ships in `/usr/bin/openssl` cannot: run `brew install openssl` and put it first on `PATH`. |
| GNU make | any | Use `gmake` on FreeBSD. |
| Go | the version in `orcaster/go.mod` | Only to build the `orcaster` CLI from source. |

On macOS, start the Podman virtual machine first:

```bash
podman machine init && podman machine start
```

### Installation

#### 1. Get the source

```bash
git clone https://github.com/AInvirion/orcaster.git
cd orcaster
```

#### 2. Start the database and point the server at it

```bash
make db-up
```

`make db-up` starts a PostgreSQL container and ends by printing an indented
line of the form `export DATABASE_URL='postgresql+asyncpg://...'`. Run that line
in your shell, and also write the URL to `server/.env` so that the server, the
migrations and the bootstrap script read it from any terminal:

```bash
export DATABASE_URL='<the URL printed by make db-up>'
echo "DATABASE_URL=$DATABASE_URL" >> server/.env
```

#### 3. Migrate, generate the signing key and start the server

```bash
make dev server
```

`make dev server` starts the database if needed, waits for it, applies the
migrations, generates the server's Ed25519 key pair if it does not exist, and
starts the server with hot reload on `http://0.0.0.0:8000`. The key pair is
written to:

- `server/.dev-ed25519.pem`: the private key. `ED25519_PRIVATE_KEY_PATH` is added
  to `server/.env`. Back this file up: losing it invalidates the signature check
  on every worker.
- `client/server-ed25519.pub`: the public key that the worker on this host uses.

`make run server` alone does not generate the key; without it the server stops at
start-up with `ED25519_PRIVATE_KEY_PATH not configured`.

At start-up a development server logs three expected warnings: no plugin is
loaded (see [Plugins](#plugins)), task secrets are disabled
(`TASK_SECRETS_KEYS_PATH`, see [Usage](#usage)), and, where the default
artifacts directory under `/var/lib` cannot be created (macOS, or a user without
write access there), artifact uploads are not stored. To keep task artifacts in
development, set `ARTIFACTS_ROOT` in `server/.env` to a directory you can write,
outside the checkout, and restart the server.

Leave the server running and open a second terminal. Check that it answers:

```bash
curl http://localhost:8000/healthz
```

#### 4. Create the first owner

The server has no public registration route. Issue the first owner invitation
from the host:

```bash
(cd server && uv run orcaster-bootstrap-owner)
```

The script prints an acceptance link of the form
`http://localhost:8000/auth/accept/<token>`, valid for 15 minutes. The host and
port of the link come from `WEBAUTHN_ORIGIN`, not from the port the server
listens on: if you start the server on another port, set `WEBAUTHN_ORIGIN` in
`server/.env` to match. Open it in a
browser on the same host and register a passkey. You become the owner of the
`Default` workspace. Running the script again without `--name` issues a fresh
invitation for the same workspace.

Passkeys need a secure context: `http://localhost` works for development; any
other host name needs HTTPS (see [Known limitations](#known-limitations)).

#### 5. Enrol a worker

In the dashboard, open **Workspaces** and create a token under **Worker
enrollment tokens**. The dialog shows a block to paste into `client/.env` in
place of its `ORCH_BASE_URL=` and `ORCH_ENROLLMENT_TOKEN=` lines, and a link to
download the server public key (`server-ed25519.pub`). On a development server
the block names `http://localhost:8000`, so replace that URL as the table below
says.

Create `client/.env` from the example and paste the block:

```bash
cp client/.env.example client/.env
$EDITOR client/.env
```

The two values that matter:

| Variable | Value |
|---|---|
| `ORCH_BASE_URL` | The server URL as the container sees it. For a development server on the same host use `http://host.containers.internal:8000`, never `localhost`, and start the worker with `EGRESS_ISOLATION=0` (step 6). With egress isolation on, the proxy only tunnels HTTPS, so `ORCH_BASE_URL` must be an `https://` URL. |
| `ORCH_ENROLLMENT_TOKEN` | The token from the dashboard. It is used only on the first start; the worker then stores its own identity in its data volume. |

On the server host, step 3 already wrote the public key to
`client/server-ed25519.pub`. On a separate worker host, save the downloaded
`server-ed25519.pub` at that path, or pass its location with
`make up client WORKER_PUBKEY=/path/to/server-ed25519.pub`. Without the public
key the worker does not start.

#### 6. Start the worker

```bash
make up client EGRESS_ISOLATION=0
make logs client
```

`make up client` builds the worker image (one to two minutes the first time) and
starts the worker; the egress proxy sidecar is started only when egress
isolation is on. The log shows `Registered worker_id=<uuid>` on the first
enrolment, followed by heartbeats, and the worker appears under **Workers** in
the dashboard. `make logs client` follows the log; press Ctrl+C to leave it.

On a development host `make up client` also warns that it found no host
configuration under `/etc` and no `setup.env`, so it uses `client/.env` and
`client/server-ed25519.pub` and creates the worker with no memory cap and no
extra read-only mounts. These warnings are expected here; a production worker
host is set up with the scripts under `setup-scripts/`.

Egress isolation is on by default and needs `aardvark-dns` 1.11.0 or newer and an
`https://` server URL; `make up client` stops with an error if aardvark-dns is
missing or older. The development worker above runs without it, so do not run
untrusted tasks on it.

If the host has a systemd user unit for the worker but the user manager is not
reachable from your shell, `make up client` stops before it touches the
containers and prints the cause. `AUTOSTART_SYNC=0` skips that probe on a
development host.

#### 7. Run a first task

From the dashboard, click **+ New task**, type `echo hello` in **Commands**,
leave **Requirements** empty and click **Create task**. Then open the task and
its execution output.

Or with the CLI. Install `orcaster` in one of three ways:

- from a release archive on the
  [releases page](https://github.com/AInvirion/orcaster/releases): each
  platform has an archive named `orcaster-cli-<version>-<os>-<arch>`
  (`.tar.gz`, `.zip` for Windows) that holds the `orcaster` binary,
  `README.md`, `LICENSE`, `NOTICE` and `THIRD_PARTY`, next to an SPDX SBOM
  (`<archive>.sbom.json`). Check the archive against the SHA-256 in
  `orcaster-<version>-checksums.txt`, or verify its build provenance with
  `gh attestation verify <archive> -R AInvirion/orcaster`, then put the binary
  on your `PATH`. The same release also carries the server and worker bundles
  and the worker wheel; see [Releasing Orcaster](docs/releasing.md);
- with Go:

  ```bash
  go install github.com/AInvirion/orcaster/orcaster@latest
  ```

  (each release `vX.Y.Z` also carries the Go module tag `orcaster/vX.Y.Z`, so
  `@vX.Y.Z` pins a release);

- or build it from the checkout with `make build orcaster`. The binary is
  written to `orcaster/orcaster`, which is not on your `PATH`: copy it to a
  directory that is, or run `export PATH="$PWD/orcaster:$PATH"` from the root of
  the repository.

Create a personal access token in the dashboard (**Tokens** in the top bar,
then **Generate token**; it is shown once), then:

```bash
orcaster auth login --host http://localhost:8000
orcaster task create --payload '{"requirements": [], "commands": ["echo hello"]}'
orcaster task output <task id>
```

The payload is `commands` (shell commands) plus `requirements` (pip specs).
`auth login` asks for the token without echo (or reads it from standard input
when that is not a terminal). `task create` prints a confirmation line such as
`Task 1 created (status queued, ...)`; the number is the task ID. Once the task
has run (a few seconds; the first task on a worker also prepares its
environment), `task output` prints the log of its latest execution: the command
line `$ echo hello` followed by `hello`.

### Production

`setup-scripts/debian13/` provisions a Debian 13 host for the server
(PostgreSQL, a systemd unit, Caddy with TLS, firewall). The installer runs as
root and copies the code into `INSTALL_DIR` (set in `setup.env`), which the
service user owns. Keep the source checkout that you run the installer from
**separate from `INSTALL_DIR` and owned by root**, so that the service user
cannot change code that root runs on the next installation:

```bash
sudo apt-get install -y git make
sudo git clone https://github.com/AInvirion/orcaster.git /root/orcaster
cd /root/orcaster
sudo cp setup-scripts/debian13/setup.env.example setup-scripts/debian13/setup.env
sudo chmod 600 setup-scripts/debian13/setup.env
sudo $EDITOR setup-scripts/debian13/setup.env   # set DOMAIN and ADMIN_EMAIL
sudo make setup-server debian13
```

The installer checks this layout before it changes anything and stops if it
does not hold: the checkout, every directory above it and the scripts must be
owned by root and not writable by group or others, `setup.env` must be a
root-owned regular file, and the checkout and `INSTALL_DIR` must not contain
one another. `setup.env` and its backups stay in the checkout; they are never
copied into `INSTALL_DIR`.

`DOMAIN` is a DNS name that points at the host. The scripts write
`WEBAUTHN_RP_ID`, `WEBAUTHN_ORIGIN`, `PUBLIC_BASE_URL` and
`SESSION_COOKIE_SECURE=true` to `server/.env` from `DOMAIN`; for a manual
deployment set them yourself. Then issue the first owner invitation with
`sudo make bootstrap debian13` from the same checkout (add
`ARGS="--name <workspace>"` to create a new workspace instead of adopting
`Default`) and open the printed link within 15 minutes to register a passkey.

To upgrade, update the source checkout (`sudo git pull`) and run
`sudo make setup-server debian13` again; the scripts are idempotent and restart
the service. Each run synchronises the checkout into `INSTALL_DIR` with
`rsync --delete`: it keeps `server/.env`, `secrets/`, the virtual environment
and every `*.env` file, and deletes any other file in `INSTALL_DIR` that is not
in the checkout. Keep data such as `ARTIFACTS_ROOT` outside `INSTALL_DIR`.

`setup-scripts/debian13-worker/` provisions worker hosts and applies the same
check to its own suite, also run from a root-owned checkout outside the worker
`INSTALL_DIR`. Keep a backup of the
server private signing key.

## Usage

Common `orcaster` commands:

```bash
orcaster auth status                         # current profile and identity
orcaster task create --payload @task.json    # payload from a file
orcaster task list
orcaster task get <id>
orcaster task cancel <id> --reason "no longer needed"
orcaster task retry <id>
orcaster task fork <id> --priority 9         # copy a task with changes
orcaster task output <id>                    # log of the latest execution
orcaster insights failures --since 2026-01-01
orcaster secret set EXAMPLE_TOKEN            # value read from stdin, never echoed
orcaster secret set SSH_KEY < key.pem        # multi-line values (PEM, JSON) go through a pipe; the one-line prompt refuses them
orcaster secret list                         # names and fingerprints, never values
```

`secret set` needs `TASK_SECRETS_KEYS_PATH` on the server: a file with one Fernet
key per line. `cd server && uv run orcaster-task-secrets-keys generate`
prints one; add `TASK_SECRETS_KEYS_PATH=<file>` to `server/.env` and restart the
server. Without it the API answers 503 `task_secrets_unconfigured`, and
`orcaster secret set` exits with `error: task secrets are not configured on this
server`.

`orcaster --help` and `orcaster <command> --help` list every command and flag.
The JSON API is described by the OpenAPI document at `/openapi.json` (a personal
access token as `Authorization: Bearer` or a dashboard session is required).

Development targets:

```bash
make help                 # all targets
make test server          # server tests
make test client          # worker tests
make test orcaster        # CLI tests
make lint server          # ruff check and format check
```

### Plugins

The core runs any shell or Python task without plugins. A server plugin adds
knowledge about one kind of workload: it recognises the *variant* of a task from
its payload (used to filter and group tasks in the dashboard and the API) and
gives default meanings to its exit codes. Without plugins the server logs one
warning at start-up, extracts no variant, and only `payload.outcomes` classifies
exit codes.

A plugin is a Python package that declares an entry point in the group
`orcaster.plugins`. A fictional example:

```toml
# pyproject.toml of the plugin
[project]
name = "orcaster-example-plugin"
version = "0.1.0"

[project.entry-points."orcaster.plugins"]
example-workload = "orcaster_example_plugin:register"
```

```python
# orcaster_example_plugin/__init__.py
from orcaster_server.plugins import OutcomeRule


def extract_label(payload: dict) -> str | None:
    # Tasks of this workload carry {"example": {"label": "..."}} in their payload.
    example = payload.get("example")
    return example.get("label") if isinstance(example, dict) else None


def register(registry) -> None:
    registry.add_variant_extractor("example-workload", extract_label)
    registry.add_outcome_defaults(
        "example-workload",
        {3: OutcomeRule(name="empty_result", closes_as="completed")},
    )
```

The server loads every installed plugin at start-up. `ORCASTER_PLUGINS` in
`server/.env` (a comma-separated list of entry-point names, for example
`ORCASTER_PLUGINS=example-workload`) makes the listed plugins required and puts
them first, in the order written; a listed plugin that is not installed stops
the server at start-up. A plugin that fails to load, or that registers a kind
twice, also stops it.

On a development server, install the plugin into the server environment and
restart the server:

```bash
(cd server && uv pip install /path/to/orcaster-example-plugin)
```

On a host provisioned with `setup-scripts/debian13/`, declare the plugin in
`setup.env` instead, because each installer run rebuilds the environment from
the lock file and drops packages installed by hand:

```bash
PLUGIN_PACKAGES="orcaster-example-plugin@git+https://github.com/example-org/orcaster-example-plugin@<40-hex commit SHA>"
ORCASTER_PLUGINS="example-workload"
SERVER_ENV_PASSTHROUGH="ORCASTER_PLUGINS"
```

`PLUGIN_PACKAGES` accepts only pinned requirements: a `git+https` URL with a
full commit SHA, an exact version (`name==1.2.3`) or the absolute path of a
wheel. The installer checks that every name in `ORCASTER_PLUGINS` is installed
and aborts otherwise. For a private plugin repository on GitHub,
`PLUGIN_GIT_TOKEN_FILE` names a root-only file with a read-only token; see
`setup-scripts/debian13/setup.env.example`.

## Known limitations

- **Egress isolation is not a full boundary.** The worker signs each task's
  egress allowlist with a per-worker key, so a task cannot add a host to it;
  a task can still delete its own list, or replay a signed list of an earlier
  task on the same worker and reach the hosts that task declared. Task code runs with the worker's uid, so it can read
  the worker credential in `state.json`.
- **Strict isolation is the default.** The code default of
  `ORCH_ENFORCE_ISOLATION` is `true`, and `client/.env.example` and the worker
  installer set it explicitly. A worker with `ORCH_ENFORCE_ISOLATION=false` runs
  in the legacy mode, where a task that declares no task environment inherits
  the worker's environment (the worker logs a warning at start). After changing the line, recreate the worker with `make up client`; a plain container restart does not
  re-read the env file.
- **Secrets are delivered over plain HTTP without refusal.** A worker whose
  server URL is `http://` receives task secrets over an unencrypted connection;
  nothing refuses it. Use `https://` for every worker that is not on the server
  host.
- **Development defaults are not production defaults.** The server binds to
  `0.0.0.0` (`BIND_HOST`), expects `http://localhost:8000` as the WebAuthn
  origin (`WEBAUTHN_ORIGIN`), and does not mark the session cookie as secure
  (`SESSION_COOKIE_SECURE=false`). Change all three before exposing a server.

See [SECURITY.md](SECURITY.md) for the threat model.

## Documentation

- [User manual](docs/user-manual.md): the operator guide (installation, dashboard, tasks, workers, tokens, JSON API, troubleshooting).
- [Operator manual](docs/operator-manual.md): enqueue and follow tasks from the CLI or the HTTP API.
- [Operator manual (Dashboard)](docs/operator-manual-ui.md): day-to-day operation from the web dashboard.
- [Worker guide](client/README.md), [CLI guide](orcaster/README.md).
- Server and worker host setup on Debian 13: [server](setup-scripts/debian13/README.md), [worker](setup-scripts/debian13-worker/README.md).
- [Release tooling](ops/release/README.md).
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup, tests and pull requests.
- [SECURITY.md](SECURITY.md): supported versions, reporting and threat model.
- [THIRD_PARTY](THIRD_PARTY): licences of the bundled third-party software.
- [NOTICE](NOTICE): copyright and attribution notice.
- [CLA.md](CLA.md): the Contributor License Agreement every contributor signs.
- [SUPPORT.md](SUPPORT.md): where to ask questions and report problems.

## Contributing

We welcome contributions. Please read the [Contributing Guidelines](CONTRIBUTING.md)
and the [Code of Conduct](CODE_OF_CONDUCT.md) before you open a pull request.

## Security

If you discover a security vulnerability, please follow the
[Security Policy](SECURITY.md). Do not open a public issue.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

Copyright 2024–2026 AInvirion LLC
