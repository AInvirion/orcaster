"""Task secrets service, per workspace and per worker.

Functions reusable by the `/api/v1/secrets` API, the dashboard and the `/poll`.
None returns the value or the ciphertext, except `resolve_for_task`, which
decrypts in memory for the poll response. None commits: the caller does, so the
change and its audit row persist together.

Scope: a row with `worker_id NULL` is workspace-wide; one with `worker_id`
applies only to that worker and, for it, wins over the workspace row of the same
name. A worker never receives another worker's row nor fails because of it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import InvalidToken, MultiFernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.audit import record_admin_action
from orcaster_server.task_secrets.keyring import TaskSecretsKeyring, get_keyring_fresh

SECRET_VALUE_MAX_BYTES = 64 * 1024


class TaskSecretsUnconfigured(RuntimeError):
    """`TASK_SECRETS_KEYS_PATH` unset: management answers 503."""


class UndecryptableTaskSecrets(RuntimeError):
    """Declared secrets that exist but that no key decrypts.

    `names` is sorted. The message names the secrets, never the values.
    """

    def __init__(self, names: Iterable[str]) -> None:
        self.names = sorted(names)
        super().__init__(
            "task secret(s) not decryptable with any configured key: " + ", ".join(self.names)
        )


class TaskSecretsKeysMissing(UndecryptableTaskSecrets):
    """Declared secrets that exist, with the store having no keys configured.

    Without `TASK_SECRETS_KEYS_PATH` the server does not silently deliver a task
    without the secrets it has stored; it fails it just like a secret that does
    not decrypt. The message names the secrets, never the values.
    """

    def __init__(self, names: Iterable[str]) -> None:
        super().__init__(names)
        self.args = (
            "task secret(s) stored but TASK_SECRETS_KEYS_PATH is not set: " + ", ".join(self.names),
        )


class WorkerNotFound(LookupError):
    """The worker does not exist in this workspace.

    Same message for a nonexistent worker and for one from another workspace:
    the existence of workers is not leaked across workspaces.
    """

    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(f"worker {ref} not found in this workspace")


class WorkerDeleted(ValueError):
    """The worker is deleted (soft-delete): it accepts no new secrets.

    Deleting its own is allowed, to clean up.
    """

    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(f"worker {ref} is deleted; it cannot receive new secrets")


class WorkerAmbiguous(ValueError):
    """The display name matches more than one worker in the workspace."""

    def __init__(self, ref: str, count: int) -> None:
        self.ref = ref
        self.count = count
        super().__init__(
            f"worker name {ref!r} matches {count} workers in this workspace; use the worker id"
        )


@dataclass(frozen=True)
class WorkerRef:
    """Worker resolved for a secret scope. `name` is the display name
    (`name`, or the `hostname` if it has none), as in the dashboard."""

    id: str
    name: str
    deleted: bool


@dataclass(frozen=True)
class SecretMeta:
    """Visible metadata of a secret. No value or ciphertext."""

    name: str
    fingerprint: str
    created_at: datetime
    updated_at: datetime
    # Author's email, or their display_name if they have no email; None if the
    # user was deleted (FK ON DELETE SET NULL).
    updated_by: str | None
    # None = workspace-wide secret. With a value, only that worker receives it;
    # `worker_name` is its display name.
    worker_id: str | None = None
    worker_name: str | None = None

    @property
    def scope(self) -> str:
        """`workspace` or `worker`, the same value that `set_task_secret` audits."""
        return "workspace" if self.worker_id is None else "worker"


@dataclass(frozen=True)
class ResolvedSecrets:
    """Secrets to deliver in a poll. `values` is never logged.

    `worker_scoped`: names (sorted) whose value came from a row of the worker
    itself and not from the workspace one.
    """

    values: dict[str, str]
    worker_scoped: list[str]


def require_keyring() -> TaskSecretsKeyring:
    """The configured keyring, or `TaskSecretsUnconfigured`.

    Rereads the keys file if it changed on disk: setting a secret is rare and
    must encrypt and fingerprint with the current primary, not with the one this
    process loaded at startup.
    """
    ring = get_keyring_fresh()
    if ring is None:
        raise TaskSecretsUnconfigured("task secrets are not configured on this server")
    return ring


def validate_secret_value(value: str) -> str:
    """Non-empty, without NUL, at most 64 KiB in UTF-8 and without losing its line breaks.

    The messages do not quote the value.
    """
    if not isinstance(value, str) or value == "":
        raise ValueError("secret value must be a non-empty string")
    if "\x00" in value:
        raise ValueError("secret value must not contain NUL characters")
    if len(value.encode("utf-8")) > SECRET_VALUE_MAX_BYTES:
        raise ValueError(f"secret value must be at most {SECRET_VALUE_MAX_BYTES} bytes (UTF-8)")
    if _looks_like_multiline_cut_to_one_line(value):
        raise ValueError(
            "secret value looks like a multi-line value (PEM key or JSON) that lost its "
            "line breaks; enter it with its line breaks, or pipe the file to "
            "`orcaster secret set NAME < file`"
        )
    return value


def _looks_like_multiline_cut_to_one_line(value: str) -> bool:
    """A PEM without line breaks or the first line alone of a JSON.

    An `<input type=password>` strips line breaks and a one-line prompt keeps
    only the first one; in both cases the server saw a valid value and the task
    later failed with a parse error giving no hint of the cause.
    """
    stripped = value.strip()
    if stripped.startswith("-----BEGIN") and "\n" not in value and "\r" not in value:
        return True
    return stripped in ("{", "[")


def declared_task_env(payload: object) -> list[str]:
    """Names in `payload.task_env`, deduplicated and sorted.

    Without `task_env` (legacy mode) or with an unexpected shape in an old row
    it returns `[]`: that task receives no server secrets.
    """
    if not isinstance(payload, dict):
        return []
    env = payload.get("task_env")
    if not isinstance(env, list):
        return []
    return sorted({n for n in env if isinstance(n, str)})


# Display name of the worker: the same as the dashboard (`w.name or w.hostname`).
_WORKER_DISPLAY_NAME = "COALESCE(w.name, w.hostname)"

# By name and, within a name, the workspace row first and then the worker rows
# by display name (the id breaks ties between repeated names).
_SQL_LIST = text(
    f"""
    SELECT s.name, s.fingerprint, s.created_at, s.updated_at,
           COALESCE(u.email, u.display_name) AS updated_by,
           CAST(s.worker_id AS text) AS worker_id,
           {_WORKER_DISPLAY_NAME} AS worker_name
      FROM task_secrets s
      LEFT JOIN users u ON u.id = s.updated_by_user_id
      LEFT JOIN workers w ON w.id = s.worker_id
     WHERE s.workspace_id = :workspace_id
     ORDER BY s.name, (s.worker_id IS NOT NULL), {_WORKER_DISPLAY_NAME}, s.worker_id
    """
)


def _upsert_sql(conflict_target: str) -> str:
    """Upsert with inference of the scope's partial unique index (0059).

    `ON CONFLICT (cols) WHERE pred` infers the partial index whose predicate
    implies `pred`:
    https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT
    """
    return f"""
    WITH up AS (
        INSERT INTO task_secrets
            (workspace_id, name, ciphertext, fingerprint, updated_by_user_id, worker_id)
        VALUES (:workspace_id, :name, :ciphertext, :fingerprint, :user_id,
                CAST(:worker_id AS uuid))
        ON CONFLICT {conflict_target} DO UPDATE
           SET ciphertext = EXCLUDED.ciphertext,
               fingerprint = EXCLUDED.fingerprint,
               updated_at = now(),
               updated_by_user_id = EXCLUDED.updated_by_user_id
        RETURNING id, name, fingerprint, created_at, updated_at, updated_by_user_id, worker_id
    )
    SELECT CAST(up.id AS text) AS id, up.name, up.fingerprint, up.created_at, up.updated_at,
           COALESCE(u.email, u.display_name) AS updated_by,
           CAST(up.worker_id AS text) AS worker_id,
           {_WORKER_DISPLAY_NAME} AS worker_name
      FROM up
      LEFT JOIN users u ON u.id = up.updated_by_user_id
      LEFT JOIN workers w ON w.id = up.worker_id
    """


_SQL_UPSERT_WORKSPACE = text(_upsert_sql("(workspace_id, name) WHERE worker_id IS NULL"))
_SQL_UPSERT_WORKER = text(
    _upsert_sql("(workspace_id, name, worker_id) WHERE worker_id IS NOT NULL")
)

_SQL_DELETE_WORKSPACE = text(
    """
    DELETE FROM task_secrets
     WHERE workspace_id = :workspace_id AND name = :name AND worker_id IS NULL
    RETURNING CAST(id AS text) AS id
    """
)

_SQL_DELETE_WORKER = text(
    """
    DELETE FROM task_secrets
     WHERE workspace_id = :workspace_id AND name = :name
       AND worker_id = CAST(:worker_id AS uuid)
    RETURNING CAST(id AS text) AS id
    """
)

# Rows applicable to a worker: the workspace ones and its own. With `:worker_id`
# NULL, `worker_id = NULL` matches nothing and only the workspace rows remain.
_APPLICABLE = "(worker_id IS NULL OR worker_id = CAST(:worker_id AS uuid))"

_SQL_STORED_NAMES = text(
    f"""
    SELECT DISTINCT name
      FROM task_secrets
     WHERE workspace_id = :workspace_id
       AND name = ANY(:names)
       AND {_APPLICABLE}
     ORDER BY name
    """
)

# One row per name: the worker's if it exists (`worker_id IS NULL` is false for
# it and sorts first), otherwise the workspace's. Only the winner is decrypted,
# so an undecryptable workspace row shadowed by the worker's does not make the
# delivery fail.
_SQL_RESOLVE = text(
    f"""
    SELECT DISTINCT ON (name) name, ciphertext, (worker_id IS NOT NULL) AS worker_scoped
      FROM task_secrets
     WHERE workspace_id = :workspace_id
       AND name = ANY(:names)
       AND {_APPLICABLE}
     ORDER BY name, (worker_id IS NULL)
    """
)

_SQL_WORKER_BY_ID = text(
    f"""
    SELECT CAST(w.id AS text) AS id, {_WORKER_DISPLAY_NAME} AS name,
           (w.deleted_at IS NOT NULL) AS deleted
      FROM workers w
     WHERE w.workspace_id = :workspace_id AND w.id = CAST(:worker_id AS uuid)
    """
)

_SQL_WORKERS_BY_NAME = text(
    f"""
    SELECT CAST(w.id AS text) AS id, {_WORKER_DISPLAY_NAME} AS name,
           (w.deleted_at IS NOT NULL) AS deleted
      FROM workers w
     WHERE w.workspace_id = :workspace_id AND {_WORKER_DISPLAY_NAME} = :name
     ORDER BY w.id
    """
)


def _meta(row) -> SecretMeta:
    return SecretMeta(
        name=row.name,
        fingerprint=row.fingerprint,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        worker_id=row.worker_id,
        worker_name=row.worker_name,
    )


def _scope_payload(name: str, worker_id: str | None) -> dict:
    """Audit payload of `set_task_secret` / `delete_task_secret`. No value."""
    return {
        "name": name,
        "worker_id": worker_id,
        "scope": "workspace" if worker_id is None else "worker",
    }


def _as_uuid(ref: str) -> str | None:
    """`ref` normalized (lowercase with hyphens) if it is a UUID; otherwise None."""
    try:
        return str(uuid.UUID(ref))
    except (ValueError, AttributeError, TypeError):
        return None


async def resolve_worker(
    session: AsyncSession,
    *,
    workspace_id: str,
    ref: str,
    allow_deleted: bool = False,
) -> WorkerRef:
    """Resolve the worker of a scope by UUID or by display name.

    - UUID: the worker with that id in this workspace. One from another
      workspace gives `WorkerNotFound`, like a nonexistent one.
    - Name: `name`, or `hostname` if it has none (the name the dashboard shows),
      among the non-deleted workers. More than one gives `WorkerAmbiguous`. With
      `allow_deleted` and none alive, the deleted ones are searched, so that
      their secrets can be cleaned up.

    A deleted worker without `allow_deleted` gives `WorkerDeleted`.
    """
    as_id = _as_uuid(ref)
    if as_id is not None:
        row = (
            await session.execute(
                _SQL_WORKER_BY_ID, {"workspace_id": workspace_id, "worker_id": as_id}
            )
        ).first()
        if row is None:
            raise WorkerNotFound(ref)
        if row.deleted and not allow_deleted:
            raise WorkerDeleted(ref)
        return WorkerRef(id=row.id, name=row.name, deleted=row.deleted)
    rows = (
        await session.execute(_SQL_WORKERS_BY_NAME, {"workspace_id": workspace_id, "name": ref})
    ).all()
    candidates = [r for r in rows if not r.deleted]
    if not candidates and allow_deleted:
        candidates = list(rows)
    if not candidates:
        raise WorkerNotFound(ref)
    if len(candidates) > 1:
        raise WorkerAmbiguous(ref, len(candidates))
    [row] = candidates
    return WorkerRef(id=row.id, name=row.name, deleted=row.deleted)


_SQL_SCOPE_WORKERS = text(
    f"""
    SELECT CAST(w.id AS text) AS id, {_WORKER_DISPLAY_NAME} AS name, false AS deleted
      FROM workers w
     WHERE w.workspace_id = :workspace_id AND w.deleted_at IS NULL
     ORDER BY {_WORKER_DISPLAY_NAME}, w.id
    """
)


async def list_scope_workers(session: AsyncSession, workspace_id: str) -> list[WorkerRef]:
    """Non-deleted workers of the workspace that can receive a secret of their own,
    by display name. For the dashboard selector."""
    rows = (await session.execute(_SQL_SCOPE_WORKERS, {"workspace_id": workspace_id})).all()
    return [WorkerRef(id=r.id, name=r.name, deleted=r.deleted) for r in rows]


async def list_secrets(session: AsyncSession, workspace_id: str) -> list[SecretMeta]:
    """Metadata of the workspace's secrets, of all scopes.

    By name and, within a name, the workspace one first. Decrypts nothing.
    """
    rows = (await session.execute(_SQL_LIST, {"workspace_id": workspace_id})).all()
    return [_meta(r) for r in rows]


async def set_secret(
    session: AsyncSession,
    keyring: TaskSecretsKeyring,
    *,
    workspace_id: str,
    name: str,
    value: str,
    actor_user_id: str,
    worker_id: str | None = None,
) -> SecretMeta:
    """Create or replace a secret in its scope and audit `set_task_secret` (no value).

    `worker_id` None is the workspace-wide secret; with a value, that worker's,
    which must belong to the workspace and not be deleted (`WorkerNotFound`,
    `WorkerDeleted`). The caller validates name and value (`validate_secret_name`,
    `validate_secret_value`) and the `owner` role.
    """
    if worker_id is not None:
        worker_id = (
            await resolve_worker(session, workspace_id=workspace_id, ref=_require_uuid(worker_id))
        ).id
    row = (
        await session.execute(
            _SQL_UPSERT_WORKSPACE if worker_id is None else _SQL_UPSERT_WORKER,
            {
                "workspace_id": workspace_id,
                "name": name,
                "ciphertext": keyring.encrypt(value.encode("utf-8")),
                "fingerprint": keyring.fingerprint(value),
                "user_id": actor_user_id,
                "worker_id": worker_id,
            },
        )
    ).one()
    await record_admin_action(
        session,
        workspace_id=workspace_id,
        action="set_task_secret",
        target_kind="task_secret",
        target_id=row.id,
        actor=actor_user_id,
        payload=_scope_payload(name, worker_id),
    )
    return _meta(row)


def _require_uuid(worker_id: str) -> str:
    """`worker_id` of the service layer: UUID only (names are resolved by the
    caller with `resolve_worker`). Any other value is `WorkerNotFound`."""
    as_id = _as_uuid(worker_id)
    if as_id is None:
        raise WorkerNotFound(worker_id)
    return as_id


async def delete_secret(
    session: AsyncSession,
    *,
    workspace_id: str,
    name: str,
    actor_user_id: str,
    worker_id: str | None = None,
) -> bool:
    """Delete the secret of exactly that scope; `False` if it did not exist.

    Without `worker_id`, only the workspace one; with it, only that worker's
    (even if deleted: that is how they are cleaned up). Audits
    `delete_task_secret`.
    """
    if worker_id is None:
        row = (
            await session.execute(
                _SQL_DELETE_WORKSPACE, {"workspace_id": workspace_id, "name": name}
            )
        ).first()
    else:
        as_id = _as_uuid(worker_id)
        if as_id is None:
            return False
        worker_id = as_id
        row = (
            await session.execute(
                _SQL_DELETE_WORKER,
                {"workspace_id": workspace_id, "name": name, "worker_id": worker_id},
            )
        ).first()
    if row is None:
        return False
    await record_admin_action(
        session,
        workspace_id=workspace_id,
        action="delete_task_secret",
        target_kind="task_secret",
        target_id=row.id,
        actor=actor_user_id,
        payload=_scope_payload(name, worker_id),
    )
    return True


async def resolve_for_task(
    session: AsyncSession,
    keyring: MultiFernet,
    *,
    workspace_id: str,
    names: Iterable[str],
    worker_id: str | None = None,
) -> ResolvedSecrets:
    """Decrypt in memory the secrets applicable to `worker_id` whose name is in `names`.

    Applicable: the workspace ones and the worker's own; by name the worker's
    wins. Only the winning row is decrypted: a shadowed workspace row is not
    read, and other workers' rows are not even queried. Without `worker_id`,
    only the workspace ones.

    Names without a stored secret are omitted (the worker looks them up in its
    own environment). If any winning row is not decrypted by any key, it raises
    `UndecryptableTaskSecrets` with all the affected names.
    """
    wanted = sorted(set(names))
    if not wanted:
        return ResolvedSecrets(values={}, worker_scoped=[])
    rows = (
        await session.execute(
            _SQL_RESOLVE,
            {"workspace_id": workspace_id, "names": wanted, "worker_id": worker_id},
        )
    ).all()
    resolved: dict[str, str] = {}
    scoped: list[str] = []
    failed: list[str] = []
    for row in rows:
        try:
            resolved[row.name] = keyring.decrypt(bytes(row.ciphertext)).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            failed.append(row.name)
            continue
        if row.worker_scoped:
            scoped.append(row.name)
    if failed:
        raise UndecryptableTaskSecrets(failed)
    return ResolvedSecrets(values=resolved, worker_scoped=sorted(scoped))


async def stored_names(
    session: AsyncSession,
    *,
    workspace_id: str,
    names: Iterable[str],
    worker_id: str | None = None,
) -> list[str]:
    """Names in `names` with a secret applicable to `worker_id` (workspace-wide or
    its own; without `worker_id`, workspace-wide only). Does not read the ciphertext."""
    wanted = sorted(set(names))
    if not wanted:
        return []
    rows = (
        await session.execute(
            _SQL_STORED_NAMES,
            {"workspace_id": workspace_id, "names": wanted, "worker_id": worker_id},
        )
    ).all()
    return [r.name for r in rows]


async def record_delivery(
    session: AsyncSession,
    *,
    workspace_id: str,
    task_id: int,
    worker_id: str,
    names: Iterable[str],
    worker_scoped: Iterable[str] = (),
) -> None:
    """Audit the delivery of secrets in a poll (`deliver_task_secrets`, no actor).

    `worker_scoped`: the names delivered from a row of the worker.
    """
    await record_admin_action(
        session,
        workspace_id=workspace_id,
        action="deliver_task_secrets",
        target_kind="task",
        target_id=str(task_id),
        actor=None,
        payload={
            "worker_id": worker_id,
            "names": sorted(names),
            "worker_scoped": sorted(worker_scoped),
        },
    )
