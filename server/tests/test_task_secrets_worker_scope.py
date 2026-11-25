"""Per-worker scope of task secrets: service layer.

Covers setting, listing and deleting by scope, worker validation (nonexistent,
from another workspace, deleted), resolution by name or UUID, and resolution for
the poll: the worker row wins over the workspace row of the same name, a worker
never receives or fails because of another worker's row, and only the winning row
is decrypted.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orcaster_server.task_secrets.keyring import TaskSecretsKeyring
from orcaster_server.workspaces.constants import DEFAULT_WORKSPACE_ID


async def _user(session: AsyncSession) -> str:
    row = await session.execute(
        text(
            "INSERT INTO users (display_name, email) VALUES ('Owner', 'o@example.com') RETURNING id"
        )
    )
    return str(row.scalar_one())


async def _worker(
    session: AsyncSession,
    hostname: str,
    *,
    name: str | None = None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    deleted: bool = False,
) -> str:
    row = await session.execute(
        text(
            "INSERT INTO workers "
            "(workspace_id, api_key_hash, hostname, name, os, arch, deleted_at) "
            "VALUES (:ws, :h, :host, :name, 'linux', 'x86_64', "
            "CASE WHEN :deleted THEN now() ELSE NULL END) RETURNING id"
        ),
        {
            "ws": workspace_id,
            "h": uuid.uuid4().hex,
            "host": hostname,
            "name": name,
            "deleted": deleted,
        },
    )
    return str(row.scalar_one())


async def _other_workspace(session: AsyncSession) -> str:
    row = await session.execute(
        text("INSERT INTO workspaces (name, slug) VALUES ('Other', 'other') RETURNING id")
    )
    return str(row.scalar_one())


async def _set(session, ring, name, value, uid, worker_id=None):
    from orcaster_server.task_secrets import service

    return await service.set_secret(
        session,
        ring,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name=name,
        value=value,
        actor_user_id=uid,
        worker_id=worker_id,
    )


@pytest.mark.asyncio
async def test_set_list_delete_per_scope_and_audit(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    w1 = await _worker(db_session, "host-1", name="worker-a")
    w2 = await _worker(db_session, "worker-b")  # no `name`: the hostname is shown

    wide = await _set(db_session, ring, "GH", "wide-value", uid)
    m1 = await _set(db_session, ring, "GH", "w1-value", uid, worker_id=w1)
    m2 = await _set(db_session, ring, "GH", "w2-value", uid, worker_id=w2)
    await _set(db_session, ring, "AAA", "a", uid, worker_id=w1)
    await db_session.commit()
    assert wide.worker_id is None and wide.worker_name is None
    assert (m1.worker_id, m1.worker_name) == (w1, "worker-a")
    assert (m2.worker_id, m2.worker_name) == (w2, "worker-b")

    # Replacing within one scope does not touch the others.
    m1b = await _set(db_session, ring, "GH", "w1-value-2", uid, worker_id=w1)
    await db_session.commit()
    assert m1b.created_at == m1.created_at
    assert m1b.fingerprint == ring.fingerprint("w1-value-2")
    rows = (
        await db_session.execute(text("SELECT count(*) FROM task_secrets WHERE name = 'GH'"))
    ).scalar_one()
    assert rows == 3

    listed = await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID)
    # By name and, within a name, the workspace row first.
    assert [(m.name, m.worker_name) for m in listed] == [
        ("AAA", "worker-a"),
        ("GH", None),
        ("GH", "worker-a"),
        ("GH", "worker-b"),
    ]
    assert listed[1].fingerprint == ring.fingerprint("wide-value")

    # Deleting without a worker deletes only the workspace row; with a worker, only its own.
    assert await service.delete_secret(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, name="GH", actor_user_id=uid
    )
    assert not await service.delete_secret(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, name="GH", actor_user_id=uid
    )
    assert await service.delete_secret(
        db_session,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="GH",
        actor_user_id=uid,
        worker_id=w2,
    )
    await db_session.commit()
    listed = await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID)
    assert [(m.name, m.worker_id) for m in listed] == [("AAA", w1), ("GH", w1)]

    audits = (
        (
            await db_session.execute(
                text(
                    "SELECT action, payload FROM admin_actions "
                    "WHERE target_kind = 'task_secret' ORDER BY id"
                )
            )
        )
        .mappings()
        .all()
    )
    assert [(a["action"], a["payload"]) for a in audits] == [
        ("set_task_secret", {"name": "GH", "worker_id": None, "scope": "workspace"}),
        ("set_task_secret", {"name": "GH", "worker_id": w1, "scope": "worker"}),
        ("set_task_secret", {"name": "GH", "worker_id": w2, "scope": "worker"}),
        ("set_task_secret", {"name": "AAA", "worker_id": w1, "scope": "worker"}),
        ("set_task_secret", {"name": "GH", "worker_id": w1, "scope": "worker"}),
        ("delete_task_secret", {"name": "GH", "worker_id": None, "scope": "workspace"}),
        ("delete_task_secret", {"name": "GH", "worker_id": w2, "scope": "worker"}),
    ]


@pytest.mark.asyncio
async def test_set_rejects_unknown_foreign_and_deleted_worker(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    other_ws = await _other_workspace(db_session)
    foreign = await _worker(db_session, "foreign", workspace_id=other_ws)
    gone = await _worker(db_session, "gone", deleted=True)
    await db_session.commit()

    for wid in (str(uuid.uuid4()), foreign):
        with pytest.raises(service.WorkerNotFound) as exc:
            await _set(db_session, ring, "GH", "v", uid, worker_id=wid)
        # Same message for nonexistent and other-workspace: it does not leak existence.
        assert str(exc.value) == f"worker {wid} not found in this workspace"
        await db_session.rollback()
    with pytest.raises(service.WorkerDeleted, match="deleted"):
        await _set(db_session, ring, "GH", "v", uid, worker_id=gone)
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT count(*) FROM task_secrets"))).scalar_one() == 0


@pytest.mark.asyncio
async def test_delete_works_for_deleted_worker(db_session: AsyncSession) -> None:
    """Soft-delete: the row of the deleted worker can be cleaned up."""
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    wid = await _worker(db_session, "soon-gone")
    await _set(db_session, ring, "GH", "v", uid, worker_id=wid)
    await db_session.execute(
        text("UPDATE workers SET deleted_at = now() WHERE id = :id"), {"id": wid}
    )
    await db_session.commit()
    assert await service.delete_secret(
        db_session,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="GH",
        actor_user_id=uid,
        worker_id=wid,
    )
    await db_session.commit()
    assert await service.list_secrets(db_session, DEFAULT_WORKSPACE_ID) == []


@pytest.mark.asyncio
async def test_resolve_worker_by_uuid_or_name(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    w1 = await _worker(db_session, "host-1", name="worker-a")
    w2 = await _worker(db_session, "worker-b")
    await _worker(db_session, "twin-a", name="twin")
    await _worker(db_session, "twin-b", name="twin")
    # A deleted worker with the same name as a live one does not make it ambiguous.
    await _worker(db_session, "old", name="worker-a", deleted=True)
    gone = await _worker(db_session, "gone-host", name="gone", deleted=True)
    other_ws = await _other_workspace(db_session)
    foreign = await _worker(db_session, "foreign", workspace_id=other_ws)
    await db_session.commit()

    async def resolve(ref: str, *, allow_deleted: bool = False):
        return await service.resolve_worker(
            db_session, workspace_id=DEFAULT_WORKSPACE_ID, ref=ref, allow_deleted=allow_deleted
        )

    assert (await resolve(w1)).id == w1
    assert (await resolve(w1.upper())).id == w1
    r = await resolve("worker-a")
    assert (r.id, r.name) == (w1, "worker-a")
    # Without `name`, the hostname is the visible name.
    assert (await resolve("worker-b")).id == w2
    with pytest.raises(service.WorkerAmbiguous, match="2 workers"):
        await resolve("twin")
    for ref in ("nope", foreign, str(uuid.uuid4())):
        with pytest.raises(service.WorkerNotFound):
            await resolve(ref)
    with pytest.raises(service.WorkerDeleted):
        await resolve(gone)
    with pytest.raises(service.WorkerNotFound):
        await resolve("gone")
    # For deletion, a deleted worker resolves by UUID or by name.
    assert (await resolve(gone, allow_deleted=True)).id == gone
    assert (await resolve("gone", allow_deleted=True)).id == gone
    assert (await resolve("worker-a", allow_deleted=True)).id == w1


@pytest.mark.asyncio
async def test_resolve_worker_matches_names_exactly(db_session: AsyncSession) -> None:
    """The service does not trim the reference; "worker-a " and "worker-a" are
    different workers and a UUID with spaces is not a UUID."""
    from orcaster_server.task_secrets import service

    padded = await _worker(db_session, "host-a", name="worker-a ")
    exact = await _worker(db_session, "host-b", name="worker-a")
    await db_session.commit()

    async def resolve(ref: str):
        return await service.resolve_worker(db_session, workspace_id=DEFAULT_WORKSPACE_ID, ref=ref)

    assert (await resolve("worker-a")).id == exact
    assert (await resolve("worker-a ")).id == padded
    assert (await resolve(padded)).name == "worker-a "
    for ref in (" worker-a", f" {exact}", f"{exact} "):
        with pytest.raises(service.WorkerNotFound):
            await resolve(ref)


@pytest.mark.asyncio
async def test_resolve_for_task_worker_row_wins(db_session: AsyncSession) -> None:
    from orcaster_server.task_secrets import service

    ring = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    w1 = await _worker(db_session, "worker-a")
    w2 = await _worker(db_session, "worker-b")
    w3 = await _worker(db_session, "worker-c")
    await _set(db_session, ring, "GH", "wide", uid)
    await _set(db_session, ring, "GH", "w1", uid, worker_id=w1)
    await _set(db_session, ring, "HF", "hf-wide", uid)
    await _set(db_session, ring, "ONLY_W2", "w2-only", uid, worker_id=w2)
    await db_session.commit()

    async def resolve(wid: str | None):
        return await service.resolve_for_task(
            db_session,
            ring,
            workspace_id=DEFAULT_WORKSPACE_ID,
            worker_id=wid,
            names=["GH", "HF", "ONLY_W2"],
        )

    r1 = await resolve(w1)
    assert r1.values == {"GH": "w1", "HF": "hf-wide"}
    assert r1.worker_scoped == ["GH"]
    r2 = await resolve(w2)
    assert r2.values == {"GH": "wide", "HF": "hf-wide", "ONLY_W2": "w2-only"}
    assert r2.worker_scoped == ["ONLY_W2"]
    r3 = await resolve(w3)
    assert r3.values == {"GH": "wide", "HF": "hf-wide"}
    assert r3.worker_scoped == []
    # Without a worker, workspace rows only.
    assert (await resolve(None)).values == {"GH": "wide", "HF": "hf-wide"}

    assert await service.stored_names(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, worker_id=w3, names=["GH", "ONLY_W2"]
    ) == ["GH"]
    assert await service.stored_names(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, worker_id=w2, names=["GH", "ONLY_W2"]
    ) == ["GH", "ONLY_W2"]


@pytest.mark.asyncio
async def test_resolve_for_task_only_decrypts_winning_row(db_session: AsyncSession) -> None:
    """An undecryptable row shadowed by the worker's row is neither used nor fails;
    the same goes for another worker's undecryptable row. The worker's own row does
    fail."""
    from orcaster_server.task_secrets import service

    old = TaskSecretsKeyring([Fernet.generate_key()])
    new = TaskSecretsKeyring([Fernet.generate_key()])
    uid = await _user(db_session)
    w1 = await _worker(db_session, "worker-a")
    w2 = await _worker(db_session, "worker-b")
    await _set(db_session, old, "GH", "wide-old", uid)  # undecryptable with `new`
    await _set(db_session, new, "GH", "w1-new", uid, worker_id=w1)
    await _set(db_session, old, "HF", "w2-old", uid, worker_id=w2)  # undecryptable
    await db_session.commit()

    r1 = await service.resolve_for_task(
        db_session, new, workspace_id=DEFAULT_WORKSPACE_ID, worker_id=w1, names=["GH", "HF"]
    )
    assert r1.values == {"GH": "w1-new"}
    assert r1.worker_scoped == ["GH"]

    with pytest.raises(service.UndecryptableTaskSecrets) as exc:
        await service.resolve_for_task(
            db_session, new, workspace_id=DEFAULT_WORKSPACE_ID, worker_id=w2, names=["GH", "HF"]
        )
    assert exc.value.names == ["GH", "HF"]
