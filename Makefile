.DEFAULT_GOAL := help

UV         ?= uv
PYTHON     ?= $(UV) run python
# Container runtime. Podman-only by project decision: the worker is the only
# thing that runs in a container, and keeping it that way simplifies
# maintenance (no compose, no third-party tooling, no per-runtime overrides).
PODMAN     ?= $(shell command -v podman 2>/dev/null)
SERVER_DIR := server
CLIENT_DIR := client
ORCASTER_DIR := orcaster
# Go toolchain for the orcaster CLI. goreleaser and golangci-lint are optional
# locally: their targets degrade with a warning when missing (CI installs them).
GO         ?= go
GORELEASER ?= goreleaser
GOLANGCI   ?= golangci-lint
BIND_HOST  ?= 0.0.0.0
BIND_PORT  ?= 8000
PG_NAME    ?= orcaster-pg-dev
PG_USER    ?= orcaster
PG_PASS    ?= orcaster
PG_DB      ?= orcaster
PG_PORT    ?= 5432
# Same major as production and CI (Postgres 17). A dev volume created with 16
# does not start with 17: run `make db-down` and `podman volume rm $(PG_NAME)-data`.
PG_IMAGE   ?= docker.io/library/postgres:17-alpine
ARGS       ?=

# Worker container (managed with a direct `podman run`, no compose).
#
# Multiple workers on the same host: `make up client N=2` derives a `-2` suffix
# for the container and volume names, and reads the env file `client/.env.2`
# (on a host, `$(CONFIG_DIR)/env.2`). Each worker needs its own
# ORCH_ENROLLMENT_TOKEN and its own data volume, where its identity is
# persisted in `state.json`.
# Without `N` the behaviour is the usual one (single worker). The same targets
# honour N: `make logs client N=2`, `make purge client N=2`, etc.
# The server pubkey (WORKER_PUBKEY) and the source code (WORKER_SRC) are shared
# by all workers, so they carry NO suffix.
N             ?=
WORKER_SUFFIX := $(if $(strip $(N)),-$(strip $(N)),)
WORKER_NAME   ?= orcaster-worker$(WORKER_SUFFIX)
WORKER_IMAGE  ?= localhost/orcaster-worker:local
WORKER_DATA   ?= orcaster-data$(WORKER_SUFFIX)
WORKER_CACHE  ?= orcaster-cache$(WORKER_SUFFIX)
# WORKER_ENV and WORKER_PUBKEY are resolved further down, next to CONFIG_DIR.
WORKER_SRC    ?= $(abspath $(CLIENT_DIR))/src/orcaster_worker
# Server Ed25519 pubkey: mandatory, the worker verifies the signature of every
# task before running it. See the WORKER_PUBKEY resolution next to CONFIG_DIR.

# Worker container hardening: all capabilities dropped, no-new-privileges,
# read-only root filesystem plus a bounded tmpfs for $HOME, and optional
# resource limits (CPU has no default; the memory cap is declared by the host in
# `setup.env` and resolved further down; pids-limit has a conservative default).
# HOME is mounted with `--mount type=tmpfs,...,tmpfs-mode=0700,U=true`: a
# root-owned tmpfs breaks `uv` in every task (`Permission denied` on
# $HOME/.local/share/uv). `U=true` makes it owned by the worker uid; the
# `--tmpfs` shorthand cannot chown.
WORKER_PIDS_LIMIT ?= 1024
WORKER_CPUS       ?=

# Container memory cap. The host declares the value in the fleet suite's
# `setup.env`. A manual `make up client N=1` used to create the container
# WITHOUT a cap, silently, unless `run-fleet.sh` passed the value explicitly on
# the `make` line. The cap is not cosmetic: with it a runaway task dies as
# `CONSTRAINT_MEMCG` inside its own cgroup; without it the host-wide OOM killer
# fires. That is why the value is resolved here from `setup.env` when it does
# not come from another source, instead of depending on whoever invokes make
# remembering to pass it.
#
# `origin` rather than `?=`: it tells "undefined" apart from "defined but
# empty", and that explicit empty value (`make up client WORKER_MEMORY=`, and
# the one `run-fleet.sh` propagates when the host declares no cap) must remain
# the opt-out. Environment and command line win over the file, in make's usual
# order.
#
# The file is sourced in a subshell with BASH, not with the `/bin/sh` that make
# uses by default for `$(shell ...)`: on Debian that `sh` is dash, while what
# consumes `setup.env` at deployment (`load_env`) is bash. With two different
# parsers, a `setup.env` that the deployment accepts could fail here and leave
# the container without a cap, the same silence this block removes. The values
# the suite itself writes are single-quoted (`env_quote`) and POSIX, but
# `setup.env.example` is edited by hand and nothing forces it to stay POSIX.
#
# NO path that ends without a cap may stay silent: creating the container
# without `--memory` is exactly the bug this block closes, and what made it
# undetectable was the silence, not the empty value. There are four states and
# each has its own reason in `SETUP_ENV_DIAG`: the file could not be sourced,
# the file declares no cap, no readable `setup.env` exists, or the cap was
# emptied on purpose through the environment or the command line. The warning
# is NOT printed here but in the `up-client` recipe, the only consumer of the
# cap: this block is evaluated for EVERY target and a `make help` in a
# development checkout has nothing to warn about. It warns and continues, it
# does not abort: dev and CI start the worker without a cap on purpose.
#
# The "no readable setup.env" case includes one that `$(wildcard)` cannot tell
# apart from absence: `/etc/orcaster` is 0750 root:orcaster, so for a user
# outside the group the file exists but `stat` gives EACCES, the wildcard
# returns empty and the search falls back to the tree, where the tarball never
# ships a `setup.env` (it is in .gitignore, `git archive` does not package it).
# That is why the diagnosis separately checks whether the directory exists and
# cannot be traversed: it is the difference between "the host is not configured"
# and "you are missing the group". The FORMAT of the value is validated by
# `load_env`/`cloud-init-worker.sh` at deployment; a corrupt one that gets here
# anyway is rejected by podman when creating the container, loudly and before
# running anything.
#
# The path is anchored to the directory of the Makefile ITSELF, not to the cwd:
# a relative path resolves against make's cwd, so `make -f
# /opt/orcaster/Makefile up-client` from another directory did not
# find the file and created the container without a cap, silently. `-C` (what
# `run-fleet.sh` uses) never had the problem, but the difference between the
# two forms should not decide whether the cap is applied.
SETUP_ENV_MISREAD := __setup_env_misread__
# setup.env search order: first the host's, which since the tarball deployment
# lives in /etc/orcaster and NOT inside the checkout; then the tree's own, where
# a development checkout may still keep it. Without the first entry, moving the
# file to /etc/orcaster would reopen the bug of the container without a cap: a
# manual `make up client` would no longer find the cap and would create the
# container without --memory, silently. The host path is its own variable so
# the harness tests can point it at a nonexistent file and not depend on the
# /etc of whoever runs them.
ETC_SETUP_ENV_FILE ?= /etc/orcaster/setup.env
ETC_SETUP_ENV_DIR  := $(patsubst %/,%,$(dir $(ETC_SETUP_ENV_FILE)))
# Host configuration directory that the root passes to the scripts in
# $(SETUP_WORKER_DIR). It is DERIVED from ETC_SETUP_ENV_DIR instead of
# hardcoding /etc/orcaster: that directory is, by definition, the one that
# contains the host's setup.env, i.e. the same answer to the same question.
# With two independent values, a host (or a test) that moves the config to
# another path silently desynchronizes the file this Makefile reads from the
# CONFIG_DIR the scripts receive, and the failure does not show up until one of
# the two finds nothing.
# `?=` so that the two paths that already carry the right host value win: an
# ENVIRONMENT variable (cloud-init-worker.sh templates CONFIG_DIR and exports
# it, so run-fleet.sh and everything below it inherits it) and a COMMAND LINE
# variable (install-release.sh passes it that way in its `make up client`).
# Without this, 06-autostart.sh fell back to its own lib.sh default, died in
# `load_env` asking for /etc/orcaster/setup.env, the `|| echo` in
# `autostart-sync-client` swallowed the failure and the systemd --user unit was
# left NOT regenerated: its PIDFile still named the container that
# `make up client` had just destroyed, so the next boot started the unit
# against a nonexistent ID.
CONFIG_DIR ?= $(ETC_SETUP_ENV_DIR)
# Guard against the one-way CONFIG_DIR <-> ETC_SETUP_ENV_FILE desync.
# The derivation above only covers the ETC_SETUP_ENV_FILE -> CONFIG_DIR
# direction. With the knob the other way round -- `make up client
# CONFIG_DIR=/srv/orcaster` by hand, without ETC_SETUP_ENV_FILE= -- CONFIG_DIR
# points to the new dir but SETUP_ENV_FILE (below) keeps looking in the default
# /etc/orcaster: the memory cap and the autostart read different paths, and the
# deviation is silent (if the old /etc survives, a wrong cap is applied without
# warning). We fail LOUDLY before creating anything. Minimal, deliberate scope,
# so as not to bother any sanctioned path:
#   - only if CONFIG_DIR came from the COMMAND LINE: it is the documented
#     trigger. The automatic paths pass CONFIG_DIR through the ENVIRONMENT
#     (cloud-init-worker.sh exports it, including the CONFIG_DIR=/srv/orcaster
#     case in its comment; run-fleet.sh inherits it from there), so they do NOT
#     land here even if the values differ.
#   - only if ETC_SETUP_ENV_FILE stayed at its default (nobody overrode it):
#     install-release.sh passes BOTH variables together, so ETC is not at its
#     default and does not trigger; the harness that points ETC at a scratch
#     file does not either (it passes it explicitly).
#   - only if the two paths DIFFER: a consistent host is not bothered. They are
#     compared NORMALIZED (no trailing slash): CONFIG_DIR=/etc/orcaster/
#     resolves to the same dir as the default and must not raise a false positive.
#   - only on the path that CREATES the worker: `make up client` (goals `up` +
#     `client`) and its equivalent direct form `make up-client` (the one used by
#     install-release.sh and the tarball deployment runbook). It does NOT cover
#     `make up server` (it does not consume CONFIG_DIR this way) nor
#     down/lint/help/tests.
CONFIG_DIR_NORM := $(patsubst %/,%,$(CONFIG_DIR))
# UP_CLIENT_GOAL is non-empty when the goal creates the worker through either
# syntax: `up-client` directly, or `up` accompanied by `client`.
UP_CLIENT_GOAL := $(filter up-client,$(MAKECMDGOALS))$(if $(filter up,$(MAKECMDGOALS)),$(filter client,$(MAKECMDGOALS)))
ifneq ($(UP_CLIENT_GOAL),)
ifeq ($(origin CONFIG_DIR),command line)
ifeq ($(filter command line environment,$(origin ETC_SETUP_ENV_FILE)),)
ifneq ($(CONFIG_DIR_NORM),$(ETC_SETUP_ENV_DIR))
$(error CONFIG_DIR=$(CONFIG_DIR) was passed on the command line but ETC_SETUP_ENV_FILE is still at its default ($(ETC_SETUP_ENV_FILE)): the Makefile would read the setup.env in $(ETC_SETUP_ENV_DIR), not in $(CONFIG_DIR_NORM), and the memory cap and the autostart would be out of sync. Also pass ETC_SETUP_ENV_FILE=$(CONFIG_DIR_NORM)/setup.env, as ops/release/install-release.sh does)
endif
endif
endif
endif
# Worker env file and pubkey: ONE single path definition for a running worker.
# run-fleet.sh and install-release.sh do not pass WORKER_ENV or
# WORKER_PUBKEY: they pass CONFIG_DIR (+ ETC_SETUP_ENV_FILE) and this block
# derives both files. The scripts detect this block by the WORKER_ENV_HOST
# assignment (lib.sh::worker_config_make_args); do not rename it without
# updating that helper. On a host the origin is $(CONFIG_DIR)
# (/etc/orcaster): `env` / `env.N` and `server-ed25519.pub`. Mind the name: on
# the host it is `env.N` (no leading dot, lib.sh::worker_envfile), in the
# development checkout it is `client/.env.N` (the one in client/.env.example).
# Resolution order:
#   1. explicit override (WORKER_ENV= / WORKER_PUBKEY= on the command line or
#      in the environment): always wins and is left untouched. It is for an
#      operator with the files elsewhere; the deploy scripts no longer pass it
#      (only to an older Makefile without this block, on a rollback).
#   2. there is host config: some $(CONFIG_DIR)/env, $(CONFIG_DIR)/env.* or
#      $(CONFIG_DIR)/server-ed25519.pub exists. The host paths are ALWAYS used,
#      even if the specific file is missing: up-client fails naming the
#      $(CONFIG_DIR) path, without silently falling back to client/ (a forgotten
#      client/.env in the throwaway checkout is exactly the deviation this rule
#      prevents). setup.env alone does not count: a development checkout may
#      have it in /etc/orcaster for the memory cap and still use client/.env.
#   3. no host config: development fallback to client/.env[.N] and
#      client/server-ed25519.pub (the README quick start). up-client warns on
#      stderr which path it used.
# The 2/3 decision is common to the PAIR of files, not per file: mixing the
# host env with the checkout pubkey (or the other way round) would reopen the
# deviation. Each keeps its own override (1).
WORKER_ENV_HOST    := $(CONFIG_DIR_NORM)/env$(if $(strip $(N)),.$(strip $(N)),)
WORKER_PUBKEY_HOST := $(CONFIG_DIR_NORM)/server-ed25519.pub
WORKER_ENV_DEV     := $(abspath $(CLIENT_DIR))/.env$(if $(strip $(N)),.$(strip $(N)),)
WORKER_PUBKEY_DEV  := $(abspath $(CLIENT_DIR))/server-ed25519.pub
HOST_WORKER_CONFIG := $(wildcard $(CONFIG_DIR_NORM)/env $(CONFIG_DIR_NORM)/env.* $(WORKER_PUBKEY_HOST))
ifneq ($(filter command line environment override,$(origin WORKER_ENV)),)
WORKER_ENV_SOURCE := override
else ifneq ($(HOST_WORKER_CONFIG),)
WORKER_ENV        := $(WORKER_ENV_HOST)
WORKER_ENV_SOURCE := host
else
WORKER_ENV        := $(WORKER_ENV_DEV)
WORKER_ENV_SOURCE := dev
endif
ifneq ($(filter command line environment override,$(origin WORKER_PUBKEY)),)
WORKER_PUBKEY_SOURCE := override
else ifneq ($(HOST_WORKER_CONFIG),)
WORKER_PUBKEY        := $(WORKER_PUBKEY_HOST)
WORKER_PUBKEY_SOURCE := host
else
WORKER_PUBKEY        := $(WORKER_PUBKEY_DEV)
WORKER_PUBKEY_SOURCE := dev
endif

REPO_SETUP_ENV_FILE := $(patsubst %/,%,$(dir $(abspath $(firstword $(MAKEFILE_LIST)))))/setup-scripts/debian13-worker/setup.env
SETUP_ENV_FILE    ?= $(firstword $(wildcard $(ETC_SETUP_ENV_FILE)) $(REPO_SETUP_ENV_FILE))
ifeq ($(origin WORKER_MEMORY),undefined)
WORKER_MEMORY := $(shell [ -r "$(SETUP_ENV_FILE)" ] || exit 0; \
	bash -c '. "$$0" >/dev/null 2>&1 && printf %s "$${WORKER_MEMORY-}"' "$(SETUP_ENV_FILE)" 2>/dev/null \
	|| printf %s '$(SETUP_ENV_MISREAD)')
ifeq ($(WORKER_MEMORY),$(SETUP_ENV_MISREAD))
$(warning $(SETUP_ENV_FILE) exists but `bash` could not source it (invalid file, or bash missing): the container will be created WITHOUT a memory cap. Fix the file or pass the cap explicitly with `make up client WORKER_MEMORY=28g`.)
WORKER_MEMORY :=
SETUP_ENV_DIAG := $(SETUP_ENV_FILE) exists but bash could not source it (see the warning above)
else ifeq ($(WORKER_MEMORY),)
# `=` (deferred expansion) and not `:=`: this branch fires in any checkout
# without a declared cap, which is the normal dev/CI state, and the whole block
# is evaluated for EVERY target. With `:=` the `$(shell)` forked a /bin/sh on
# every make invocation (help, lint, test, etc.) to compute a text that only
# the `up-client` recipe reads. Deferred, the fork happens only when that
# recipe expands `$(SETUP_ENV_DIAG)`: the `$(if $(WORKER_MEMORY),...)` that
# wraps it there does not even expand this branch when the cap WAS resolved.
SETUP_ENV_DIAG = $(shell \
	if [ -r "$(SETUP_ENV_FILE)" ]; then \
	    printf %s '$(SETUP_ENV_FILE) does not declare WORKER_MEMORY'; \
	elif [ -d "$(ETC_SETUP_ENV_DIR)" ] && [ ! -x "$(ETC_SETUP_ENV_DIR)" ]; then \
	    printf %s '$(ETC_SETUP_ENV_DIR) exists but this user cannot traverse it (missing membership in the orcaster group)'; \
	else \
	    printf %s 'no readable setup.env (looked in $(ETC_SETUP_ENV_FILE) and $(REPO_SETUP_ENV_FILE))'; \
	fi)
endif
endif
# Fourth state: an explicit `WORKER_MEMORY=` through the environment or the
# command line (the opt-out `run-fleet.sh` propagates when the host declares no
# cap) skips the whole block above, so this default supplies the reason.
SETUP_ENV_DIAG ?= cap emptied on purpose through the environment or the command line
WORKER_MEMORY_FLAG := $(if $(WORKER_MEMORY),--memory $(WORKER_MEMORY),)
WORKER_CPUS_FLAG   := $(if $(WORKER_CPUS),--cpus $(WORKER_CPUS),)

# Host directories mounted READ-ONLY at the SAME path inside the worker
# container. Use case: a wheel in /opt/example-ro that a task installs with
# `file:///opt/example-ro/<wheel>`, which is resolved against the container's
# filesystem, not the host's. Space-separated list; empty or undeclared = no
# extra mount and the usual `podman run`.
#
# Same channel as WORKER_MEMORY, for the same reason: the host declares the
# value in `setup.env`, and a manual `make up client N=i` must create the
# container with the same mounts as the deployment. `origin` rather than `?=`:
# environment and command line win over the file, and an explicit empty value
# (`WORKER_RO_MOUNTS=`) is the opt-out. The file is sourced with bash, as for
# the cap.
#
# No path leaves the mounts out silently. If the file exists but bash cannot
# source it, there is a parse-time $(warning) (as for the cap) and a WARNING in
# the recipe. If there is no readable setup.env (the typical case: /etc/orcaster
# is 0750 root:orcaster and whoever runs make is not in the group), it cannot
# be known whether the host declares mounts: the recipe WARNS about that too,
# with the reason.
#
# The value is not validated here but in `.worker-ro-mounts-check`, the first
# prerequisite of `up-client`, which runs check-ro-mounts.sh as the user running
# make BEFORE building, touching the proxy, stopping the systemd unit and the
# `podman rm -f` (also under `make -j`: see the .NOTPARALLEL next to the
# target): an invalid entry or a directory that the container uid cannot read
# stops the run with the previous container intact.
#
# No `z` or `Z`. `z` (lowercase) is the SELinux label SHARED between containers
# and `Z` the private one of a single container; both make podman RELABEL the
# host content. They are omitted because the fleet's Debian hosts do not run
# SELinux and because we do not want to relabel host content (e.g.
# /opt/example-ro) as a side effect of creating a worker. Hosts with SELinux in
# enforcing mode (Fedora, the macOS podman machine) are out of scope: there a
# directory without the right label gives EACCES inside even if it passes
# validation. Plain `:ro`.
ifeq ($(origin WORKER_RO_MOUNTS),undefined)
WORKER_RO_MOUNTS := $(shell [ -r "$(SETUP_ENV_FILE)" ] || exit 0; \
	bash -c '. "$$0" >/dev/null 2>&1 && printf %s "$${WORKER_RO_MOUNTS-}"' "$(SETUP_ENV_FILE)" 2>/dev/null \
	|| printf %s '$(SETUP_ENV_MISREAD)')
ifeq ($(WORKER_RO_MOUNTS),$(SETUP_ENV_MISREAD))
$(warning $(SETUP_ENV_FILE) exists but `bash` could not source it: the container will be created WITHOUT the WORKER_RO_MOUNTS mounts it declares. Fix the file or pass the list explicitly with `make up client WORKER_RO_MOUNTS="/opt/example-ro"`.)
WORKER_RO_MOUNTS :=
WORKER_RO_MOUNTS_DIAG := $(SETUP_ENV_FILE) exists but bash could not source it
else
# Deferred for the same reason as SETUP_ENV_DIAG: only the up-client recipe
# expands it. Empty when the file was read fine (whether or not it declares the list).
WORKER_RO_MOUNTS_DIAG = $(shell \
	if [ -r "$(SETUP_ENV_FILE)" ]; then \
	    :; \
	elif [ -d "$(ETC_SETUP_ENV_DIR)" ] && [ ! -x "$(ETC_SETUP_ENV_DIR)" ]; then \
	    printf %s '$(ETC_SETUP_ENV_DIR) exists but this user cannot traverse it (missing membership in the orcaster group)'; \
	else \
	    printf %s 'no readable setup.env (looked in $(ETC_SETUP_ENV_FILE) and $(REPO_SETUP_ENV_FILE))'; \
	fi)
endif
endif
# `strip` as in `.worker-ro-mounts-check`: the validator and the flags must see
# the SAME words. Without it, GNU Make 3.81's foreach (macOS) does not split on
# a newline, and a multi-line list would pass validation as two paths but reach
# podman as a single one.
WORKER_RO_MOUNT_FLAGS := $(foreach d,$(strip $(WORKER_RO_MOUNTS)),-v $(d):$(d):ro)
# Anchored to the directory of the Makefile itself, like REPO_SETUP_ENV_FILE:
# `make -f /opt/orcaster/Makefile` from another cwd must find it.
RO_MOUNTS_CHECK := $(patsubst %/,%,$(dir $(abspath $(firstword $(MAKEFILE_LIST)))))/setup-scripts/debian13-worker/check-ro-mounts.sh
# Same anchoring as RO_MOUNTS_CHECK: SETUP_WORKER_DIR is relative to the repo
# and `make -f /opt/orcaster/Makefile up-client` from another cwd
# would not find user-bus.sh.
USER_BUS_SH := $(patsubst %/,%,$(dir $(abspath $(firstword $(MAKEFILE_LIST)))))/setup-scripts/debian13-worker/user-bus.sh

# Server Ed25519 key. Local dev convention:
#   private → server/.dev-ed25519.pem   (referenced by ED25519_PRIVATE_KEY_PATH)
#   public  → $(WORKER_PUBKEY)          (consumed by the worker)
SERVER_KEY        ?= $(abspath $(SERVER_DIR))/.dev-ed25519.pem
SERVER_KEY_REL    ?= .dev-ed25519.pem
SERVER_ENV        ?= $(abspath $(SERVER_DIR))/.env

# Egress topology: the worker runs in the internal network `orcaster-egress`
# (--internal, no route out of its own) and can only reach the Internet through
# the `orcaster-proxy` sidecar, which is also connected to
# `orcaster-egress-out` (with real outbound access). The `orcaster-egress-vol`
# volume is where the worker writes the per-task allowlist (`allow.txt`) and
# the proxy reads it (rw in the worker, ro in the proxy). The task runs with the
# worker's uid and can also write it: that is why the worker signs it and the
# proxy discards whatever does not carry a valid signature (see
# EGRESS_KEY_SECRET).
#
# Authoritative default: egress isolation is ALWAYS on. The production fleet
# and a routine `make up client` start the worker behind the egress sidecar +
# internal network. Explicit opt-out with `make up client EGRESS_ISOLATION=0`
# (for dev/CI without the egress infrastructure: no up-proxy dependency, no
# internal network, no proxy envs, no egress volume). The worker container
# hardening (cap-drop, read-only, tmpfs, pids-limit, memory/cpus) is independent
# and always present.
EGRESS_ISOLATION ?= 1
EGRESS_NET_IN    ?= orcaster-egress
EGRESS_NET_OUT   ?= orcaster-egress-out
# The volume and the proxy sidecar DO carry the N suffix (unlike the networks,
# which are only wiring and can be shared): each worker of a multi-worker host
# needs its own per-task allowlist (otherwise worker A finishing a task would
# delete the allowlist of worker B's running task) and its own proxy
# (otherwise `up-client N=2` would `podman rm -f` the proxy that worker 1 is
# actively using).
EGRESS_VOL       ?= orcaster-egress-vol$(WORKER_SUFFIX)
PROXY_NAME       ?= orcaster-proxy$(WORKER_SUFFIX)
# Key of the signed allowlist, per worker like the volume. The worker and the
# task share a uid, so the task can write everything the worker writes in
# $(EGRESS_VOL): the boundary is this key, which the task does not read. It
# lives in the host's Podman secret store (not in a volume nor in
# `podman inspect`) and enters the worker and the proxy as an environment
# variable (`--secret ...,type=env`). `egress-net` creates it once (32 random
# bytes) and reuses it; `purge-client` deletes it (it removes the containers
# first, so it leaves none created with the secret and without it). Rotating it
# is NOT `make purge client`: that target also deletes the data volume
# ($(WORKER_DATA), with the `state.json` that is the worker's enrollment
# identity), i.e. a full reset. Rotation: pause and drain the worker; then, as
# the rootless user, `podman secret rm $(EGRESS_KEY_SECRET)` immediately
# followed by `make up client N=<n>` (recreates the secret, the proxy and the
# worker). Podman resolves the secret on every container start: one created
# with `--secret` does not start if the secret no longer exists (`podman start`
# fails with `no secret with name or id ...: no such secret`, verified on
# Podman 6.0.2), nor does the `systemd --user` unit at boot or on its restarts.
# That is why the secret is not left deleted while containers created with it
# exist. In a rollback, the previous release is redeployed first (it recreates
# the containers without `--secret`) and the secret is deleted only afterwards.
EGRESS_KEY_SECRET ?= orcaster-egress-key$(WORKER_SUFFIX)
EGRESS_KEY_FLAG   := --secret $(EGRESS_KEY_SECRET),type=env,target=ORCH_EGRESS_KEY
PROXY_PORT       ?= 3128
PROXY_BASE_ALLOW ?= pypi.org,files.pythonhosted.org
# Hostname of the orchestrator (control plane): it is added to the proxy's base
# allowlist so the worker itself can keep polling/reporting through the
# sidecar. It is derived from ORCH_BASE_URL in $(WORKER_ENV) if not passed
# explicitly (make up client ORCH_HOST=orchestrator.your-domain.com). If
# $(WORKER_ENV) does not exist or does not define ORCH_BASE_URL, it stays empty
# (the extra `,` is discarded when parsing ORCASTER_PROXY_BASE_ALLOW, see
# egress_proxy.py).
# Note: this derivation assumes a DNS/IPv4 host; URLs with an IPv6 literal
# (`https://[::1]:8000`) are not supported (the sed does not parse them well).
ORCH_HOST ?= $(shell grep -m1 '^ORCH_BASE_URL=' $(WORKER_ENV) 2>/dev/null | sed -E -e 's,^ORCH_BASE_URL=,,' -e 's,^[A-Za-z][A-Za-z0-9+.-]*://,,' -e 's,[:/].*$$,,')

# Automatic autostart synchronization after recreating the worker (default: 1).
# When the host already has autostart at boot enabled (the systemd --user unit
# 'container-$(WORKER_NAME).service' exists, created by
# setup-scripts/debian13-worker/06-autostart.sh), `up-client` regenerates that
# unit when it finishes. The unit is an adoption unit ('podman start <name>',
# see 06-autostart.sh) and embeds a PIDFile with the container ID; when the
# container is recreated (EGRESS_ISOLATION=1 applies new flags via
# 'podman rm -f' + 'podman run') the ID changes, so without regenerating the
# unit systemd would fail at boot looking for the PIDFile of a container that no
# longer exists. It is a no-op on machines without that autostart (dev/CI, or
# workers not yet handed over to systemd): it only acts if the unit already
# exists and `systemctl` is available. It can be disabled with AUTOSTART_SYNC=0
# (used by run-fleet.sh, which already invokes 06-autostart.sh explicitly after
# each recreation). AUTOSTART_SYNC=0 turns off ALL handling of the unit: the
# manager probe (.autostart-bus-check), the earlier stop of the worker and
# sidecar units (user-bus.sh stop-unit) and the final re-synchronization.
AUTOSTART_SYNC ?= 1
# AUTOSTART_RESYNC=0 turns off ONLY the final re-synchronization
# (autostart-sync-client) and leaves the probe and the stops active. It is used
# by install-release.sh: the hook runs 06-autostart.sh from THIS tree, which on
# a tarball-installed host is INSTALL_DIR, and the origin guard in lib.sh
# (require_source_outside_install_dir) rejects it; the script re-synchronizes on
# its own as root from root's copy. The stops must remain: the script's
# pre-stop only stops `active` units, and user-bus.sh also stops those in
# `activating` (auto-restart) or `deactivating`, which would otherwise relaunch
# the container in a race with `podman run`.
AUTOSTART_RESYNC ?= 1
# A failed re-synchronization makes `up-client` fail (default: 1; see the
# comment on autostart-sync-client). With 0 it only warns and exits 0, for
# whoever recreates several workers in a loop and must NOT have it cut halfway
# (the following ones would be left on already-replaced inodes) and verifies
# each unit afterwards on their own. install-release.sh passes it together with
# AUTOSTART_RESYNC=0 because it may install the tarball of an earlier release,
# whose Makefile does not know AUTOSTART_RESYNC and runs the hook anyway.
AUTOSTART_SYNC_FATAL ?= 1
# Location of the worker provisioning suite (relative: `make` runs from the
# repo root, like the setup-server/bootstrap targets further down).
SETUP_WORKER_DIR := setup-scripts/debian13-worker

# Egress topology flags injected into `up-client`, active only under
# EGRESS_ISOLATION=1 (see the comment above). UP_CLIENT_DEPS adds the up-proxy
# dependency; EGRESS_CLIENT_FLAGS adds the internal network + proxy envs + the
# egress volume to the worker's `podman run`.
#
# NO `--dns=none`. Under `--dns=none` podman does not publish aardvark-dns to
# the worker (resolv.conf is left with an unreachable public nameserver in the
# `--internal` network), so the worker does NOT resolve the name of the
# `$(PROXY_NAME)` sidecar → registration/poll fails. The `--internal` network
# already guarantees default-deny (no route out of its own): aardvark resolves
# the proxy but there is no egress outside the sidecar's tunnel (validated:
# `example.com` neither resolves nor is reachable). PRECONDITION: that DNS
# guarantee requires aardvark-dns >= 1.11.0 ("Do not allow 'internal' networks
# to access DNS"); on older stacks (podman 4.x/CNI or aardvark < 1.11) the DNS
# of --internal networks IS forwarded upstream and reopens an exfiltration
# channel.
# The egress volume is mounted with `:U` so podman chowns it to the worker uid;
# otherwise the worker (non-root uid) cannot write the per-task allowlist and
# `egress_allowlist` has no effect.
ifeq ($(EGRESS_ISOLATION),1)
UP_CLIENT_DEPS      := up-proxy
EGRESS_CLIENT_FLAGS := --network $(EGRESS_NET_IN) \
    -e HTTPS_PROXY=http://$(PROXY_NAME):$(PROXY_PORT) \
    -e HTTP_PROXY=http://$(PROXY_NAME):$(PROXY_PORT) \
    -e NO_PROXY=localhost,127.0.0.1 \
    $(EGRESS_KEY_FLAG) \
    -v $(EGRESS_VOL):/var/lib/orcaster-egress:rw,U
else
UP_CLIENT_DEPS      :=
EGRESS_CLIENT_FLAGS :=
endif

DEV_DB_URL := postgresql+asyncpg://$(PG_USER):$(PG_PASS)@127.0.0.1:$(PG_PORT)/$(PG_DB)

COMPONENT := $(word 2,$(MAKECMDGOALS))

.PHONY: help run test lint fmt migrate up down logs restart shell build purge keygen dev \
        run-server test-server lint-server fmt-server migrate-server up-server keygen-server dev-server \
        test-client lint-client fmt-client \
        build-client up-client down-client logs-client restart-client shell-client purge-client \
        egress-net up-proxy autostart-sync-client \
        test-orcaster lint-orcaster fmt-orcaster build-orcaster snapshot-orcaster release-dry-run licenses-orcaster \
        db-up db-down db-shell db-wait clean server client orcaster debian13 setup-server bootstrap \
        .podman-check .aardvark-check .uv-check .openssl-check .go-check .worker-ro-mounts-check

help:
	@echo "AInvirion Orcaster — available targets:"
	@echo ""
	@echo "  make dev server         One-shot: db-up + migrate + keygen + uvicorn (recommended)"
	@echo "  make run server         Start uvicorn with hot-reload (server/orcaster_server/main.py)"
	@echo "  make up server          Generate the Ed25519 key if missing and start uvicorn"
	@echo "  make keygen server      Only generate the Ed25519 key (priv+pub) if it does not exist"
	@echo "  make test server        Run pytest in server/  (ARGS=\"-v -k foo\")"
	@echo "  make lint server        Ruff check + format --check"
	@echo "  make fmt server         Ruff format + check --fix"
	@echo "  make migrate server     orcaster-migrate (alembic upgrade head)"
	@echo ""
	@echo "  make test client        Run pytest in client/  (ARGS=\"-v -k foo\")"
	@echo "  make lint client        Ruff check + format --check"
	@echo "  make fmt client         Ruff format + check --fix"
	@echo ""
	@echo "  make build client       Build the worker image (orcaster-worker:local)"
	@echo "  make up client          Start the worker in a container (bind-mounts src/)"
	@echo "    EGRESS_ISOLATION=0    Opt-out: disables the egress sidecar + internal network (default: 1, isolation on)"
	@echo "  make up-proxy           Only the egress sidecar (orcaster-proxy); up client already includes it by default"
	@echo "    ORCH_HOST=host        Orchestrator hostname (otherwise derived from ORCH_BASE_URL in .env)"
	@echo "  make down client        Stop and remove the unsuffixed worker + its egress sidecar; with N=2, only that"
	@echo "                          worker and its sidecar. The other workers on the host are left alone"
	@echo "    ALL=1                 Sweep ALL the workers + sidecars on the host (orcaster-worker[-n], orcaster-proxy[-n]);"
	@echo "                          lists the containers before removing them. Cannot be combined with N"
	@echo "  make restart client     Restart the worker (applies edits without a rebuild)"
	@echo "  make logs client        Follow the worker logs (ctrl-C to exit)"
	@echo "  make shell client       Open bash inside the worker container"
	@echo "  make purge client       Remove containers ($(WORKER_NAME), $(PROXY_NAME)) + volumes ($(WORKER_DATA), $(WORKER_CACHE), $(EGRESS_VOL))"
	@echo "    N=2                   Multiple workers on one host: suffixes the container/volumes and reads"
	@echo "                          client/.env.2 (e.g.: make up client N=2 / make logs client N=2)"
	@echo ""
	@echo "  make test orcaster      Run go test in orcaster/  (ARGS=\"-run TestFoo -v\")"
	@echo "  make lint orcaster      gofmt + go vet (+ golangci-lint if installed)"
	@echo "  make fmt orcaster       gofmt -w in orcaster/"
	@echo "  make build orcaster     Build orcaster/orcaster"
	@echo "  make snapshot-orcaster  Cross-compile all platforms without publishing (goreleaser)"
	@echo "  make release-dry-run    Build every release asset and the notes locally, no tag, no publish (VERSION=vX.Y.Z)"
	@echo "  make licenses-orcaster  Module graph licenses vs allowlist and THIRD_PARTY (NOTICE=path; default: ./THIRD_PARTY)"
	@echo ""
	@echo "  make db-up              Start local Postgres ($(PG_NAME)) with podman"
	@echo "  make db-down            Stop and remove the container"
	@echo "  make db-shell           psql inside the container"
	@echo ""
	@echo "  make clean              Delete __pycache__, .pytest_cache, .ruff_cache"
	@echo ""
	@echo "  make setup-server debian13   Provision a VPS for the server (see setup-scripts/)"
	@echo "                               ARGS=\"--skip-hardening\" / \"--from 04\" for variants"
	@echo "  make bootstrap debian13      Issue the first owner's invitation (passkey link)"
	@echo "                               ARGS=\"--name Acme\" for a new workspace"
	@echo ""
	@echo "Equivalent syntax: make run-server / make test-server / etc."

# Positional subcommands: no-op so that 'make run server' does not fail.
# `debian13` is the "component" of the setup-server dispatcher.
server client orcaster debian13:
	@:

# Positional dispatchers
run:
	@$(MAKE) --no-print-directory run-$(COMPONENT)

test:
	@$(MAKE) --no-print-directory test-$(COMPONENT)

lint:
	@$(MAKE) --no-print-directory lint-$(COMPONENT)

fmt:
	@$(MAKE) --no-print-directory fmt-$(COMPONENT)

migrate:
	@$(MAKE) --no-print-directory migrate-$(COMPONENT)

build:
	@$(MAKE) --no-print-directory build-$(COMPONENT)

up:
	@$(MAKE) --no-print-directory up-$(COMPONENT)

down:
	@$(MAKE) --no-print-directory down-$(COMPONENT)

logs:
	@$(MAKE) --no-print-directory logs-$(COMPONENT)

restart:
	@$(MAKE) --no-print-directory restart-$(COMPONENT)

shell:
	@$(MAKE) --no-print-directory shell-$(COMPONENT)

purge:
	@$(MAKE) --no-print-directory purge-$(COMPONENT)

keygen:
	@$(MAKE) --no-print-directory keygen-$(COMPONENT)

dev:
	@$(MAKE) --no-print-directory dev-$(COMPONENT)

# Provisioning a VPS to run the server. `make setup-server debian13`
# runs setup-scripts/$(COMPONENT)/run-all.sh (self-elevates with sudo).
# Pass flags with ARGS, e.g.: make setup-server debian13 ARGS="--skip-hardening"
setup-server:
	@if [ ! -d setup-scripts/$(COMPONENT) ]; then \
	    echo "ERROR: setup-scripts/$(COMPONENT)/ does not exist"; \
	    echo "       Usage: make setup-server <distro>  (available: $$(ls setup-scripts 2>/dev/null | tr '\n' ' '))"; \
	    exit 1; \
	fi
	@echo ">> Provisioning server with setup-scripts/$(COMPONENT)/run-all.sh"
	bash setup-scripts/$(COMPONENT)/run-all.sh $(ARGS)

# Bootstrap of the first owner on an already provisioned server. Issues the
# invitation and prints the /auth/accept/{token} link to register the passkey.
# `make bootstrap debian13` (optional: ARGS="--name Acme" for a new workspace)
bootstrap:
	@if [ ! -f setup-scripts/$(COMPONENT)/09-bootstrap-owner.sh ]; then \
	    echo "ERROR: setup-scripts/$(COMPONENT)/09-bootstrap-owner.sh does not exist"; \
	    echo "       Usage: make bootstrap <distro>  (available: $$(ls setup-scripts 2>/dev/null | tr '\n' ' '))"; \
	    exit 1; \
	fi
	bash setup-scripts/$(COMPONENT)/09-bootstrap-owner.sh $(ARGS)

# Guard: targets that invoke $(UV) require uv in PATH.
.uv-check:
	@if ! command -v $(UV) >/dev/null 2>&1; then \
	    echo "ERROR: '$(UV)' not found in PATH."; \
	    echo "  macOS:   brew install uv"; \
	    echo "  FreeBSD: pkg install uv"; \
	    echo "  Linux:   curl -LsSf https://astral.sh/uv/install.sh | sh"; \
	    echo "  Docs:    https://docs.astral.sh/uv/"; \
	    exit 1; \
	fi

# Guard: cryptography targets require an openssl in PATH that can generate
# Ed25519 keys. The capability is probed, not the presence: the LibreSSL that
# macOS ships in /usr/bin/openssl exists but does not generate Ed25519, and
# without this check keygen-server failed with an openssl error that does not
# name the cause. The probe key goes to stdout and is discarded: no file is
# written.
.openssl-check:
	@if ! command -v openssl >/dev/null 2>&1; then \
	    echo "ERROR: 'openssl' not found in PATH."; \
	    echo "  macOS:   brew install openssl and put it first in PATH"; \
	    echo "           (the LibreSSL in /usr/bin/openssl does not generate Ed25519 keys)"; \
	    echo "  FreeBSD: pkg install openssl"; \
	    echo "  Linux:   install your distro's openssl package"; \
	    exit 1; \
	fi
	@if ! openssl genpkey -algorithm Ed25519 >/dev/null 2>&1; then \
	    echo "ERROR: '$$(command -v openssl)' cannot generate Ed25519 keys."; \
	    echo "  Detected version: $$(openssl version 2>&1)"; \
	    echo "  macOS:   the LibreSSL in /usr/bin/openssl does not generate Ed25519."; \
	    echo "           brew install openssl and put it first in PATH:"; \
	    echo "           export PATH=\"\$$(brew --prefix openssl)/bin:\$$PATH\""; \
	    echo "  FreeBSD: pkg install openssl"; \
	    echo "  Linux:   install OpenSSL 1.1.1 or later from your distro"; \
	    exit 1; \
	fi

# ---------- server ----------
# Generates the server's Ed25519 key if it does not exist, derives the
# pubkey for the worker and ensures ED25519_PRIVATE_KEY_PATH in server/.env.
# Idempotent: never overwrites an existing key.
#   private → $(SERVER_KEY)
#   public  → $(WORKER_PUBKEY)
keygen-server: .openssl-check
	@if [ -f $(SERVER_KEY) ]; then \
	    echo ">> Private key already exists: $(SERVER_KEY) (unchanged)"; \
	else \
	    echo ">> Generating private Ed25519 key at $(SERVER_KEY)"; \
	    mkdir -p $(dir $(SERVER_KEY)); \
	    umask 077 && openssl genpkey -algorithm Ed25519 -out $(SERVER_KEY); \
	fi
	@if [ -f $(WORKER_PUBKEY) ]; then \
	    echo ">> Pubkey already exists: $(WORKER_PUBKEY) (unchanged)"; \
	else \
	    echo ">> Deriving pubkey at $(WORKER_PUBKEY)"; \
	    mkdir -p $(dir $(WORKER_PUBKEY)); \
	    openssl pkey -in $(SERVER_KEY) -pubout -out $(WORKER_PUBKEY); \
	fi
	@touch $(SERVER_ENV)
	@if grep -q '^ED25519_PRIVATE_KEY_PATH=' $(SERVER_ENV) 2>/dev/null; then \
	    echo ">> $(SERVER_ENV) already defines ED25519_PRIVATE_KEY_PATH (unchanged)"; \
	else \
	    echo "ED25519_PRIVATE_KEY_PATH=$(SERVER_KEY_REL)" >> $(SERVER_ENV); \
	    echo ">> Added ED25519_PRIVATE_KEY_PATH=$(SERVER_KEY_REL) to $(SERVER_ENV)"; \
	fi
	@echo ""
	@echo "   Summary:"
	@echo "     private → $(SERVER_KEY)"
	@echo "     public  → $(WORKER_PUBKEY)"
	@echo "     env     → $(SERVER_ENV) (ED25519_PRIVATE_KEY_PATH=$(SERVER_KEY_REL))"

up-server: keygen-server run-server

# Development shortcut: Postgres → migrations → keygen → uvicorn. Idempotent.
# Solves the typical case of starting the server with the DB down or unmigrated.
dev-server: db-up db-wait migrate-server up-server

run-server: .uv-check
	@echo ">> Starting uvicorn at http://$(BIND_HOST):$(BIND_PORT) (hot-reload)"
	cd $(SERVER_DIR) && $(UV) run uvicorn orcaster_server.main:app --reload --host $(BIND_HOST) --port $(BIND_PORT)

test-server: .uv-check
	@echo ">> Running pytest in $(SERVER_DIR)/  ARGS=$(ARGS)"
	cd $(SERVER_DIR) && $(UV) run --extra dev pytest $(ARGS)

lint-server: .uv-check
	@echo ">> Ruff check + format --check in $(SERVER_DIR)/"
	cd $(SERVER_DIR) && $(UV) run --extra dev ruff check . && $(UV) run --extra dev ruff format --check .

fmt-server: .uv-check
	@echo ">> Ruff format + check --fix in $(SERVER_DIR)/"
	cd $(SERVER_DIR) && $(UV) run --extra dev ruff format . && $(UV) run --extra dev ruff check --fix .

migrate-server: .uv-check
	@echo ">> alembic upgrade head in $(SERVER_DIR)/"
	cd $(SERVER_DIR) && $(UV) run orcaster-migrate

# ---------- client ----------
test-client: .uv-check
	@echo ">> Running pytest in $(CLIENT_DIR)/  ARGS=$(ARGS)"
	cd $(CLIENT_DIR) && $(UV) run --extra dev pytest $(ARGS)

lint-client: .uv-check
	@echo ">> Ruff check + format --check in $(CLIENT_DIR)/"
	cd $(CLIENT_DIR) && $(UV) run --extra dev ruff check . && $(UV) run --extra dev ruff format --check .

fmt-client: .uv-check
	@echo ">> Ruff format + check --fix in $(CLIENT_DIR)/"
	cd $(CLIENT_DIR) && $(UV) run --extra dev ruff format . && $(UV) run --extra dev ruff check --fix .

# ---------- orcaster (Go CLI) ----------
# Guard: Go targets require `go` in PATH.
.go-check:
	@if ! command -v $(GO) >/dev/null 2>&1; then \
	    echo "ERROR: '$(GO)' not found in PATH."; \
	    echo "  macOS:   brew install go"; \
	    echo "  FreeBSD: pkg install go"; \
	    echo "  Linux:   https://go.dev/dl/"; \
	    exit 1; \
	fi

test-orcaster: .go-check
	@echo ">> go test in $(ORCASTER_DIR)/  ARGS=$(ARGS)"
	cd $(ORCASTER_DIR) && $(GO) test ./... $(ARGS)

# Lint: gofmt + go vet always; golangci-lint if installed (CI does install it).
lint-orcaster: .go-check
	@echo ">> gofmt + go vet in $(ORCASTER_DIR)/"
	@unformatted=$$(cd $(ORCASTER_DIR) && gofmt -l .); \
	 if [ -n "$$unformatted" ]; then \
	    echo "ERROR: unformatted files (run 'make fmt orcaster'):"; echo "$$unformatted"; exit 1; \
	 fi
	cd $(ORCASTER_DIR) && $(GO) vet ./...
	@if command -v $(GOLANGCI) >/dev/null 2>&1; then \
	    echo ">> golangci-lint run"; cd $(ORCASTER_DIR) && $(GOLANGCI) run ./...; \
	 else \
	    echo ">> golangci-lint not installed; only gofmt+vet (brew install golangci-lint)."; \
	 fi

fmt-orcaster: .go-check
	@echo ">> gofmt -w in $(ORCASTER_DIR)/"
	cd $(ORCASTER_DIR) && gofmt -w .

# Builds a local binary at orcaster/orcaster. The version is derived from git
# (nearest tag or short hash); the official multi-platform release is made by
# goreleaser with the tag's version (see docs/releasing.md).
build-orcaster: .go-check
	@echo ">> go build -> $(ORCASTER_DIR)/orcaster"
	cd $(ORCASTER_DIR) && V="$$(git describe --tags --match 'v*' --always --dirty 2>/dev/null || echo dev)" && \
	    $(GO) build -ldflags "-X github.com/AInvirion/orcaster/orcaster/internal/api.Version=$$V" -o orcaster .

# Cross-compiles all platforms without publishing (validates the release config).
snapshot-orcaster: .go-check
	@command -v $(GORELEASER) >/dev/null 2>&1 || { echo "ERROR: goreleaser not found (brew install goreleaser)."; exit 1; }
	cd $(ORCASTER_DIR) && $(GORELEASER) build --snapshot --clean

# Every asset and the notes of a release, built locally without tagging or
# publishing (docs/releasing.md). VERSION=vX.Y.Z overrides the version of
# client/pyproject.toml. The release itself is published only by the release
# workflow, on a pushed v* tag.
release-dry-run: .go-check
	@command -v $(GORELEASER) >/dev/null 2>&1 || { echo "ERROR: goreleaser not found (brew install goreleaser)."; exit 1; }
	ops/release/dry-run.sh $(VERSION)

# Licenses of orcaster's module graph: allowlist (Apache-2.0, MIT, BSD-2/3,
# ISC) and one entry per module and version in the third-party notice. NOTICE
# points to the notice (by default THIRD_PARTY at the root). Requires
# go-licenses (go install github.com/google/go-licenses/v2@v2.0.1).
NOTICE ?= THIRD_PARTY
licenses-orcaster: .go-check
	ops/release/check-licenses.sh --notice $(NOTICE)

# ---------- client container (podman-only, no compose) ----------
# Guard: all the container targets require `podman` in PATH.
.podman-check:
	@if [ -z "$(PODMAN)" ]; then \
	    echo "ERROR: 'podman' not found in PATH."; \
	    echo "       Install Podman (https://podman.io) — this project is podman-only."; \
	    exit 1; \
	fi

# Guard for the EGRESS_ISOLATION=1 precondition: without aardvark-dns the worker
# does not resolve the name of the $(PROXY_NAME) sidecar in the --internal
# network and fails cryptically; with aardvark < 1.11.0 the DNS of --internal
# networks is forwarded to upstream resolvers and reopens an exfiltration
# channel via DNS ("Do not allow 'internal' networks to access DNS", release
# v1.11.0). It is queried through `podman info` and not through the filesystem
# because on a Mac the binary lives inside the podman machine, not on the host.
# It only hangs off up-proxy: with EGRESS_ISOLATION=0 `up client` does not go
# through here and nothing changes.
AARDVARK_MIN := 1.11.0
.aardvark-check: .podman-check
	@aardvark_ver="$$($(PODMAN) info --format '{{.Host.NetworkBackendInfo.DNS.Version}}' 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)+' | head -n1)"; \
	if [ -z "$$aardvark_ver" ]; then \
	    echo "ERROR: aardvark-dns not detected ('podman info' does not report host.networkBackendInfo.dns)."; \
	    echo "       EGRESS_ISOLATION=1 requires it: without aardvark the worker does not resolve the sidecar"; \
	    echo "       $(PROXY_NAME) in the --internal network."; \
	    echo "       On Debian: apt-get install aardvark-dns (it is a Recommends of netavark;"; \
	    echo "       --no-install-recommends leaves it out)."; \
	    exit 1; \
	fi; \
	if [ "$$(printf '%s\n%s\n' "$(AARDVARK_MIN)" "$$aardvark_ver" | sort -V | head -n1)" != "$(AARDVARK_MIN)" ]; then \
	    echo "ERROR: aardvark-dns $$aardvark_ver < $(AARDVARK_MIN)."; \
	    echo "       With earlier versions the DNS of --internal networks is forwarded to resolvers"; \
	    echo "       upstream and reopens an exfiltration channel via DNS."; \
	    exit 1; \
	fi

build-client: .podman-check
	@echo ">> Building the worker image ($(WORKER_IMAGE))"
	$(PODMAN) build --os linux --network=host -t $(WORKER_IMAGE) $(CLIENT_DIR)

# Idempotent creation of the networks and the volume of the egress topology.
# `network exists`/`volume exists` return non-zero if the resource is missing;
# the `-@...|| ...` follows the same pattern as keygen-server: it does not fail
# if they already exist, and it does not print the command (silent except on a
# real error).
egress-net: .podman-check
	-@$(PODMAN) network exists $(EGRESS_NET_IN)  || $(PODMAN) network create --internal $(EGRESS_NET_IN)
	-@$(PODMAN) network exists $(EGRESS_NET_OUT) || $(PODMAN) network create $(EGRESS_NET_OUT)
	-@$(PODMAN) volume exists  $(EGRESS_VOL)     || $(PODMAN) volume create $(EGRESS_VOL)
	@# Key of the signed allowlist. Unlike the lines above, a failure here
	@# aborts: without the key the proxy would accept an unsigned allowlist. The
	@# value travels through a pipe (never in argv nor in the log).
	@$(PODMAN) secret inspect $(EGRESS_KEY_SECRET) >/dev/null 2>&1 || { \
	    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | $(PODMAN) secret create $(EGRESS_KEY_SECRET) - >/dev/null \
	    && echo ">> Created the egress allowlist key (Podman secret $(EGRESS_KEY_SECRET))"; \
	} || { echo "ERROR: could not create the Podman secret $(EGRESS_KEY_SECRET) (egress allowlist key)." >&2; exit 1; }

# Probe of the systemd --user manager for the worker unit, as a prerequisite of
# `up-client` that runs BEFORE `up-proxy`: the unit stop lives in the
# `up-client` recipe, which starts once the sidecar has already been recreated,
# so without this probe an unreachable manager only aborted there, with the
# sidecar already touched. Under EGRESS_ISOLATION=1 it also probes for the
# sidecar's unit, to abort before the build even if the worker's does not exist.
# It stops nothing; it is a no-op without the unit or without systemctl.
.PHONY: .autostart-bus-check
.autostart-bus-check:
	@if [ "$(AUTOSTART_SYNC)" != "0" ]; then \
	    bash '$(USER_BUS_SH)' check "container-$(WORKER_NAME).service" "$(WORKER_NAME)" || exit 1; \
	    $(if $(filter up-proxy,$(UP_CLIENT_DEPS)),bash '$(USER_BUS_SH)' check "container-$(PROXY_NAME).service" "$(PROXY_NAME)" || exit 1; \
	    )true; \
	fi

# CONNECT proxy sidecar (egress_proxy.py). Connected to BOTH networks:
# `orcaster-egress` (where the worker talks to it) and `orcaster-egress-out`
# (where it tunnels to the Internet). Hardened like the worker (cap-drop,
# no-new-privileges, read-only); the source code is bind-mounted as in
# up-client (the image does not ship it) and needs the same PYTHONPATH.
# `--entrypoint python` because the image sets
# `ENTRYPOINT ["python", "-m", "orcaster_worker"]`: without the override, the
# module to run would be appended as an *argument* of `orcaster_worker`, not
# replace the module.
up-proxy: .podman-check .aardvark-check build-client egress-net
	@echo ">> Starting egress sidecar $(PROXY_NAME) (networks $(EGRESS_NET_IN) + $(EGRESS_NET_OUT))"
	@if [ "$(AUTOSTART_SYNC)" != "0" ]; then \
	    bash '$(USER_BUS_SH)' stop-unit "container-$(PROXY_NAME).service" "$(PROXY_NAME)" || exit 1; \
	fi
	-@$(PODMAN) rm -f $(PROXY_NAME) >/dev/null 2>&1 || true
	$(PODMAN) run -d \
	    --name $(PROXY_NAME) \
	    --restart unless-stopped \
	    --network $(EGRESS_NET_IN) \
	    --network $(EGRESS_NET_OUT) \
	    --cap-drop=ALL \
	    --security-opt=no-new-privileges \
	    --read-only \
	    --tmpfs /tmp:rw,size=64m,mode=1777 \
	    -e PYTHONPATH=/app/src \
	    -e PYTHONDONTWRITEBYTECODE=1 \
	    -e PYTHONUNBUFFERED=1 \
	    -e ORCASTER_PROXY_PORT=$(PROXY_PORT) \
	    -e ORCASTER_PROXY_BASE_ALLOW="$(ORCH_HOST),$(PROXY_BASE_ALLOW)" \
	    -e ORCASTER_EGRESS_ALLOW_FILE=/var/lib/orcaster-egress/allow.txt \
	    $(EGRESS_KEY_FLAG) \
	    -v $(EGRESS_VOL):/var/lib/orcaster-egress:ro \
	    -v $(WORKER_SRC):/app/src/orcaster_worker:ro \
	    --entrypoint python \
	    $(WORKER_IMAGE) \
	    -m orcaster_worker.egress_proxy
	@echo "   Done. Logs: $(PODMAN) logs -f $(PROXY_NAME)"

# Worker env file and pubkey: they exist and can be read. A file that exists
# but is not readable (the host's env.N is 0640 root:$WORKER_USER) is an
# explicit error: it never falls back to another path nor continues with an
# empty value (the `grep` for ORCH_HOST would swallow it and the proxy
# allowlist would be left empty). The development fallback warning goes to
# stderr with the path used.
.PHONY: .worker-config-check
.worker-config-check:
	@if [ "$(WORKER_ENV_SOURCE)" = dev ] || [ "$(WORKER_PUBKEY_SOURCE)" = dev ]; then \
	    echo ">> WARNING: no host config in $(CONFIG_DIR_NORM) (no env nor server-ed25519.pub): development fallback to $(WORKER_ENV) and $(WORKER_PUBKEY)." >&2; \
	fi
	@if [ ! -e $(WORKER_ENV) ]; then \
	    echo "ERROR: $(WORKER_ENV) is missing."; \
	    if [ "$(WORKER_ENV_SOURCE)" = dev ]; then \
	        echo "       Copy $(CLIENT_DIR)/.env.example to $(WORKER_ENV) and fill in the values"; \
	    else \
	        echo "       Create it on the host (setup-scripts/debian13-worker/03-worker-env.sh stamps it) or pass WORKER_ENV=/path/to/env to make."; \
	    fi; \
	    echo "       (remember each worker needs its own ORCH_ENROLLMENT_TOKEN)."; \
	    exit 1; \
	fi
	@if [ ! -f $(WORKER_ENV) ] || [ ! -r $(WORKER_ENV) ]; then \
	    echo "ERROR: $(WORKER_ENV) exists but is not a file readable by $$(id -un) (check permissions: the host's is 0640 root:<worker user>)."; \
	    exit 1; \
	fi
	@if [ ! -e $(WORKER_PUBKEY) ]; then \
	    echo "ERROR: $(WORKER_PUBKEY) is missing."; \
	    echo "       Every task comes signed; without the pubkey the worker rejects all tasks."; \
	    echo "       Get the pubkey on the server host:"; \
	    echo "         openssl pkey -in server/.dev-ed25519.pem -pubout -out server-ed25519.pub"; \
	    echo "       and copy it to $(WORKER_PUBKEY) (or pass WORKER_PUBKEY=/path/to/.pub to make)."; \
	    echo "       Details in client/README.md §\"Server public key\"."; \
	    exit 1; \
	fi
	@if [ ! -f $(WORKER_PUBKEY) ] || [ ! -r $(WORKER_PUBKEY) ]; then \
	    echo "ERROR: $(WORKER_PUBKEY) exists but is not a file readable by $$(id -un) (check permissions)."; \
	    exit 1; \
	fi

# Validation of WORKER_RO_MOUNTS. It is deliberately the FIRST prerequisite of
# up-client: an invalid entry, a nonexistent directory or one that the
# container uid cannot read stops here, before the build, the proxy recreation,
# the systemd unit stop and the `podman rm -f`. The running container is not
# touched.
#
# The prerequisite order is only honoured without `-j`: under `make -j` the
# `podman build` used to start in parallel with the validation. The
# `.NOTPARALLEL: up-client` below enforces it. In GNU Make >= 4.4 (Debian 13)
# it serializes only the prerequisites of up-client, in the listed order, and
# each one (with its own dependencies) waits for the previous one to finish. In
# GNU Make < 4.4 (macOS's 3.81) the prerequisites of .NOTPARALLEL are ignored
# and the whole invocation runs serially: same order, no parallelism.
#
# With an empty list it is a no-op (it does not even invoke bash), so a host
# that does not declare it stays exactly as before. The value travels between
# single quotes with the inner ones escaped: a quote in setup.env cannot break
# the line, the validator rejects it by naming it.
.worker-ro-mounts-check:
	$(if $(strip $(WORKER_RO_MOUNTS)),@bash '$(RO_MOUNTS_CHECK)' --host '$(subst ','\'',$(strip $(WORKER_RO_MOUNTS)))' || { echo "ERROR: WORKER_RO_MOUNTS did not pass validation (reason above). The container $(WORKER_NAME) was not touched."; exit 1; })

.NOTPARALLEL: up-client
# No healthcheck. `podman healthcheck run` launches its command with
# `podman exec`, which hands it the container's full environment
# (ORCH_ENROLLMENT_TOKEN included), and that process is dumpable until its
# execve: a task of the same uid reads it in /proc on every run, even with
# `env -i`. Nothing consumed the status (it only printed `healthy`), so it is
# removed. `--no-healthcheck` also overrides the HEALTHCHECK of images built
# earlier. A future liveness signal must not exec inside the container (e.g.
# have the host read the server's last_seen).
up-client: .worker-config-check .worker-ro-mounts-check .autostart-bus-check .podman-check build-client $(UP_CLIENT_DEPS)
	@echo ">> Starting worker $(WORKER_NAME) (dev bind-mount of src/)"
	$(if $(WORKER_MEMORY),,@printf '>> WARNING: the worker is created WITHOUT a memory cap. Reason: %s\n' '$(SETUP_ENV_DIAG)' >&2)
	$(if $(WORKER_RO_MOUNTS_DIAG),@printf '>> WARNING: could not read WORKER_RO_MOUNTS from the host setup.env: if it declares mounts the worker is created WITHOUT them. Reason: %s. Pass it explicitly with WORKER_RO_MOUNTS="/opt/example-ro".\n' '$(WORKER_RO_MOUNTS_DIAG)' >&2)
	@if [ "$(AUTOSTART_SYNC)" != "0" ]; then \
	    bash '$(USER_BUS_SH)' stop-unit "container-$(WORKER_NAME).service" "$(WORKER_NAME)" \
	        || { $(if $(filter up-proxy,$(UP_CLIENT_DEPS)),echo "       The $(PROXY_NAME) sidecar WAS recreated in this same command (it goes before the worker): its unit is re-synchronized when 'make up client' finishes successfully." >&2;) exit 1; }; \
	fi
	-@$(PODMAN) rm -f $(WORKER_NAME) >/dev/null 2>&1 || true
	$(PODMAN) run -d \
	    --name $(WORKER_NAME) \
	    --restart unless-stopped \
	    --env-file $(WORKER_ENV) \
	    $(EGRESS_CLIENT_FLAGS) \
	    --cap-drop=ALL \
	    --security-opt=no-new-privileges \
	    --read-only \
	    --mount type=tmpfs,dst=/home/orcaster,tmpfs-size=256m,tmpfs-mode=0700,U=true \
	    --pids-limit $(WORKER_PIDS_LIMIT) \
	    $(WORKER_MEMORY_FLAG) \
	    $(WORKER_CPUS_FLAG) \
	    -e PYTHONPATH=/app/src \
	    -e PYTHONDONTWRITEBYTECODE=1 \
	    -e PYTHONUNBUFFERED=1 \
	    -e HOST_HOSTNAME="$$(hostname)" \
	    -e TMPDIR=/var/cache/orcaster/tmp \
	    -v $(WORKER_DATA):/var/lib/orcaster \
	    -v $(WORKER_CACHE):/var/cache/orcaster \
	    -v $(WORKER_SRC):/app/src/orcaster_worker:ro \
	    -v $(WORKER_PUBKEY):/var/lib/orcaster/ed25519.pub:ro,z$(if $(WORKER_RO_MOUNT_FLAGS), $(WORKER_RO_MOUNT_FLAGS)) \
	    --no-healthcheck \
	    $(WORKER_IMAGE)
	@echo "   Done. Logs: make logs client"
	@$(MAKE) --no-print-directory autostart-sync-client N="$(strip $(N))" AUTOSTART_SYNC="$(AUTOSTART_SYNC)" AUTOSTART_RESYNC="$(AUTOSTART_RESYNC)"

# Re-synchronizes the autostart systemd --user unit after recreating the worker
# (see the comment on AUTOSTART_SYNC). No-op if autostart is not enabled on this
# host (the unit does not exist) or if there is no `systemctl` (dev/CI). It runs
# as the user invoking `make` (orcaster in production), who owns the --user
# unit: it does not need root.
#
# A failure here DOES make `up-client` fail. The container has already been
# recreated, but the systemd --user manager still has the old unit loaded and a
# MainPID that no longer exists: the next `daemon-reload` would stop the worker.
# Reporting success over that state is what let a worker go down in production;
# the warning on how to re-synchronize by hand is still printed, and the target
# exits non-zero. AUTOSTART_SYNC_FATAL=0 leaves it as a warning (exits 0) for
# whoever verifies the unit on their own afterwards. The most frequent cause (a
# shell without XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS) no longer gets this
# far: the recipe's stop step (user-bus.sh) derives the variables or aborts
# before the `podman rm -f`, and 06-autostart.sh probes the manager before
# rewriting the unit.
#
# CONFIG_DIR is passed EXPLICITLY in the invocation: 06-autostart.sh resolves
# the host's setup.env with `load_env`, which falls back to the lib.sh default
# when the variable does not arrive. On a host whose config does not live in
# /etc/orcaster that ended with the script dead asking for
# /etc/orcaster/setup.env, the `|| echo` swallowing the failure and the unit
# not regenerated (see the CONFIG_DIR comment above).
#
# The fallback message does NOT simply repeat the $(CONFIG_DIR) that just
# failed: the dominant reason 06-autostart.sh dies is precisely that this value
# points at the wrong directory, because nothing persists the host's CONFIG_DIR
# in an interactive shell, so the one the Makefile resolves is its default
# /etc/orcaster. Suggesting a well-formed command carrying the same value is
# suggesting a command that fails identically. So the message separates what it
# knows (the value that was tried, and N, which comes from the invocation
# itself) from what it cannot know (where this host's config really lives), and
# leaves that gap as a placeholder for the operator to fill in.
#
# The fallback command is `sudo env VAR=value bash ...` and not
# `sudo bash ... N=<n>`: 06-autostart.sh reads N and CONFIG_DIR from the
# ENVIRONMENT, so passed as positional arguments they are silently ignored, and
# `sudo VAR=value cmd` is rejected by sudo unless sudoers allows it. It is the
# same form used by the worker migration to tarball. It does not carry
# `RUN_AS=`: that command is run as root, where the default `as_worker` (drop
# to the worker user via sudo) is the right path, unlike the hook, which
# already runs AS the worker. The path is that of root's copy of the release and
# not the relative $(SETUP_WORKER_DIR): this target runs from INSTALL_DIR, which
# belongs to the worker user, and root does not execute code from that tree.
#
# The message prefix follows the exit code: ERROR when the target exits
# non-zero (AUTOSTART_SYNC_FATAL=1, default) and WARNING when it exits 0
# (AUTOSTART_SYNC_FATAL=0). An ERROR with exit 0 reads as a failed deployment in
# the release log. The prefix is decided with the SAME comparison that decides
# the exit, so they cannot diverge; the text after the prefix does not change,
# so greps on it keep finding it.
autostart-sync-client:
	@[ "$(AUTOSTART_SYNC)" != "0" ] && [ "$(AUTOSTART_RESYNC)" != "0" ] || exit 0; \
	unit="container-$(WORKER_NAME).service"; \
	ucfg="$${XDG_CONFIG_HOME:-$$HOME/.config}/systemd/user/$$unit"; \
	if command -v systemctl >/dev/null 2>&1 && [ -f "$$ucfg" ]; then \
	    echo ">> Autostart already enabled ($$unit): re-synchronizing the systemd --user unit after recreating"; \
	    RUN_AS= N="$(strip $(N))" CONFIG_DIR="$(CONFIG_DIR)" bash $(SETUP_WORKER_DIR)/06-autostart.sh \
	        || { pfx=ERROR; [ "$(AUTOSTART_SYNC_FATAL)" != "0" ] || pfx=WARNING; \
	          printf '%s\n' \
	            "$$pfx: Could not re-sync the autostart ($$unit). Tried with CONFIG_DIR=$(CONFIG_DIR)." \
	            "   The container $(WORKER_NAME) WAS recreated and is running, but its systemd --user unit is not in sync:" \
	            "   until it is re-synchronized, do NOT run 'systemctl --user daemon-reload' on this host (it may stop the worker) and boot startup is not guaranteed." \
	            "   If this host's config does not live in $(CONFIG_DIR), that is the most likely cause: nothing persists the host's real CONFIG_DIR in an interactive shell, so an invocation that does not pass it falls back to the Makefile default." \
	            "   Run by hand passing this host's REAL CONFIG_DIR (the directory that contains its setup.env):" \
	            "     sudo env CONFIG_DIR=<host-config-dir>$(if $(strip $(N)), N=$(strip $(N)),) bash /root/orcaster/$(SETUP_WORKER_DIR)/06-autostart.sh" \
	            "   (root's copy of the release that install-release.sh leaves; if this host declares another SOURCE_DIR, use that path. Never the 06-autostart.sh in this directory: it belongs to the worker user.)" >&2; \
	          [ "$(AUTOSTART_SYNC_FATAL)" = "0" ] || exit 1; }; \
	fi

# Brings the worker down and ALSO its egress sidecar. The $(PROXY_NAME) sidecar
# is per-worker (it carries the same N suffix as the worker container) and is
# created by `up-client`, through the up-proxy dependency that EGRESS_ISOLATION=1
# adds: it never exists without its worker, so it must not outlive it either.
# The two concrete failures that deleting it here avoids: the orphaned proxy
# keeps occupying the egress networks (and its autostart unit raises it again at
# boot, pointing at a worker that no longer exists), and on a tarball-deployed
# host its `-v $(WORKER_SRC):/app/src/orcaster_worker:ro` is left pointing at
# the inode of the previous release after the swap, i.e. serving egress with the
# old code. `run-fleet.sh` dismantles surplus workers with
# `make down client N=n`, so that path was precisely the one that left the
# sidecar dangling.
#
# It is not conditioned on EGRESS_ISOLATION: that flag describes how it is
# STARTED, and a worker created with isolation is brought down all the same from
# a shell where the flag is not set (or with the inherited opt-out). Listing by
# name already makes the no-sidecar case a silent no-op.
#
# Volumes are NOT touched here: they hold the worker's identity (`state.json` in
# $(WORKER_DATA)) and deleting them is `purge-client`, which is destructive and
# is requested separately. The egress networks are touched by NEITHER of the
# two: they are shared by the whole fleet (they carry no N suffix) and are
# created by `egress-net`, which is idempotent.
#
# The outcome of each container is reported separately: the
# `xargs -r podman rm -f >/dev/null 2>&1 || true` that used to be here hid
# podman's stderr and the `|| true` swallowed the exit code, so a container
# that podman refused to delete (locked storage, broken state) was reported as
# "removed" right after anyway, with the same unconditional `echo` that had
# already been fixed for the volumes in `purge-client` but not for the
# containers of this recipe.
#
# Scope. By default (`N` empty, `ALL` unset) the pattern is anchored exactly to
# the unsuffixed pair, $(WORKER_NAME) and $(PROXY_NAME): the same pair that a
# bare `make up client` creates. Previously, an empty `N` also swept every
# orcaster-worker-<n> and orcaster-proxy-<n> on the host, so the documented
# single-worker recipes (`make down client && make up client` to rebuild or
# rotate the API key) brought the whole fleet down and `up client` started only
# one: the other workers were left down and without their egress proxies.
#
# With `N` set, WORKER_NAME and PROXY_NAME already carry the suffix and the
# pattern is anchored exactly like `purge-client`: `down client N=2` asks for
# one worker and only that one (it is the run-fleet.sh path, which dismantles
# the surplus ones one by one with `make down client N=n`).
#
# Sweeping the whole fleet is requested explicitly with `ALL=1`: it keeps the
# optional numeric suffix `(-[0-9]+)?` and lists the containers it is going to
# delete before deleting them, so the operator sees the scope. `ALL=1` together
# with `N` is contradictory (one worker or all) and is rejected with exit 2
# instead of silently choosing one; an `ALL` with any other value is rejected too.
comma        := ,
DOWN_ALL     := $(filter 1,$(strip $(ALL)))
DOWN_PATTERN := ^($(WORKER_NAME)|$(PROXY_NAME))$(if $(DOWN_ALL),(-[0-9]+)?,)$$
down-client: .podman-check
	@if [ -n "$(strip $(ALL))" ] && [ "$(strip $(ALL))" != "1" ]; then \
	    echo "!! ALL='$(strip $(ALL))' is not valid: sweeping all the workers on the host is requested with ALL=1." >&2; \
	    exit 2; \
	fi
	@if [ -n "$(DOWN_ALL)" ] && [ -n "$(strip $(N))" ]; then \
	    echo "!! ALL=1 and N=$(strip $(N)) are mutually exclusive: N brings down one worker, ALL=1 sweeps all those on the host." >&2; \
	    exit 2; \
	fi
	@echo ">> Stopping and removing $(if $(DOWN_ALL),ALL the workers + egress sidecars on the host ($(WORKER_NAME) + $(WORKER_NAME)-*$(comma) $(PROXY_NAME) + $(PROXY_NAME)-*),the worker + its egress sidecar ($(WORKER_NAME)$(comma) $(PROXY_NAME)))"
	-@names=$$($(PODMAN) ps -a --format '{{.Names}}' | grep -E "$(DOWN_PATTERN)" || true); \
	if [ -n "$$names" ]; then \
	    $(if $(DOWN_ALL),echo "   containers to remove (ALL=1):"; for n in $$names; do echo "     - $$n"; done;) \
	    for n in $$names; do \
	        if $(PODMAN) rm -f "$$n" >/dev/null 2>&1; then \
	            echo "   removed: $$n"; \
	        else \
	            echo "!! Could not remove container $$n (does it have a stuck process or locked storage?). Retry by hand: $(PODMAN) rm -f $$n"; \
	        fi; \
	    done; \
	else \
	    echo "   (none found)"; \
	fi

logs-client: .podman-check
	@$(PODMAN) logs -f $(WORKER_NAME)

restart-client: .podman-check
	@echo ">> Restarting $(WORKER_NAME) (applies edits without a rebuild)"
	@$(PODMAN) restart $(WORKER_NAME) >/dev/null
	@echo "   OK"

shell-client: .podman-check
	@$(PODMAN) exec -it $(WORKER_NAME) /bin/bash

# Purges the requested worker: its container, its egress sidecar and the volumes
# that are its own. The $(PROXY_NAME) sidecar and the $(EGRESS_VOL) volume are
# included here: both are PER-WORKER (they carry the same N suffix, see the
# comment on EGRESS_VOL above) and are created by `up-proxy`/`egress-net` as
# part of bringing up THIS worker, so deleting the worker and leaving them alive
# produced exactly the orphaned sidecar that `down-client` exists to avoid:
# occupying the egress networks, raised again at boot by its autostart unit and,
# on a tarball-deployed host, with its code bind mount pointing at the inode of
# the previous release. `make purge client N=n` was a sanctioned path to that
# state.
#
# The $(EGRESS_NET_IN)/$(EGRESS_NET_OUT) networks are NOT deleted: they are the
# only pieces of the topology SHARED by the whole fleet (they carry no N suffix,
# they are only wiring), so deleting them when purging one worker would cut the
# egress of those that keep running. They are left empty when the whole fleet is
# dismantled and are cleaned by hand (`podman network rm`); `egress-net`
# recreates them if missing.
#
# The name is ANCHORED without the optional suffix `(-[0-9]+)?`, ALWAYS, with or
# without N: purge is destructive and its volumes are per-N, so sweeping the
# containers of all the workers while deleting only one's volumes would leave
# the others' data orphaned, silently. Purge is requested worker by worker.
# `down-client` is anchored the same way except with `ALL=1`, which sweeps the
# whole fleet with the `(-[0-9]+)?` and can do so because it is reversible with
# an `up client`.
#
# Each volume reports its outcome: the `volume rm ... >/dev/null || true` that
# used to be here leaves the operator believing the worker's identity was purged
# when podman refused (the volume is still used by another container), and the
# next `up client` revives the old `state.json`. "Does not exist" and "could
# not be deleted" are different states and neither can stay silent.
#
# The same rule was missing for the containers, in the SAME recipe: the
# `xargs -r podman rm -f >/dev/null 2>&1 || true` further down discarded
# podman's stderr and the `|| true` swallowed the exit code, so
# `make purge client N=n` with a container that podman refused to delete
# (locked storage, broken state) still printed "removed" for that name. The
# container loop reports each one separately, like the volume one.
purge-client: .podman-check
	@echo ">> Purging worker: containers $(WORKER_NAME) + $(PROXY_NAME), volumes $(WORKER_DATA), $(WORKER_CACHE), $(EGRESS_VOL), secret $(EGRESS_KEY_SECRET)"
	-@names=$$($(PODMAN) ps -a --format '{{.Names}}' | grep -E "^($(WORKER_NAME)|$(PROXY_NAME))$$" || true); \
	if [ -n "$$names" ]; then \
	    for n in $$names; do \
	        if $(PODMAN) rm -f "$$n" >/dev/null 2>&1; then \
	            echo "   removed: $$n"; \
	        else \
	            echo "!! Could not remove container $$n (does it have a stuck process or locked storage?). Retry by hand: $(PODMAN) rm -f $$n"; \
	        fi; \
	    done; \
	else \
	    echo "   (no container found)"; \
	fi
	-@for v in $(WORKER_DATA) $(WORKER_CACHE) $(EGRESS_VOL); do \
	    if ! $(PODMAN) volume exists "$$v" >/dev/null 2>&1; then \
	        echo "   (volume $$v does not exist)"; \
	    elif $(PODMAN) volume rm "$$v" >/dev/null 2>&1; then \
	        echo "   removed: volume $$v"; \
	    else \
	        echo "!! Could not delete volume $$v (is another container using it?). Retry by hand: $(PODMAN) volume rm $$v"; \
	    fi; \
	done
	-@if ! $(PODMAN) secret inspect $(EGRESS_KEY_SECRET) >/dev/null 2>&1; then \
	    echo "   (secret $(EGRESS_KEY_SECRET) does not exist)"; \
	elif $(PODMAN) secret rm $(EGRESS_KEY_SECRET) >/dev/null 2>&1; then \
	    echo "   removed: secret $(EGRESS_KEY_SECRET)"; \
	else \
	    echo "!! Could not delete secret $(EGRESS_KEY_SECRET). Retry by hand: $(PODMAN) secret rm $(EGRESS_KEY_SECRET)"; \
	fi
	@echo "   OK"

# ---------- Postgres dev ----------
db-up: .podman-check
	@echo ">> Starting dev Postgres with $(PODMAN) ($(PG_NAME))"
	@if $(PODMAN) container exists $(PG_NAME) 2>/dev/null || $(PODMAN) ps -a --format '{{.Names}}' | grep -q '^$(PG_NAME)$$'; then \
		echo "   container already exists, starting..."; \
		$(PODMAN) start $(PG_NAME) >/dev/null; \
	else \
		$(PODMAN) run -d --name $(PG_NAME) \
			-e POSTGRES_USER=$(PG_USER) \
			-e POSTGRES_PASSWORD=$(PG_PASS) \
			-e POSTGRES_DB=$(PG_DB) \
			-p $(PG_PORT):5432 \
			-v $(PG_NAME)-data:/var/lib/postgresql/data \
			$(PG_IMAGE) >/dev/null; \
	fi
	@echo ""
	@echo "   Done. Export:"
	@echo "   export DATABASE_URL='$(DEV_DB_URL)'"

db-wait: .podman-check
	@echo ">> Waiting for Postgres ($(PG_NAME)) to accept connections..."
	@i=0; until $(PODMAN) exec $(PG_NAME) pg_isready -U $(PG_USER) -d $(PG_DB) -q >/dev/null 2>&1; do \
	    i=$$((i+1)); \
	    if [ $$i -ge 30 ]; then \
	        echo "   ERROR: Postgres did not respond after 30 attempts."; \
	        $(PODMAN) logs --tail 20 $(PG_NAME) || true; \
	        exit 1; \
	    fi; \
	    sleep 1; \
	done
	@echo "   OK"

db-down:
	@echo ">> Stopping and removing $(PG_NAME)"
	-@$(PODMAN) stop $(PG_NAME) >/dev/null 2>&1 || true
	-@$(PODMAN) rm $(PG_NAME) >/dev/null 2>&1 || true

db-shell:
	@$(PODMAN) exec -it $(PG_NAME) psql -U $(PG_USER) -d $(PG_DB)

clean:
	@echo ">> Cleaning caches"
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) -prune -exec rm -rf {} + 2>/dev/null || true
