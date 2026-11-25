#!/usr/bin/env bash
# 02 — The server's Ed25519 public key. Mandatory: every task comes signed;
# without the pubkey the worker silently rejects every task.
# It leaves the key at CONFIG_DIR/server-ed25519.pub (outside the checkout),
# where the Makefile mounts it in the container as /var/lib/orcaster/ed25519.pub.
set -euo pipefail
# shellcheck source=lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"
need_root "$@"
load_env

step "02 · Server Ed25519 public key"

# Validation is mandatory on both paths (file already present and write from
# setup.env), so without openssl no check is possible. Abort naming the cause:
# degrading to "present = good" is exactly the silent false negative this script
# exists to eliminate. 00-preflight.sh installs openssl.
command -v openssl >/dev/null 2>&1 \
    || die "'openssl' is missing from PATH and without it I cannot validate the pubkey. Run 00-preflight.sh."

# openssl rejects anything that is not a readable PEM public key: a truncated
# file, loose text, a private key. Its stderr tells the operator nothing, so it
# is discarded and our own message explains what to do.
pubkey_is_valid() { openssl pkey -pubin -in "$1" -noout >/dev/null 2>&1; }

# SHA-256 fingerprint of the DER of the public key read from stdin. Compares
# IDENTITY, not bytes: the same PEM can differ in CRLF or in the trailing
# newline and still be the SAME key, and a textual `cmp` would treat that as a
# rotation, rewriting the file on every pass.
#
# Fails (rc != 0) when the input is not a public key. That check is not
# redundant: a failing `openssl pkey` writes nothing, and the `openssl dgst` on
# the right would then return the fingerprint of the empty string, which would
# be read as "another key" instead of "nothing to compare against".
pubkey_fingerprint() { openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256 | awk '{print $NF}'; }

# Is the file already in place the SAME key that setup.env declares? Without this
# comparison, a well-formed file from ANOTHER server wins forever: rotating the
# server key this way was impossible, and a golden snapshot baked against
# staging left the whole fleet enrolling fine and silently rejecting every
# signed task (docs/user-manual.md, "the wrong key, from another server").
#
# Also returns 0 ("nothing to replace") when there is nothing to compare
# against, and in the second case it says so:
#   - SERVER_ED25519_PUBKEY empty: the file placed by hand (Option B of the
#     help text below) is the host's only source of truth. Requiring setup.env
#     here would break a legitimate, documented path.
#   - SERVER_ED25519_PUBKEY present but unreadable: the FILE's key works, so
#     aborting would leave a host that works today without a worker. It
#     continues, warning that the comparison could not be made.
pubkey_is_declared() {
    local file_fingerprint declared_fingerprint
    [ -n "$SERVER_ED25519_PUBKEY" ] || return 0
    declared_fingerprint="$(printf '%s\n' "$SERVER_ED25519_PUBKEY" | pubkey_fingerprint)" || {
        warn "SERVER_ED25519_PUBKEY in setup.env is not a readable public key, so I cannot confirm that $1 is the server's key: the file is left as it is (it is valid). Paste the correct PEM block into setup.env."
        return 0
    }
    file_fingerprint="$(pubkey_fingerprint < "$1")" \
        || die "Could not compute the fingerprint of $1 even though openssl accepts it as a public key. Check the host's openssl."
    [ "$file_fingerprint" = "$declared_fingerprint" ]
}

# Way out, common to the two possible aborts.
pubkey_help="    Option A: paste the PEM block into SERVER_ED25519_PUBKEY inside setup.env
              and run this script again.
    Option B: replace the file by hand at $CLIENT_PUBKEY.
    To get it on the server host:
        openssl pkey -in server/.dev-ed25519.pem -pubout -out server-ed25519.pub"

if [ -s "$CLIENT_PUBKEY" ] && pubkey_is_valid "$CLIENT_PUBKEY" \
   && pubkey_is_declared "$CLIENT_PUBKEY"; then
    ok "pubkey already present and valid: $CLIENT_PUBKEY (content unchanged)."
elif [ -n "$SERVER_ED25519_PUBKEY" ]; then
    # "Present but invalid" is treated the same as "absent": the rerun repairs it.
    # That state arrives on its own, without anyone making a mistake: any host
    # provisioned by a version prior to this validation leaves it, and so does
    # step 2 of the migration runbook, which copies the file by hand without
    # validating anything. Dying here would force the operator to delete the
    # file to be able to fix it, with the good key at hand in setup.env.
    # Repairing is the purpose of an idempotent script; it aborts only when
    # there is nowhere to get a good key from.
    # The two causes are named differently: a VALID file that falls into this
    # branch is not broken, it belongs to another server, and telling the
    # operator it "is not a valid public key" sends them looking for the wrong
    # problem.
    if [ -s "$CLIENT_PUBKEY" ]; then
        if pubkey_is_valid "$CLIENT_PUBKEY"; then
            warn "$CLIENT_PUBKEY is a valid public key but it is NOT the one declared by SERVER_ED25519_PUBKEY in setup.env (different SHA-256 fingerprints); it is replaced with the declared one. If the file's key was the right one, fix setup.env before the next rerun."
        else
            warn "$CLIENT_PUBKEY is not a valid public key; it is replaced with SERVER_ED25519_PUBKEY from setup.env."
        fi
    fi
    # Braces around ${CLIENT_PUBKEY}: without them the bytes of '…' stick to the
    # variable name and bash 3.2 aborts with "unbound variable" under `set -u`.
    log "Writing pubkey from setup.env to ${CLIENT_PUBKEY}…"
    # Validate BEFORE writing, not after: writing first left the invalid file at
    # its final path even if the script died, and the next rerun took it as good
    # through the `[ -s "$CLIENT_PUBKEY" ]` check above ("pubkey already present,
    # unchanged"). The error would only reappear at runtime, with the worker
    # silently rejecting every signed task, which is the failure this script
    # exists to prevent.
    printf '%s\n' "$SERVER_ED25519_PUBKEY" | openssl pkey -pubin -noout 2>/dev/null \
        || die "The content of SERVER_ED25519_PUBKEY is not a valid public key.
$pubkey_help"
    install -d -m 0750 -o root -g "$WORKER_USER" "$CONFIG_DIR"
    printf '%s\n' "$SERVER_ED25519_PUBKEY" > "$CLIENT_PUBKEY"
    ok "pubkey validated and written."
elif [ -s "$CLIENT_PUBKEY" ]; then
    die "The content of $CLIENT_PUBKEY is not a valid public key and
    SERVER_ED25519_PUBKEY is empty in setup.env: there is nothing to repair it with.
$pubkey_help"
else
    die "There is no server pubkey.
$pubkey_help"
fi

# Ownership and mode, reconciled on EVERY pass and not only the one that writes.
# We only get here through the two branches that leave a file (the other two
# die), so the file exists by construction. The worker runs as $WORKER_USER and
# reads the pubkey through the group: a file left root:root 0600 (what a manual
# `cp`/`scp` under umask 077 leaves, or any deviation from the `install -m 0644
# -o root -g orcaster` of step 2 of the migration runbook) makes the worker
# unable to read it and silently reject every signed task, while this script
# reported "unchanged" in green. Nobody else notices: 05-smoke-test.sh runs as
# root (reads any mode) and ops/release/install-release.sh only checks that the
# file exists. It is the same criterion already applied to the other three host
# config files: 00-preflight.sh with setup.env, 03-worker-env.sh with env and
# run-fleet.sh with each env.N. The contract (0644 root:$WORKER_USER) is declared
# in this suite's README.
#
# Fatal and with the cause, never best-effort: a chown that fails silently
# fabricates exactly the failure this block exists to avoid.
if ! chown "root:$WORKER_USER" "$CLIENT_PUBKEY" || ! chmod 0644 "$CLIENT_PUBKEY"; then
    die "Could not leave $CLIENT_PUBKEY as 0644 root:$WORKER_USER.
    The worker runs as $WORKER_USER and reads it through the group: without
    those permissions it silently rejects every signed task."
fi
