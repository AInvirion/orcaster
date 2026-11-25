"""Tests of the workspaces service and the `require_workspace` dependency.

Direct service tests against `db_session` (no HTTP, nothing is wired to a route
yet). `require_workspace` is tested with a trivial stub of `require_session`
that injects the test user.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from orcaster_server.workspaces.deps import WorkspaceContext, make_require_workspace
from orcaster_server.workspaces.service import (
    add_member,
    create_workspace,
    get_workspace_name,
    is_workspace_disabled,
    list_memberships,
    resolve_active_workspace,
)


async def _make_user(db_session, name: str = "Tester") -> str:
    row = await db_session.execute(
        text("INSERT INTO users (display_name) VALUES (:n) RETURNING id"),
        {"n": name},
    )
    uid = str(row.scalar_one())
    await db_session.commit()
    return uid


async def _set_active(db_session, *, user_id: str, workspace_id: str) -> None:
    await db_session.execute(
        text("INSERT INTO user_active_workspace (user_id, workspace_id) VALUES (:u, :w)"),
        {"u": user_id, "w": workspace_id},
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_create_add_list_roundtrip(db_session):
    user = await _make_user(db_session)
    ws = await create_workspace(db_session, name="Acme", slug="acme", created_by_user_id=user)
    await db_session.commit()
    assert ws

    await add_member(db_session, workspace_id=ws, user_id=user, role="owner")
    await db_session.commit()

    memberships = await list_memberships(db_session, user)
    assert len(memberships) == 1
    assert memberships[0].workspace_id == ws
    assert memberships[0].role == "owner"


@pytest.mark.asyncio
async def test_add_member_is_idempotent(db_session):
    user = await _make_user(db_session)
    ws = await create_workspace(db_session, name="Acme", slug="acme")
    await db_session.commit()

    await add_member(db_session, workspace_id=ws, user_id=user, role="owner")
    await add_member(db_session, workspace_id=ws, user_id=user, role="member")
    await db_session.commit()

    memberships = await list_memberships(db_session, user)
    assert len(memberships) == 1
    # El primer insert gana (ON CONFLICT DO NOTHING).
    assert memberships[0].role == "owner"


@pytest.mark.asyncio
async def test_add_member_role_conflict_warns_and_does_not_promote(db_session, caplog):
    """A 2nd invitation with a different role does NOT promote and emits a warning.

    `add_member` preserves the original role (`on_conflict_do_nothing`) and logs
    a warning when the requested role differs from the existing one — making the
    silent case visible without changing the invitation semantics.
    """
    import logging

    user = await _make_user(db_session)
    ws = await create_workspace(db_session, name="Acme", slug="acme")
    await db_session.commit()

    await add_member(db_session, workspace_id=ws, user_id=user, role="member")
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="orcaster_server.workspaces.service"):
        await add_member(db_session, workspace_id=ws, user_id=user, role="owner")
        await db_session.commit()

    # Role NOT promoted: still 'member'.
    memberships = await list_memberships(db_session, user)
    assert len(memberships) == 1
    assert memberships[0].role == "member"
    # The role conflict warning was emitted.
    assert any(
        rec.levelno == logging.WARNING
        and "promotion via re-invitation is not allowed" in rec.getMessage()
        for rec in caplog.records
    ), caplog.records


@pytest.mark.asyncio
async def test_add_member_same_role_does_not_warn(db_session, caplog):
    """Re-inviting with the SAME role emits no warning (no conflict)."""
    import logging

    user = await _make_user(db_session)
    ws = await create_workspace(db_session, name="Acme", slug="acme")
    await db_session.commit()

    await add_member(db_session, workspace_id=ws, user_id=user, role="owner")
    await db_session.commit()
    with caplog.at_level(logging.WARNING, logger="orcaster_server.workspaces.service"):
        await add_member(db_session, workspace_id=ws, user_id=user, role="owner")
        await db_session.commit()

    assert not [r for r in caplog.records if r.levelno == logging.WARNING], caplog.records


@pytest.mark.asyncio
async def test_list_memberships_ordered_by_created_at(db_session):
    user = await _make_user(db_session)
    ws1 = await create_workspace(db_session, name="First", slug="first")
    ws2 = await create_workspace(db_session, name="Second", slug="second")
    await db_session.commit()

    await add_member(db_session, workspace_id=ws1, user_id=user, role="owner")
    await db_session.commit()
    # Force a later created_at for the second one to guarantee the ordering.
    await db_session.execute(
        text(
            "INSERT INTO workspace_members (workspace_id, user_id, role, created_at) "
            "VALUES (:w, :u, 'member', now() + interval '1 second')"
        ),
        {"w": ws2, "u": user},
    )
    await db_session.commit()

    memberships = await list_memberships(db_session, user)
    assert [m.workspace_id for m in memberships] == [ws1, ws2]


@pytest.mark.asyncio
async def test_resolve_returns_active_when_member(db_session):
    user = await _make_user(db_session)
    ws1 = await create_workspace(db_session, name="First", slug="first")
    ws2 = await create_workspace(db_session, name="Second", slug="second")
    await db_session.commit()
    await add_member(db_session, workspace_id=ws1, user_id=user, role="owner")
    await add_member(db_session, workspace_id=ws2, user_id=user, role="member")
    await db_session.commit()
    await _set_active(db_session, user_id=user, workspace_id=ws2)

    resolved = await resolve_active_workspace(db_session, user)
    assert resolved == (ws2, "member")


@pytest.mark.asyncio
async def test_resolve_falls_back_when_active_not_member(db_session):
    user = await _make_user(db_session)
    ws_member = await create_workspace(db_session, name="Mine", slug="mine")
    ws_other = await create_workspace(db_session, name="Other", slug="other")
    await db_session.commit()
    # User is a member only of ws_member, but the active one points to ws_other.
    await add_member(db_session, workspace_id=ws_member, user_id=user, role="owner")
    await db_session.commit()
    await _set_active(db_session, user_id=user, workspace_id=ws_other)

    resolved = await resolve_active_workspace(db_session, user)
    assert resolved == (ws_member, "owner")


@pytest.mark.asyncio
async def test_resolve_falls_back_when_no_active_row(db_session):
    user = await _make_user(db_session)
    ws1 = await create_workspace(db_session, name="First", slug="first")
    await db_session.commit()
    await add_member(db_session, workspace_id=ws1, user_id=user, role="owner")
    await db_session.commit()

    resolved = await resolve_active_workspace(db_session, user)
    assert resolved == (ws1, "owner")


@pytest.mark.asyncio
async def test_resolve_returns_none_without_memberships(db_session):
    user = await _make_user(db_session)
    assert await resolve_active_workspace(db_session, user) is None


@pytest.mark.asyncio
async def test_require_workspace_raises_403_without_membership(db_session):
    user_id = await _make_user(db_session)

    class _StubUser:
        id = user_id

    async def fake_require_session():
        return _StubUser()

    require_workspace = make_require_workspace(fake_require_session)
    with pytest.raises(HTTPException) as exc:
        await require_workspace(user=_StubUser(), db=db_session)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_workspace_returns_context(db_session):
    user_id = await _make_user(db_session)
    ws = await create_workspace(db_session, name="Acme", slug="acme")
    await db_session.commit()
    await add_member(db_session, workspace_id=ws, user_id=user_id, role="owner")
    await db_session.commit()

    class _StubUser:
        id = user_id

    async def fake_require_session():
        return _StubUser()

    stub = _StubUser()
    require_workspace = make_require_workspace(fake_require_session)
    ctx = await require_workspace(user=stub, db=db_session)
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_id == ws
    assert ctx.role == "owner"
    assert ctx.user is stub


@pytest.mark.asyncio
async def test_add_member_returns_whether_a_row_was_inserted(db_session):
    """`add_member` returns True on insert and False if already a member."""
    user = await _make_user(db_session)
    ws = await create_workspace(db_session, name="Acme", slug="acme")
    await db_session.commit()

    assert await add_member(db_session, workspace_id=ws, user_id=user, role="member") is True
    assert await add_member(db_session, workspace_id=ws, user_id=user, role="member") is False
    assert await add_member(db_session, workspace_id=ws, user_id=user, role="owner") is False
    await db_session.commit()


@pytest.mark.asyncio
async def test_get_workspace_name_accepts_canonical_id_in_any_case(db_session):
    ws = await create_workspace(db_session, name="Acme Name", slug="acme-name")
    await db_session.commit()

    assert await get_workspace_name(db_session, ws) == "Acme Name"
    assert await get_workspace_name(db_session, ws.upper()) == "Acme Name"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_id",
    [
        pytest.param(lambda ws: f"urn:uuid:{ws}", id="urn"),
        pytest.param(lambda ws: "{" + ws + "}", id="braces"),
        pytest.param(lambda ws: ws.replace("-", ""), id="unhyphenated"),
        pytest.param(lambda ws: f" {ws}", id="padded"),
        pytest.param(lambda ws: "not-a-uuid", id="garbage"),
        pytest.param(lambda ws: "", id="empty"),
        pytest.param(lambda ws: "11111111-2222-3333-4444-555555555555", id="unknown"),
    ],
)
async def test_get_workspace_name_returns_none_instead_of_raising(db_session, make_id):
    """An id that `uuid.UUID` parses but asyncpg rejects (urn, braces) must not
    reach the query: the function returns None, never raises."""
    ws = await create_workspace(db_session, name="Acme Name", slug="acme-name")
    await db_session.commit()

    assert await get_workspace_name(db_session, make_id(ws)) is None


async def _disable(db_session, workspace_id: str) -> None:
    await db_session.execute(
        text("UPDATE workspaces SET disabled_at = now() WHERE id = :i"), {"i": workspace_id}
    )
    await db_session.commit()


# `uuid.UUID` parses all these forms to the same workspace. For a privacy check
# being permissive is the safe direction: an alias of a disabled workspace
# counts as disabled.
_ID_FORMS = [
    pytest.param(lambda ws: ws, id="canonical"),
    pytest.param(lambda ws: ws.upper(), id="uppercase"),
    pytest.param(lambda ws: f"urn:uuid:{ws}", id="urn"),
    pytest.param(lambda ws: "{" + ws + "}", id="braces"),
    pytest.param(lambda ws: ws.replace("-", ""), id="unhyphenated"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_id", _ID_FORMS)
async def test_is_workspace_disabled_true_for_disabled_workspace_in_any_form(db_session, make_id):
    ws = await create_workspace(db_session, name="Acme Name", slug="acme-name")
    await db_session.commit()
    await _disable(db_session, ws)

    assert await is_workspace_disabled(db_session, make_id(ws)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("make_id", _ID_FORMS)
async def test_is_workspace_disabled_false_for_enabled_workspace_in_any_form(db_session, make_id):
    ws = await create_workspace(db_session, name="Acme Name", slug="acme-name")
    await db_session.commit()

    assert await is_workspace_disabled(db_session, make_id(ws)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_id",
    [
        pytest.param("11111111-2222-3333-4444-555555555555", id="unknown"),
        pytest.param("{11111111-2222-3333-4444-555555555555}", id="unknown-braced"),
        pytest.param("not-a-uuid", id="garbage"),
        pytest.param("", id="empty"),
        pytest.param("0123456789abcdef", id="short-hex"),
    ],
)
async def test_is_workspace_disabled_false_instead_of_raising(db_session, workspace_id):
    """Without a workspace, or with an id that is not a UUID, returns False and never raises."""
    ws = await create_workspace(db_session, name="Acme Name", slug="acme-name")
    await db_session.commit()
    await _disable(db_session, ws)

    assert await is_workspace_disabled(db_session, workspace_id) is False
