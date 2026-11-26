# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project versions the system as a whole (server + client + CLI) under a single tag.

## [Unreleased]

### Added

- The worker exports `TASKORCH_CACHE_DIR` and `TASKORCH_ARTIFACTS_DIR` to the
  task as deprecated aliases of `ORCASTER_CACHE_DIR` and
  `ORCASTER_ARTIFACTS_DIR`, with the same values. Tasks written before the
  rename read those names and found them empty on a 2.0.0 worker.

### Changed

- The README explains why the project is named Orcaster.

## [2.0.0] - 2026-11-25

First public release.

### Changed

- **BREAKING:** release assets are renamed by component: the CLI archives are
  `orcaster-cli-<version>-<os>-<arch>` (they were
  `orcaster_<version>_<os>_<arch>`), and `orcaster-<version>-checksums.txt`
  replaces `checksums.txt`. Each release also carries the server bundle
  (`orcaster-server-<version>.tar.gz`), the worker bundle
  (`orcaster-worker-<version>.tar.gz`) and the worker wheel
  (`orcaster_worker-<version>-py3-none-any.whl`), and its notes have one
  section per component. See `docs/releasing.md`.
- **BREAKING:** every setting, path, system user, container, volume, network
  and systemd unit uses the `orcaster` name (worker settings are
  `ORCH_ORCASTER_*`; the CLI reads `~/.config/orcaster`, `ORCASTER_HOST` and
  `ORCASTER_TOKEN`), and personal access tokens use the `orcaster_pat_` prefix.
  No other names are read, and a token with any other prefix is rejected with
  401.
- The CLI, worker and server now report unsupported legacy configuration
  names and token prefixes instead of ignoring them silently: the CLI prints a
  warning, the worker logs a warning that names the replacement setting, and
  the server's `401` says the token prefix is no longer accepted and a new
  token must be minted.
- Deploy: `run-fleet.sh` and `install-release.sh` no longer pass `WORKER_ENV`
  and `WORKER_PUBKEY` to `make up client`. They pass `CONFIG_DIR` and
  `ETC_SETUP_ENV_FILE`, and the Makefile resolves `$CONFIG_DIR/env[.N]` and
  `$CONFIG_DIR/server-ed25519.pub`, which makes it the single definition of
  those paths. An older Makefile without that resolution (a rollback with
  `--ref`) still receives both overrides, and an explicit `WORKER_ENV=` /
  `WORKER_PUBKEY=` from the operator still wins. The resolved files are the
  same, so there is no deploy step.
- The server refuses to start when `ARTIFACTS_ROOT` cannot be created or is
  not writable, with an error naming the path; it used to log a warning and
  start without it.
- The documentation describes the public project; the README links the user
  manual and the component guides.
- Deploy: `setup-scripts/debian13/deploy.sh` no longer sends `docs/` or `llms.txt` to the server; they are removed from the extracted tree and the `--delete` of both rsync calls removes them from the host. This is a deploy tooling change.

### Removed

- `make release-orcaster`: use `make release-dry-run`; releases are published
  only by the tag workflow.

### Fixed

- `make snapshot-orcaster` and `make build-orcaster` no longer take the Go
  module tag `orcaster/vX.Y.Z` as the version.
- A signed-in member who opens an owner-only dashboard page sees an
  **Access denied** page (`403`) instead of being redirected to sign-in; htmx
  requests get a plain `403` without `HX-Redirect`. Only a missing or expired
  session redirects to sign-in (#15).
- The worker's enrollment error no longer calls enrollment tokens single use.
- Code examples in the worker and CLI guides use the current task payload
  format.

### Security

- Worker control from the dashboard (pause, resume, clear cache, delete, push
  command and control operation) is owner-only. A member gets `403`, and the
  workers deck and worker page no longer show those controls to members (#13).

## [1.0.0] - 2026-11-18

Initial release.

Orcaster is a self-hosted orchestrator for distributed tasks. This release
contains three parts that are versioned together under one tag:

- **Server**: a FastAPI application that keeps the task queue in PostgreSQL,
  signs every task with an Ed25519 key, and serves the web interface, the JSON
  API and the single worker endpoint (`/poll`). Workspaces use passkey
  (WebAuthn) login, worker enrolment tokens and personal access tokens.
- **Worker client**: a Python worker that runs as a rootless Podman container,
  verifies the signature of each task before it runs it, and executes the task
  in an ephemeral `uv` virtual environment with optional per-task egress
  isolation.
- **CLI**: `orcaster`, a Go command-line client to create, inspect, retry, fork and
  cancel tasks and to manage workspace task secrets.

See `README.md` for installation and `SECURITY.md` for the trust model and its
known limitations.
