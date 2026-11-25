"""Task secrets keys: generate and rotate.

Usage:
    orcaster-task-secrets-keys generate
    orcaster-task-secrets-keys rotate \\
        [--keys-file PATH [--confirm-server-key-id ID]]

`generate` prints a new Fernet key (one line), ready to add to the
`TASK_SECRETS_KEYS_PATH` file.

`rotate` re-encrypts every row of `task_secrets` with the primary key (the first
in the file) through `MultiFernet.rotate` and recomputes its fingerprint, which
derives from the primary. In the same transaction it re-encrypts the
`token_ciphertext` of the active share links with their derived keys
(`keyring.share_links`); rows without ciphertext are skipped (created before
0058, minted without keys, or revoked). It prints how many rows it touched in
each table, never values. If any row cannot be re-encrypted nothing changes and
it names the affected rows, split by output: those that no key in the file
decrypts (restore the old key or revoke) and the share links that no key fixes
(revoke): a token that does not match its `token_hash`, that is not UTF-8,
encrypted with the raw keys, or bytes that are not a Fernet token.

Rotation procedure: add the new key at the top of the file the server loads,
restart the server, run `rotate` and retire the old key.

Before rotating, `rotate` prints the non-secret id of the primary key of the
file it is going to use and that of all its keys (`keyring.key_id`: 12
hexadecimal characters that do not reveal the key). The server logs the same
ids at startup (`task secrets keys loaded ... key ids=...`) and when it reloads
the file.

`--keys-file PATH` replaces `TASK_SECRETS_KEYS_PATH` for this run. It is needed
when the environment (or `.env`) points to `$CREDENTIALS_DIRECTORY`, which only
exists inside the systemd service: from a shell, pass the path of a decrypted
copy of the credential (see the user manual).

`--keys-file` guard: rotating with a primary that the server does not have
leaves all the secrets unreadable for it, and they are lost if the copy is
deleted. Therefore, before opening the database:
- if `TASK_SECRETS_KEYS_PATH` is set and can be read from this shell, the
  primary of `--keys-file` must be among the keys of that file; otherwise,
  nothing is rotated;
- if it is not set or cannot be read from this shell (the case of
  `$CREDENTIALS_DIRECTORY`), the script cannot verify: it prints the id of the
  primary and requires `--confirm-server-key-id ID` with that same id, which the
  operator passes only after finding it in the server's startup line.
Without `--keys-file` the script reads the same file as the server and there is
no guard.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from orcaster_server.config import settings
from orcaster_server.status.tokens import hash_share_token
from orcaster_server.task_secrets.keyring import KeyringError, TaskSecretsKeyring, load_keyring


class RotateError(RuntimeError):
    """Rows that cannot be re-encrypted: the rotation applies nothing."""


# Decoded Fernet token: version (1) + timestamp (8) + IV (16) + at least one AES
# block (16) + HMAC (32); the encrypted body comes in blocks of 16.
_FERNET_VERSION = 0x80
_FERNET_OVERHEAD = 1 + 8 + 16 + 32
_FERNET_MIN_LEN = _FERNET_OVERHEAD + 16


def _is_fernet_token(ciphertext: bytes) -> bool:
    """`True` if the bytes have the shape of a Fernet token (without verifying the HMAC).

    It distinguishes a ciphertext that no key in the file opens (restoring the
    old key fixes it) from bytes that are not a token (only revoking is left).
    """
    try:
        data = base64.b64decode(ciphertext, altchars=b"-_", validate=True)
    except ValueError:
        return False
    return (
        len(data) >= _FERNET_MIN_LEN
        and data[0] == _FERNET_VERSION
        and (len(data) - _FERNET_OVERHEAD) % 16 == 0
    )


def _share_link_unfixable_reason(keyring: TaskSecretsKeyring, ciphertext: bytes) -> str | None:
    """Cause why a ciphertext that `share_links` does not open cannot be fixed with a key.

    `None` if it is a Fernet token that no key decrypts: restoring the old key
    fixes it. If the raw keys open it, it is a ciphertext from the secrets domain
    (or from the format prior to derived keys) and no key turns it into a
    share-link token.
    """
    try:
        keyring.decrypt(ciphertext)
    except InvalidToken:
        pass
    else:
        return "encrypted with the raw task secret keys: old format or a task secret"
    if not _is_fernet_token(ciphertext):
        return "not a Fernet token"
    return None


@dataclass(frozen=True)
class RotateResult:
    """Rows re-encrypted per table."""

    task_secrets: int
    share_links: int


async def rotate_all(engine: AsyncEngine, keyring: TaskSecretsKeyring) -> RotateResult:
    """Re-encrypt with the primary key the secrets (recomputing their fingerprint) and
    the token of the active share links that have ciphertext. All or nothing."""
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT s.id, w.slug, s.name, s.ciphertext,
                           CAST(s.worker_id AS text) AS worker_id
                      FROM task_secrets s
                      JOIN workspaces w ON w.id = s.workspace_id
                     ORDER BY w.slug, s.name, s.worker_id NULLS FIRST
                       FOR UPDATE OF s
                    """
                )
            )
        ).all()
        rotated: list[dict] = []
        failed: list[str] = []
        for row in rows:
            try:
                ciphertext = bytes(row.ciphertext)
                value = keyring.decrypt(ciphertext).decode("utf-8")
                rotated.append(
                    {
                        "id": row.id,
                        "ct": keyring.rotate(ciphertext),
                        "fp": keyring.fingerprint(value),
                    }
                )
            except (InvalidToken, UnicodeDecodeError):
                # A name can repeat per worker; tell them apart.
                scope = f" (worker {row.worker_id})" if row.worker_id else ""
                failed.append(f"{row.slug}/{row.name}{scope}")
        # Only active rows with ciphertext. A revoked one is never decrypted
        # (the UI does not show its URL; revoke also deletes the ciphertext).
        share_rows = (
            await conn.execute(
                text(
                    """
                    SELECT t.id::text AS id, w.slug, t.token_hash, t.token_ciphertext
                      FROM workspace_share_token t
                      JOIN workspaces w ON w.id = t.workspace_id
                     WHERE t.token_ciphertext IS NOT NULL
                       AND t.revoked_at IS NULL
                     ORDER BY w.slug, t.id
                       FOR UPDATE OF t
                    """
                )
            )
        ).all()
        rotated_links: list[dict] = []
        # Share links that no key fixes (only revoking is left), with their cause.
        unfixable_links: list[str] = []
        for row in share_rows:
            # Same criterion as the secrets: it is decrypted and decoded before
            # re-encrypting, and the token must also match the row's hash;
            # otherwise, rotating it would only propagate a URL the UI cannot show.
            name = f"{row.slug}/share-link:{row.id}"
            ciphertext = bytes(row.token_ciphertext)
            try:
                plaintext = keyring.share_links.decrypt(ciphertext)
            except InvalidToken:
                reason = _share_link_unfixable_reason(keyring, ciphertext)
                if reason is None:
                    failed.append(name)
                else:
                    unfixable_links.append(f"{name} ({reason})")
                continue
            try:
                token = plaintext.decode("utf-8")
            except UnicodeDecodeError:
                unfixable_links.append(f"{name} (token is not UTF-8)")
                continue
            if hash_share_token(token) != row.token_hash:
                unfixable_links.append(f"{name} (token does not match its hash)")
                continue
            rotated_links.append({"id": row.id, "ct": keyring.share_links.rotate(ciphertext)})
        if failed or unfixable_links:
            # Raising inside `begin()` rolls back the transaction. Each group
            # gets its own output: restoring a key only fixes a ciphertext that
            # no key in the file opens.
            problems: list[str] = []
            if failed:
                problems.append(
                    "no key in TASK_SECRETS_KEYS_PATH decrypts: "
                    + ", ".join(failed)
                    + (
                        " (restore the old key, or revoke the share links listed)"
                        if any("/share-link:" in f for f in failed)
                        else ""
                    )
                )
            if unfixable_links:
                problems.append(
                    "share link URLs that no key can fix: "
                    + ", ".join(unfixable_links)
                    + " (revoke these share links)"
                )
            raise RotateError("; ".join(problems) + "; nothing was rotated")
        if rotated:
            await conn.execute(
                text("UPDATE task_secrets SET ciphertext = :ct, fingerprint = :fp WHERE id = :id"),
                rotated,
            )
        if rotated_links:
            await conn.execute(
                text(
                    "UPDATE workspace_share_token SET token_ciphertext = :ct "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                rotated_links,
            )
    return RotateResult(task_secrets=len(rotated), share_links=len(rotated_links))


_STARTUP_LOG_HINT = (
    'the server\'s startup log line "task secrets keys loaded ... key ids=..." '
    '(e.g. journalctl -u <service> | grep "task secrets keys loaded")'
)


def _check_server_has_primary(keyring: TaskSecretsKeyring, confirmed_key_id: str | None) -> None:
    """`--keys-file` guard: the server must have the primary of the file.

    Rotating with a primary that the server did not load leaves all the rows
    unreadable for it. If the server's file can be read from this shell, it is
    verified against its keys; if not, the id confirmed by the operator against
    the startup log is required. Raises `SystemExit` with the reason; never
    prints keys.
    """
    primary = keyring.primary_key_id
    server_path = settings.task_secrets_keys_path
    if not server_path:
        unverifiable = "TASK_SECRETS_KEYS_PATH is not set"
    else:
        try:
            server_ring = load_keyring(server_path)
        except KeyringError as exc:
            unverifiable = f"TASK_SECRETS_KEYS_PATH cannot be read from this shell ({exc})"
        else:
            if primary in server_ring.key_ids:
                return
            raise SystemExit(
                f"--keys-file primary key id {primary} is not among the key ids of the "
                f"server's keys file TASK_SECRETS_KEYS_PATH ({','.join(server_ring.key_ids)}): "
                "the server could not decrypt the rotated secrets; nothing was rotated. "
                "Add the new key as the first line of the server's keys file and restart "
                "the server before rotating."
            )
    if confirmed_key_id is None:
        raise SystemExit(
            "cannot verify --keys-file against the keys the server loads: "
            f"{unverifiable}; nothing was rotated. The primary key id of --keys-file is "
            f"{primary}. Compare it with {_STARTUP_LOG_HINT} and re-run with "
            f"--confirm-server-key-id {primary} only if that id is listed there."
        )
    if confirmed_key_id != primary:
        raise SystemExit(
            f"--confirm-server-key-id {confirmed_key_id} does not match the primary key id "
            f"of --keys-file ({primary}); nothing was rotated. Pass {primary} only if it is "
            f"listed in {_STARTUP_LOG_HINT}."
        )


async def _rotate(keys_file: str | None, confirm_server_key_id: str | None = None) -> int:
    if not settings.database_url:
        raise SystemExit(
            "DATABASE_URL is not set (environment variable or .env in the working directory)"
        )
    keys_path = keys_file or settings.task_secrets_keys_path
    if not keys_path:
        raise SystemExit(
            "TASK_SECRETS_KEYS_PATH is not set (environment variable or .env in the "
            "working directory) and no --keys-file was given"
        )
    try:
        keyring = load_keyring(keys_path)
    except KeyringError as exc:
        raise SystemExit(str(exc)) from None
    print(
        f"keys file primary key id: {keyring.primary_key_id} "
        f"({len(keyring.key_ids)} key(s): {','.join(keyring.key_ids)})"
    )
    if keys_file:
        # Before creating the engine: a rejection opens no connection.
        _check_server_has_primary(keyring, confirm_server_key_id)
    engine = create_async_engine(settings.database_url)
    try:
        result = await rotate_all(engine, keyring)
    except RotateError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        await engine.dispose()
    print(
        f"rotated {result.task_secrets} task secret(s) and "
        f"{result.share_links} share link URL(s) to the primary key"
    )
    return 0


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task secrets encryption keys (host only)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate", help="print a new Fernet key")
    rotate = sub.add_parser(
        "rotate",
        help="re-encrypt every task secret and active share link URL with the primary key",
    )
    rotate.add_argument(
        "--keys-file",
        metavar="PATH",
        help="keys file to use instead of TASK_SECRETS_KEYS_PATH (e.g. a decrypted "
        "copy of a systemd credential)",
    )
    rotate.add_argument(
        "--confirm-server-key-id",
        metavar="ID",
        help="with --keys-file, when this shell cannot read the server's keys file: the "
        "primary key id of --keys-file, confirmed against the key ids in the server's "
        'startup log line ("task secrets keys loaded")',
    )
    args = parser.parse_args(argv)
    if args.command == "generate":
        print(Fernet.generate_key().decode("ascii"))
        return 0
    return asyncio.run(_rotate(args.keys_file, args.confirm_server_key_id))


def main() -> int:
    """Console entry point."""
    return run()


if __name__ == "__main__":
    sys.exit(main())
