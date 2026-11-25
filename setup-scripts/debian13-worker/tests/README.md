# Tests for the debian13-worker suite

`bats` tests for the pure functions of `lib.sh` and for the mocked orchestration
of `run-fleet.sh`, `06-autostart.sh`, `cloud-init-worker.sh` (golden snapshot
detection, early validations, `setup.env`), `03-worker-env.sh` (stamping of the
base `.env`), `02-server-pubkey.sh` plus the artifact check of
`05-smoke-test.sh` (validation of the server pubkey with `openssl`: present and
valid, present and poisoned, repair from `setup.env`) and `fleet-disk-check.sh`
(parsing/verdict/exit code of the fleet disk check, with `ssh` stubbed, never
against real hosts) and `check-ro-mounts.sh` (validator of `WORKER_RO_MOUNTS`,
plus its use in the root Makefile's `up-client` target against a fake
`podman`). The `WORKER_RO_MOUNTS` tests create their mountable directories
inside this directory (`ro-fixture.*`, removed in the teardown): a `mktemp -d`
falls under `/var` or `/tmp`, paths the validator rejects on purpose. They need
no root, Podman or server: they run on any machine with `bats`.

`test_server_pubkey.bats` generates real ed25519 keys with `openssl genpkey`
instead of fabricating PEM blocks by hand: the validation it tests is precisely
`openssl`'s, so an invented fixture would prove nothing.

## Run

    brew install bats-core        # Mac (or: apt-get install -y bats  on Debian)
    bats setup-scripts/debian13-worker/tests/

## What they do NOT cover

Real provisioning (apt/make/podman) and container startup are verified with
`shellcheck -x`, `bash -n` and an end-to-end run on a disposable VPS. These
tests cover the pure logic and the control-flow decisions with mocks.
